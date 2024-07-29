#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
"""Generic coordinator for a distributed charm deployment."""

import glob
import json
import logging
import os
import re
import shutil
import socket
from functools import partial
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Set, TypedDict
from urllib.parse import urlparse

import ops
import yaml

import cosl
from cosl.coordinated_workers.interface import ClusterProvider
from cosl.coordinated_workers.nginx import Nginx, NginxMappingOverrides, NginxPrometheusExporter
from cosl.helpers import check_libs_installed

check_libs_installed(
    "charms.data_platform_libs.v0.s3",
    "charms.grafana_k8s.v0.grafana_source",
    "charms.grafana_k8s.v0.grafana_dashboard",
    "charms.observability_libs.v1.cert_handler",
    "charms.prometheus_k8s.v0.prometheus_scrape",
    "charms.loki_k8s.v1.loki_push_api",
    "charms.tempo_k8s.v2.tracing",
)

from charms.data_platform_libs.v0.s3 import S3Requirer
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.loki_k8s.v1.loki_push_api import LokiPushApiConsumer
from charms.observability_libs.v1.cert_handler import VAULT_SECRET_LABEL, CertHandler
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.tempo_k8s.v2.tracing import TracingEndpointRequirer

logger = logging.getLogger(__name__)


# The paths of the base rules to be rendered in CONSOLIDATED_ALERT_RULES_PATH
NGINX_ORIGINAL_ALERT_RULES_PATH = "./src/prometheus_alert_rules/nginx"
WORKER_ORIGINAL_ALERT_RULES_PATH = "./src/prometheus_alert_rules/workers"
# The path of the rules that will be sent to Prometheus
CONSOLIDATED_ALERT_RULES_PATH = "./src/prometheus_alert_rules/consolidated_rules"


class S3NotFoundError(Exception):
    """Raised when the s3 integration is not present or not ready."""


class ClusterRolesConfig(Protocol):
    """Worker roles and deployment requirements."""

    roles: Iterable[str]
    meta_roles: Mapping[str, Iterable[str]]
    minimal_deployment: Iterable[str]
    recommended_deployment: Dict[str, int]


def validate_roles_config(roles_config: ClusterRolesConfig) -> None:
    """Assert that all the used roles have been defined."""
    roles = set(roles_config.roles)
    assert set(roles_config.meta_roles.keys()).issubset(roles)
    for role_set in roles_config.meta_roles.values():
        assert set(role_set).issubset(roles)
    assert set(roles_config.minimal_deployment).issubset(roles)
    assert set(roles_config.recommended_deployment.keys()).issubset(roles)


_EndpointMapping = TypedDict(
    "_EndpointMapping",
    {
        "certificates": str,
        "cluster": str,
        "grafana-dashboards": str,
        "logging": str,
        "metrics": str,
        "tracing": str,
        "s3": str,
    },
    total=True,
)
"""Mapping of the relation endpoint names that the charms uses, as defined in metadata.yaml."""


