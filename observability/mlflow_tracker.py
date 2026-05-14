"""
MLflow tracking wrapper for Kube-AutoFix agent observability.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings
    from core.models import DebugBundle, LLMDiagnosis

logger = logging.getLogger(__name__)


class MLflowTracker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = settings.enable_mlflow
        self._mlflow = None
        self._run_id = None
        self._active_run = None

        if self.enabled:
            try:
                import mlflow
                self._mlflow = mlflow
                self._init_mlflow()
            except ImportError:
                logger.warning(
                    "MLflow is enabled but not installed. Tracking disabled. "
                    "Run `pip install mlflow` to enable."
                )
                self.enabled = False
            except Exception as e:
                logger.warning(f"Failed to initialize MLflow: {e}")
                self.enabled = False

    def _init_mlflow(self) -> None:
        if not self._mlflow:
            return
        
        try:
            self._mlflow.set_tracking_uri(self.settings.mlflow_tracking_uri)
            self._mlflow.set_experiment(self.settings.mlflow_experiment_name)
        except Exception as e:
            logger.warning(f"MLflow setup failed: {e}")
            self.enabled = False

    def _safe_call(self, func, *args, **kwargs):
        if not self.enabled or not self._mlflow:
            return None
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.warning(f"MLflow error: {e}")
            return None

    def _redact_secrets(self, text: str) -> str:
        if not text:
            return text
        # Redact common secret keys
        pattern = re.compile(
            r"(?i)(password|token|secret|api_key|access_key|private_key|credential|authorization)[\s:=]+([^\s,]+)",
            re.IGNORECASE
        )
        return pattern.sub(r"\1: <REDACTED>", text)

    def start_loop_run(
        self,
        manifest_path_or_name: str,
        model_name: str,
        namespace: str,
        max_iterations: int,
        dry_run: bool
    ) -> None:
        if not self.enabled or not self._mlflow:
            return

        def _start():
            import pathlib
            manifest_filename = pathlib.Path(manifest_path_or_name).name
            run_name = f"{self.settings.mlflow_run_name_prefix}-{manifest_filename}"
            self._active_run = self._mlflow.start_run(run_name=run_name)
            self._run_id = self._active_run.info.run_id
            
            self._mlflow.log_params({
                "manifest_name": manifest_path_or_name,
                "model_name": model_name,
                "namespace": namespace,
                "max_iterations": max_iterations,
                "dry_run": dry_run,
                "mlflow_log_prompts": self.settings.mlflow_log_prompts,
                "project_name": "kube-autofix"
            })
            self._mlflow.set_tags({
                "agent_type": "kubernetes_sre_agent",
                "safety_model": "pydantic_yaml_validation_namespace_lock",
                "integration": "databricks_mlflow"
            })
        
        self._safe_call(_start)

    def log_iteration_start(self, iteration: int) -> None:
        pass

    def log_deploy_result(
        self,
        iteration: int,
        success: bool,
        message: str,
        resources_created: list[str],
        resources_failed: list[str]
    ) -> None:
        if not self.enabled: return
        def _log():
            self._mlflow.log_metric("resources_created_count", len(resources_created), step=iteration)
            self._mlflow.log_metric("resources_failed_count", len(resources_failed), step=iteration)
        self._safe_call(_log)

    def log_monitor_result(
        self,
        iteration: int,
        is_success: bool,
        message: str,
        failure_reasons: list[str] | None = None
    ) -> None:
        pass

    def log_debug_bundle(self, iteration: int, debug_bundle: DebugBundle) -> None:
        if not self.enabled: return
        def _log():
            pod_count = len(debug_bundle.pod_debug_infos)
            self._mlflow.log_metric("debug_pod_count", pod_count, step=iteration)
            
            if self.settings.mlflow_log_prompts:
                summary = self._redact_secrets(debug_bundle.summary)
                self._mlflow.log_text(summary, f"iteration_{iteration}/debug_summary.txt")
        self._safe_call(_log)

    def log_llm_result(self, iteration: int, diagnosis: LLMDiagnosis, latency_seconds: float | None = None) -> None:
        if not self.enabled: return
        def _log():
            if latency_seconds is not None:
                self._mlflow.log_metric("llm_latency_seconds", latency_seconds, step=iteration)
            
            self._mlflow.log_metric("llm_confidence_score", diagnosis.confidence_score, step=iteration)
            
            self._mlflow.log_text(diagnosis.root_cause, f"iteration_{iteration}/root_cause.txt")
            if diagnosis.changes_made:
                changes_str = "\n".join(diagnosis.changes_made)
                self._mlflow.log_text(changes_str, f"iteration_{iteration}/changes.txt")
            
            safe_yaml = self._redact_secrets(diagnosis.corrected_yaml)
            self._mlflow.log_text(safe_yaml, f"iteration_{iteration}/corrected.yaml")
        self._safe_call(_log)

    def log_llm_error(self, iteration: int, error: Exception) -> None:
        if not self.enabled: return
        def _log():
            self._mlflow.log_text(str(error), f"iteration_{iteration}/llm_error.txt")
        self._safe_call(_log)

    def log_iteration_end(self, iteration: int, outcome: str, duration_seconds: float, confidence_score: float | None = None) -> None:
        if not self.enabled: return
        def _log():
            self._mlflow.log_metric("iteration_duration_seconds", duration_seconds, step=iteration)
        self._safe_call(_log)

    def log_final_result(self, success: bool, total_iterations: int) -> None:
        if not self.enabled: return
        def _log():
            self._mlflow.log_metric("total_iterations", total_iterations)
            self._mlflow.log_metric("success", 1 if success else 0)
        self._safe_call(_log)

    def end_run(self, status: str = "FINISHED") -> None:
        if not self.enabled or not self._mlflow: return
        def _end():
            if self._active_run:
                self._mlflow.end_run(status=status)
                self._active_run = None
                self._run_id = None
        self._safe_call(_end)
