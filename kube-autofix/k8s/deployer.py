"""
Kube-AutoFix — Kubernetes Manifest Deployer.

Responsible for:
  • Ensuring the isolated namespace exists.
  • Parsing multi-document YAML manifests.
  • Applying (create-or-update) resources to the cluster.
  • Deleting resources from a manifest.
  • Cleaning up the namespace on shutdown.

All operations are STRICTLY scoped to the 'autofix-agent-env' namespace.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import KUBE_NAMESPACE
from core.models import DeploymentResult

logger = logging.getLogger("kube-autofix.deployer")

# ── Resource kind → (API class, namespaced create method, namespaced replace method, namespaced delete method) ──
# This registry maps supported K8s resource kinds to the correct
# client methods.  We use explicit dispatch rather than the dynamic
# client for clarity, debuggability, and fine-grained error handling.

_RESOURCE_REGISTRY: dict[str, dict[str, str]] = {
    "Deployment": {
        "api_class": "AppsV1Api",
        "create": "create_namespaced_deployment",
        "replace": "replace_namespaced_deployment",
        "delete": "delete_namespaced_deployment",
        "read": "read_namespaced_deployment",
    },
    "Service": {
        "api_class": "CoreV1Api",
        "create": "create_namespaced_service",
        "replace": "replace_namespaced_service",
        "delete": "delete_namespaced_service",
        "read": "read_namespaced_service",
    },
    "ConfigMap": {
        "api_class": "CoreV1Api",
        "create": "create_namespaced_config_map",
        "replace": "replace_namespaced_config_map",
        "delete": "delete_namespaced_config_map",
        "read": "read_namespaced_config_map",
    },
    "Pod": {
        "api_class": "CoreV1Api",
        "create": "create_namespaced_pod",
        "replace": "replace_namespaced_pod",
        "delete": "delete_namespaced_pod",
        "read": "read_namespaced_pod",
    },
    "Secret": {
        "api_class": "CoreV1Api",
        "create": "create_namespaced_secret",
        "replace": "replace_namespaced_secret",
        "delete": "delete_namespaced_secret",
        "read": "read_namespaced_secret",
    },
}


def _get_api_client(api_class_name: str) -> Any:
    """Instantiate the correct versioned API client."""
    api_cls = getattr(client, api_class_name)
    return api_cls()


class KubeDeployer:
    """
    Applies, updates, and deletes Kubernetes resources from YAML manifests.

    All operations are scoped to KUBE_NAMESPACE ('autofix-agent-env').
    Supports multi-document YAML manifests.
    """

    def __init__(self) -> None:
        # Load kubeconfig from the default location (~/.kube/config).
        # In-cluster config can be added later for production workloads.
        try:
            config.load_kube_config()
            logger.info("Loaded kubeconfig from default location.")
        except config.ConfigException:
            logger.warning(
                "Failed to load kubeconfig, attempting in-cluster config..."
            )
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config.")

        self._core_v1 = client.CoreV1Api()
        self._apps_v1 = client.AppsV1Api()

    # ── Namespace management ──────────────────────────────────────────

    def ensure_namespace(self) -> None:
        """
        Create the isolated agent namespace if it does not already exist.
        This MUST be called before any apply/delete operations.
        """
        try:
            self._core_v1.read_namespace(name=KUBE_NAMESPACE)
            logger.debug(f"Namespace '{KUBE_NAMESPACE}' already exists.")
        except ApiException as e:
            if e.status == 404:
                ns_body = client.V1Namespace(
                    metadata=client.V1ObjectMeta(
                        name=KUBE_NAMESPACE,
                        labels={
                            "managed-by": "kube-autofix",
                            "purpose": "agent-isolated-environment",
                        },
                    )
                )
                self._core_v1.create_namespace(body=ns_body)
                logger.info(f"Created namespace '{KUBE_NAMESPACE}'.")
            else:
                raise

    # ── YAML parsing ──────────────────────────────────────────────────

    @staticmethod
    def parse_manifest(yaml_str: str) -> list[dict[str, Any]]:
        """
        Parse a (potentially multi-document) YAML string into a list
        of Kubernetes resource dictionaries.

        Raises ValueError if the YAML is unparseable or empty.
        """
        try:
            docs = list(yaml.safe_load_all(yaml_str))
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML manifest: {e}") from e

        # Filter out None documents (empty YAML docs separated by ---)
        resources = [doc for doc in docs if doc is not None]

        if not resources:
            raise ValueError("Manifest contains no valid YAML documents.")

        # Validate each document has required fields
        for i, resource in enumerate(resources):
            if not isinstance(resource, dict):
                raise ValueError(
                    f"YAML document {i} is not a mapping (got {type(resource).__name__})."
                )
            for required_key in ("apiVersion", "kind", "metadata"):
                if required_key not in resource:
                    raise ValueError(
                        f"YAML document {i} missing required field '{required_key}'."
                    )

        return resources

    # ── Apply manifest ────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(ApiException),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def _create_or_update_resource(self, resource: dict[str, Any]) -> str:
        """
        Create a single K8s resource.  If it already exists (409),
        replace it with the new spec.

        Returns a human-readable string like "Deployment/autofix-test-nginx".
        """
        kind = resource["kind"]
        name = resource["metadata"]["name"]
        resource_id = f"{kind}/{name}"

        if kind not in _RESOURCE_REGISTRY:
            raise ValueError(
                f"Unsupported resource kind '{kind}'. "
                f"Supported: {list(_RESOURCE_REGISTRY.keys())}"
            )

        registry = _RESOURCE_REGISTRY[kind]
        api = _get_api_client(registry["api_class"])

        # Force the namespace into the resource metadata for safety
        resource.setdefault("metadata", {})
        resource["metadata"]["namespace"] = KUBE_NAMESPACE

        try:
            create_method = getattr(api, registry["create"])
            create_method(namespace=KUBE_NAMESPACE, body=resource)
            logger.info(f"  ✅ Created {resource_id}")
        except ApiException as e:
            if e.status == 409:
                # Resource already exists → replace it
                logger.debug(
                    f"  {resource_id} already exists, replacing..."
                )
                replace_method = getattr(api, registry["replace"])
                replace_method(
                    name=name, namespace=KUBE_NAMESPACE, body=resource
                )
                logger.info(f"  🔄 Replaced {resource_id}")
            else:
                logger.error(
                    f"  ❌ Failed to create {resource_id}: "
                    f"{e.status} {e.reason}"
                )
                raise

        return resource_id

    def apply_manifest(self, yaml_str: str) -> DeploymentResult:
        """
        Parse and apply a YAML manifest to the cluster.

        Handles multi-document YAML.  Each resource is created or
        updated individually.  Namespace is enforced.
        """
        self.ensure_namespace()

        try:
            resources = self.parse_manifest(yaml_str)
        except ValueError as e:
            return DeploymentResult(
                success=False,
                message=f"Manifest parse error: {e}",
            )

        created: list[str] = []
        failed: list[str] = []

        logger.info(
            f"Applying {len(resources)} resource(s) to "
            f"namespace '{KUBE_NAMESPACE}'..."
        )

        for resource in resources:
            kind = resource.get("kind", "Unknown")
            name = resource.get("metadata", {}).get("name", "unnamed")
            resource_id = f"{kind}/{name}"
            try:
                result_id = self._create_or_update_resource(resource)
                created.append(result_id)
            except (ApiException, ValueError) as e:
                failed.append(f"{resource_id}: {e}")
                logger.error(f"  ❌ Failed: {resource_id} — {e}")

        success = len(failed) == 0
        message = (
            f"Applied {len(created)} resource(s) successfully."
            if success
            else f"Applied {len(created)}, failed {len(failed)} resource(s)."
        )

        return DeploymentResult(
            success=success,
            message=message,
            resources_created=created,
            resources_failed=failed,
        )

    # ── Delete manifest ───────────────────────────────────────────────

    def delete_manifest(self, yaml_str: str) -> DeploymentResult:
        """
        Delete all resources defined in a YAML manifest from the cluster.

        Ignores 404 errors (resource already gone).
        """
        try:
            resources = self.parse_manifest(yaml_str)
        except ValueError as e:
            return DeploymentResult(
                success=False,
                message=f"Manifest parse error: {e}",
            )

        deleted: list[str] = []
        failed: list[str] = []

        logger.info(
            f"Deleting {len(resources)} resource(s) from "
            f"namespace '{KUBE_NAMESPACE}'..."
        )

        for resource in resources:
            kind = resource.get("kind", "Unknown")
            name = resource.get("metadata", {}).get("name", "unnamed")
            resource_id = f"{kind}/{name}"

            if kind not in _RESOURCE_REGISTRY:
                failed.append(f"{resource_id}: unsupported kind")
                continue

            registry = _RESOURCE_REGISTRY[kind]
            api = _get_api_client(registry["api_class"])

            try:
                delete_method = getattr(api, registry["delete"])
                delete_method(name=name, namespace=KUBE_NAMESPACE)
                logger.info(f"  🗑️  Deleted {resource_id}")
                deleted.append(resource_id)
            except ApiException as e:
                if e.status == 404:
                    logger.debug(f"  {resource_id} already gone (404).")
                    deleted.append(f"{resource_id} (already absent)")
                else:
                    failed.append(f"{resource_id}: {e.status} {e.reason}")
                    logger.error(f"  ❌ Failed to delete {resource_id}: {e}")

        success = len(failed) == 0
        message = (
            f"Deleted {len(deleted)} resource(s)."
            if success
            else f"Deleted {len(deleted)}, failed {len(failed)}."
        )

        return DeploymentResult(
            success=success,
            message=message,
            resources_created=deleted,  # Reusing field for deleted list
            resources_failed=failed,
        )

    # ── Cleanup ───────────────────────────────────────────────────────

    def cleanup_namespace(self) -> None:
        """
        Delete ALL Deployments, Services, ConfigMaps, Pods, and Secrets
        in the agent namespace.  Used for full environment reset.
        """
        logger.warning(
            f"🧹 Cleaning up ALL resources in namespace '{KUBE_NAMESPACE}'..."
        )

        # Delete deployments first (they own pods via ReplicaSets)
        try:
            deps = self._apps_v1.list_namespaced_deployment(
                namespace=KUBE_NAMESPACE
            )
            for dep in deps.items:
                self._apps_v1.delete_namespaced_deployment(
                    name=dep.metadata.name,
                    namespace=KUBE_NAMESPACE,
                    body=client.V1DeleteOptions(
                        propagation_policy="Foreground"
                    ),
                )
                logger.info(f"  🗑️  Deleted Deployment/{dep.metadata.name}")
        except ApiException as e:
            logger.error(f"  Failed to clean Deployments: {e}")

        # Delete services
        try:
            svcs = self._core_v1.list_namespaced_service(
                namespace=KUBE_NAMESPACE
            )
            for svc in svcs.items:
                # Skip the default kubernetes service
                if svc.metadata.name == "kubernetes":
                    continue
                self._core_v1.delete_namespaced_service(
                    name=svc.metadata.name, namespace=KUBE_NAMESPACE
                )
                logger.info(f"  🗑️  Deleted Service/{svc.metadata.name}")
        except ApiException as e:
            logger.error(f"  Failed to clean Services: {e}")

        # Delete configmaps
        try:
            cms = self._core_v1.list_namespaced_config_map(
                namespace=KUBE_NAMESPACE
            )
            for cm in cms.items:
                self._core_v1.delete_namespaced_config_map(
                    name=cm.metadata.name, namespace=KUBE_NAMESPACE
                )
                logger.info(f"  🗑️  Deleted ConfigMap/{cm.metadata.name}")
        except ApiException as e:
            logger.error(f"  Failed to clean ConfigMaps: {e}")

        logger.info("🧹 Namespace cleanup complete.")