class Coordinator(ops.Object):
    """Charming coordinator.

    This class takes care of the shared tasks of a coordinator, including handling workers,
    running Nginx, and implementing self-monitoring integrations.
    """

    def __init__(
        self,
        charm: ops.CharmBase,
        roles_config: ClusterRolesConfig,
        s3_bucket_name: str,
        external_url: str,  # the ingressed url if we have ingress, else fqdn
        worker_metrics_port: int,
        endpoints: _EndpointMapping,
        nginx_config: Callable[["Coordinator"], str],
        workers_config: Callable[["Coordinator"], str],
        nginx_options: Optional[NginxMappingOverrides] = None,
        is_coherent: Optional[Callable[[ClusterProvider, ClusterRolesConfig], bool]] = None,
        is_recommended: Optional[Callable[[ClusterProvider, ClusterRolesConfig], bool]] = None,
        tracing_receivers: Optional[Callable[[], Dict[str, str]]] = None,
    ):
        """Constructor for a Coordinator object.

        Args:
            charm: The coordinator charm object.
            roles_config: Definition of the roles and the deployment requirements.
            s3_bucket_name: The name of the S3 Bucket to use.
            external_url: The external (e.g., ingressed) URL of the coordinator charm.
            worker_metrics_port: The port under which workers expose their metrics.
            nginx_config: A function generating the Nginx configuration file for the workload.
            workers_config: A function generating the configuration for the workers, to be
                published in relation data.
            endpoints: Endpoint names for coordinator relations, as defined in metadata.yaml.
            nginx_options: Non-default config options for Nginx.
            is_coherent: Custom coherency checker for a minimal deployment.
            is_recommended: Custom coherency checker for a recommended deployment.
            tracing_receivers: Endpoints to which the workload (and the worker charm) can push traces to.
        """
        super().__init__(charm, key="coordinator")
        self._charm = charm
        self.topology = cosl.JujuTopology.from_charm(self._charm)
        self._external_url = external_url
        self._worker_metrics_port = worker_metrics_port

        self._endpoints = endpoints

        validate_roles_config(roles_config)
        self.roles_config = roles_config

        self.cluster = ClusterProvider(
            self._charm,
            frozenset(roles_config.roles),
            roles_config.meta_roles,
            endpoint=self._endpoints["cluster"],
        )

        self._is_coherent = is_coherent
        self._is_recommended = is_recommended
        self._tracing_receivers_getter = tracing_receivers

        self.nginx = Nginx(
            self._charm,
            partial(nginx_config, self),
            options=nginx_options,
        )
        self._workers_config_getter = partial(workers_config, self)
        self.nginx_exporter = NginxPrometheusExporter(self._charm, options=nginx_options)

        self.cert_handler = CertHandler(
            self._charm,
            certificates_relation_name=self._endpoints["certificates"],
            # let's assume we don't need the peer relation as all coordinator charms will assume juju secrets
            key="coordinator-server-cert",
            sans=[self.hostname],
        )

        self.s3_requirer = S3Requirer(self._charm, self._endpoints["s3"], s3_bucket_name)

        self._grafana_dashboards = GrafanaDashboardProvider(
            self._charm, relation_name=self._endpoints["grafana-dashboards"]
        )

        self._logging = LokiPushApiConsumer(self._charm, relation_name=self._endpoints["logging"])

        # Provide ability for this to be scraped by Prometheus using prometheus_scrape
        refresh_events = [self._charm.on.update_status, self.cluster.on.changed]
        if self.cert_handler:
            refresh_events.append(self.cert_handler.on.cert_changed)

        self._render_alert_rules()
        self._scraping = MetricsEndpointProvider(
            self._charm,
            relation_name=self._endpoints["metrics"],
            alert_rules_path=CONSOLIDATED_ALERT_RULES_PATH,
            jobs=self._scrape_jobs,
            external_url=self._external_url,
            refresh_event=refresh_events,
        )

        self.tracing = TracingEndpointRequirer(
            self._charm, relation_name=self._endpoints["tracing"], protocols=["otlp_http"]
        )

        # We always listen to collect-status
        self.framework.observe(self._charm.on.collect_unit_status, self._on_collect_unit_status)

        # If the cluster isn't ready, refuse to handle any other event as we can't possibly know what to do
        if not self.cluster.has_workers:
            logger.warning(
                f"Incoherent deployment. {charm.unit.name} is missing relation to workers. "
                "This charm will be unresponsive and refuse to handle any event until "
                "the situation is resolved by the cloud admin, to avoid data loss."
            )
            return
        if not self.is_coherent:
            logger.error(
                f"Incoherent deployment. {charm.unit.name} will be shutting down. "
                "This likely means you are lacking some required roles in your workers. "
                "This charm will be unresponsive and refuse to handle any event until "
                "the situation is resolved by the cloud admin, to avoid data loss."
            )
            return
        if self.cluster.has_workers and not self.s3_ready:
            logger.error(
                f"Incoherent deployment. {charm.unit.name} will be shutting down. "
                "This likely means you need to add an s3 integration. "
                "This charm will be unresponsive and refuse to handle any event until "
                "the situation is resolved by the cloud admin, to avoid data loss."
            )
            return

        # lifecycle
        self.framework.observe(self._charm.on.config_changed, self._on_config_changed)

        # nginx
        self.framework.observe(self._charm.on.nginx_pebble_ready, self._on_nginx_pebble_ready)
        self.framework.observe(
            self._charm.on.nginx_prometheus_exporter_pebble_ready,
            self._on_nginx_prometheus_exporter_pebble_ready,
        )

        # s3
        self.framework.observe(
            self.s3_requirer.on.credentials_changed, self._on_s3_credentials_changed
        )
        self.framework.observe(self.s3_requirer.on.credentials_gone, self._on_s3_credentials_gone)

        # tracing
        # self.framework.observe(self._charm.on.peers_relation_created, self._on_peers_relation_created)
        # self.framework.observe(self._charm.on.peers_relation_changed, self._on_peers_relation_changed)

        # logging
        self.framework.observe(
            self._logging.on.loki_push_api_endpoint_joined, self._on_loki_relation_changed
        )
        self.framework.observe(
            self._logging.on.loki_push_api_endpoint_departed, self._on_loki_relation_changed
        )

        # tls
        self.framework.observe(self.cert_handler.on.cert_changed, self._on_cert_handler_changed)

        # cluster
        self.framework.observe(self.cluster.on.changed, self._on_cluster_changed)

    ######################
    # UTILITY PROPERTIES #
    ######################

    @property
    def is_coherent(self) -> bool:
        """Check whether this coordinator is coherent."""
        if manual_coherency_checker := self._is_coherent:
            return manual_coherency_checker(self.cluster, self.roles_config)

        rc = self.roles_config
        minimal_deployment = set(rc.minimal_deployment)
        cluster = self.cluster
        roles = cluster.gather_roles()

        # Whether the roles list makes up a coherent mimir deployment.
        is_coherent = set(roles.keys()).issuperset(minimal_deployment)

        return is_coherent

    @property
    def missing_roles(self) -> Set[str]:
        """What roles are missing from this cluster, if any."""
        roles = self.cluster.gather_roles()
        missing_roles: Set[str] = set(self.roles_config.minimal_deployment).difference(
            roles.keys()
        )
        return missing_roles

    @property
    def is_recommended(self) -> Optional[bool]:
        """Check whether this coordinator is connected to the recommended number of workers.

        Will return None if no recommended criterion is defined.
        """
        if manual_recommended_checker := self._is_recommended:
            return manual_recommended_checker(self.cluster, self.roles_config)

        rc = self.roles_config
        if not rc.recommended_deployment:
            # we don't have a definition of recommended: return None
            return None

        cluster = self.cluster
        roles = cluster.gather_roles()
        for role, min_n in rc.recommended_deployment.items():
            if roles.get(role, 0) < min_n:
                return False
        return True

    @property
    def can_handle_events(self) -> bool:
        """Check whether the coordinaator should handle events."""
        return self.cluster.has_workers and self.is_coherent and self.s3_ready

    @property
    def hostname(self) -> str:
        """Unit's hostname."""
        return socket.getfqdn()

    @property
    def _internal_url(self) -> str:
        """Unit's hostname including the scheme."""
        scheme = "https" if self.tls_available else "http"
        return f"{scheme}://{self.hostname}"

    @property
    def tls_available(self) -> bool:
        """Return True if tls is enabled and the necessary certs are found."""
        return (
            self.cert_handler.enabled
            and (self.cert_handler.server_cert is not None)
            and (self.cert_handler.private_key is not None)  # type: ignore
            and (self.cert_handler.ca_cert is not None)
        )

    @property
    def _s3_config(self) -> Dict[str, Any]:
        """The s3 configuration from relation data.

        The configuration is adapted to a drop-in format for the HA workers to use.

        Raises:
            S3NotFoundError: The s3 integration is inactive.
        """
        s3_data = self.s3_requirer.get_s3_connection_info()
        s3_config: Dict[str, Any] = {}
        if not (
            s3_data
            and "bucket" in s3_data
            and "endpoint" in s3_data
            and "access-key" in s3_data
            and "secret-key" in s3_data
        ):
            raise S3NotFoundError("s3 integration inactive")
        s3_config["insecure"] = not s3_data["endpoint"].startswith("https://")
        s3_config["endpoint"] = re.sub(
            rf"^{urlparse(s3_data['endpoint']).scheme}://", "", s3_data["endpoint"]
        )
        s3_config["region"] = s3_data.get("region", "")
        s3_config["access_key_id"] = s3_data.pop("access-key")
        s3_config["secret_access_key"] = s3_data.pop("secret-key")
        s3_config["bucket_name"] = s3_data.pop("bucket")
        return s3_config

    @property
    def s3_ready(self) -> bool:
        """Check whether s3 is configured."""
        try:
            return bool(self._s3_config)
        except S3NotFoundError:
            return False

    @property
    def peer_addresses(self) -> List[str]:
        """If a peer relation is present, return the addresses of the peers."""
        peers = self._peers
        relation = self.model.get_relation("peers")
        # get unit addresses for all the other units from a databag
        addresses = []
        if peers and relation:
            addresses = [relation.data[unit].get("local-ip") for unit in peers]
            addresses = list(filter(None, addresses))

        # add own address
        if self._local_ip:
            addresses.append(self._local_ip)

        return addresses

    @property
    def _local_ip(self) -> Optional[str]:
        """Local IP of the peers binding."""
        try:
            binding = self.model.get_binding("peers")
            if not binding:
                logger.error(
                    "unable to get local IP at this time: "
                    "peers binding not active yet. It could be that the charm "
                    "is still being set up..."
                )
                return None
            return str(binding.network.bind_address)
        except (ops.ModelError, KeyError) as e:
            logger.debug("failed to obtain local ip from peers binding", exc_info=True)
            logger.error(
                f"unable to get local IP at this time: failed with {type(e)}; "
                f"see debug log for more info"
            )
            return None

    @property
    def _workers_scrape_jobs(self) -> List[Dict[str, Any]]:
        """The Prometheus scrape jobs for the workers connected to the coordinator."""
        scrape_jobs: List[Dict[str, Any]] = []
        worker_topologies = self.cluster.gather_topology()

        for worker in worker_topologies:
            job = {
                "static_configs": [
                    {
                        "targets": [f"{worker['address']}:{self._worker_metrics_port}"],
                    }
                ],
                # setting these as "labels" in the static config gets some of them
                # replaced by the coordinator topology
                # https://github.com/canonical/prometheus-k8s-operator/issues/571
                "relabel_configs": [
                    {"target_label": "juju_charm", "replacement": worker["charm_name"]},
                    {"target_label": "juju_unit", "replacement": worker["unit"]},
                    {"target_label": "juju_application", "replacement": worker["application"]},
                    {"target_label": "juju_model", "replacement": self.model.name},
                    {"target_label": "juju_model_uuid", "replacement": self.model.uuid},
                ],
            }
            scrape_jobs.append(job)
        return scrape_jobs

    @property
    def _nginx_scrape_jobs(self) -> List[Dict[str, Any]]:
        """The Prometheus scrape job for Nginx."""
        job: Dict[str, Any] = {
            "static_configs": [
                {"targets": [f"{self.hostname}:{self.nginx.options['nginx_port']}"]}
            ]
        }
        return [job]

    @property
    def _scrape_jobs(self) -> List[Dict[str, Any]]:
        """The scrape jobs to send to Prometheus."""
        return self._workers_scrape_jobs + self._nginx_scrape_jobs

    ##################
    # EVENT HANDLERS #
    ##################
    def _on_cert_handler_changed(self, _: ops.RelationChangedEvent):
        if self.tls_available:
            logger.debug("enabling TLS")
            self.nginx.configure_tls(
                server_cert=self.cert_handler.server_cert,  # type: ignore
                ca_cert=self.cert_handler.ca_cert,  # type: ignore
                private_key=self.cert_handler.private_key,  # type: ignore
            )
        else:
            logger.debug("disabling TLS")
            self.nginx.delete_certificates()

        # notify the cluster
        self.update_cluster()

    def _on_cluster_changed(self, _: ops.RelationEvent):
        self.update_cluster()

    def _on_nginx_pebble_ready(self, _: ops.PebbleReadyEvent):
        self.update_cluster()

    def _on_nginx_prometheus_exporter_pebble_ready(self, _: ops.PebbleReadyEvent):
        self.update_cluster()

    def _on_loki_relation_changed(self, _: ops.EventBase):
        self.update_cluster()

    def _on_s3_credentials_changed(self, _: ops.RelationChangedEvent):
        self._on_s3_changed()

    def _on_s3_credentials_gone(self, _: ops.RelationChangedEvent):
        self._on_s3_changed()

    def _on_s3_changed(self):
        self.update_cluster()

    def _on_peers_relation_created(self, event: ops.RelationCreatedEvent):
        if self._local_ip:
            event.relation.data[self._charm.unit]["local-ip"] = self._local_ip

    def _on_peers_relation_changed(self, _: ops.RelationChangedEvent):
        self.update_cluster()

    def _on_config_changed(self, _: ops.ConfigChangedEvent):
        if self.tls_available:
            self.nginx.configure_tls(
                server_cert=self.cert_handler.server_cert,  # type: ignore
                ca_cert=self.cert_handler.ca_cert,  # type: ignore
                private_key=self.cert_handler.private_key,  # type: ignore
            )
        else:
            self.nginx.delete_certificates()
        self.update_cluster()

    # keep this event handler at the bottom
    def _on_collect_unit_status(self, e: ops.CollectStatusEvent):
        # todo add [nginx.workload] statuses

        if not self.cluster.has_workers:
            e.add_status(ops.BlockedStatus("[consistency] Missing any worker relation."))
        if not self.is_coherent:
            e.add_status(ops.BlockedStatus("[consistency] Cluster inconsistent."))
        if not self.s3_ready:
            e.add_status(ops.BlockedStatus("[consistency] Missing S3 integration."))
        elif not self.is_recommended:
            # if is_recommended is None: it means we don't have a recommended deployment criterion.
            e.add_status(ops.ActiveStatus("[coordinator] Degraded."))
        else:
            e.add_status(ops.ActiveStatus())

    ###################
    # UTILITY METHODS #
    ###################
    @property
    def _peers(self) -> Optional[Set[ops.model.Unit]]:
        relation = self.model.get_relation("peers")
        if not relation:
            return None

        # self is not included in relation.units
        return relation.units

    @property
    def loki_endpoints_by_unit(self) -> Dict[str, str]:
        """Loki endpoints from relation data in the format needed for Pebble log forwarding.

        Returns:
            A dictionary of remote units and the respective Loki endpoint.
            {
                "loki/0": "http://loki:3100/loki/api/v1/push",
                "another-loki/0": "http://another-loki:3100/loki/api/v1/push",
            }
        """
        endpoints: Dict[str, str] = {}
        relations: List[ops.Relation] = self.model.relations.get(self._endpoints["logging"], [])

        for relation in relations:
            for unit in relation.units:
                if "endpoint" not in relation.data[unit]:
                    continue
                endpoint = relation.data[unit]["endpoint"]
                deserialized_endpoint = json.loads(endpoint)
                url = deserialized_endpoint["url"]
                endpoints[unit.name] = url

        return endpoints

    def update_cluster(self):
        """Build the workers config and distribute it to the relations."""
        self.nginx.configure_pebble_layer()
        self.nginx_exporter.configure_pebble_layer()
        if not self.is_coherent:
            logger.error("skipped cluster update: incoherent deployment")
            return

        if not self._charm.unit.is_leader():
            return
        # we share the certs in plaintext as they're not sensitive information
        # On every function call, we always publish everything to the databag; however, if there
        # are no changes, Juju will notice there's no delta and do nothing
        self.cluster.publish_data(
            worker_config=self._workers_config_getter(),
            loki_endpoints=self.loki_endpoints_by_unit,
            **(
                {
                    "ca_cert": self.cert_handler.ca_cert,
                    "server_cert": self.cert_handler.server_cert,
                    "privkey_secret_id": self.cluster.grant_privkey(VAULT_SECRET_LABEL),
                }
                if self.tls_available
                else {}
            ),
            **(
                {
                    "tracing_receivers": self._tracing_receivers_getter(),
                }
                if self._tracing_receivers_getter
                else {}
            ),
        )

    def _render_workers_alert_rules(self):
        """Regenerate the worker alert rules from relation data."""
        self._remove_rendered_alert_rules()

        apps: Set[str] = set()
        for worker in self.cluster.gather_topology():
            if worker["application"] in apps:
                continue

            apps.add(worker["application"])
            topology_dict = {
                "model": self.model.name,
                "model_uuid": self.model.uuid,
                "application": worker["application"],
                "unit": worker["unit"],
                "charm_name": worker["charm_name"],
            }
            topology = cosl.JujuTopology.from_dict(topology_dict)
            alert_rules = cosl.AlertRules(query_type="promql", topology=topology)
            alert_rules.add_path(WORKER_ORIGINAL_ALERT_RULES_PATH, recursive=True)
            alert_rules_contents = yaml.dump(alert_rules.as_dict())

            file_name = f"{CONSOLIDATED_ALERT_RULES_PATH}/rendered_{worker['application']}.rules"
            with open(file_name, "w") as writer:
                writer.write(alert_rules_contents)

    def _remove_rendered_alert_rules(self):
        files = glob.glob(f"{CONSOLIDATED_ALERT_RULES_PATH}/rendered_*")
        for f in files:
            os.remove(f)

    def _consolidate_nginx_alert_rules(self):
        """Copy Nginx alert rules to the consolidated alert folder."""
        for filename in glob.glob(os.path.join(NGINX_ORIGINAL_ALERT_RULES_PATH, "*.*")):
            shutil.copy(filename, f"{CONSOLIDATED_ALERT_RULES_PATH}/")

    def _render_alert_rules(self):
        """Render the alert rules for Nginx and the connected workers."""
        os.makedirs(CONSOLIDATED_ALERT_RULES_PATH, exist_ok=True)
        self._render_workers_alert_rules()
        self._consolidate_nginx_alert_rules()