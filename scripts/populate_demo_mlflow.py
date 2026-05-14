"""
Script to populate a local MLflow instance with synthetic data.
This generates demo observability traces for Kube-AutoFix without requiring active credentials.
"""

import sys
import time
import os
from pathlib import Path

try:
    import mlflow
except ImportError:
    print("MLflow not installed. Run `pip install mlflow` first.")
    sys.exit(1)

# Ensure mlruns directory exists
os.environ["MLFLOW_TRACKING_URI"] = "file:./mlruns"
mlflow.set_tracking_uri("file:./mlruns")

EXPERIMENT_NAME = "kube-autofix-agent-observability"
mlflow.set_experiment(EXPERIMENT_NAME)

DEMO_RUNS = [
    {
        "name": "kube-autofix-demo-ImagePullBackOff",
        "params": {
            "manifest_name": "frontend-deployment.yaml",
            "model_name": "gpt-4o",
            "namespace": "autofix-agent-env",
            "max_iterations": 5,
            "dry_run": False,
            "mlflow_log_prompts": True,
            "project_name": "kube-autofix"
        },
        "tags": {
            "agent_type": "kubernetes_sre_agent",
            "safety_model": "pydantic_yaml_validation_namespace_lock",
            "integration": "databricks_mlflow",
            "incident_type": "ImagePullBackOff"
        },
        "iterations": [
            {
                "duration": 4.2,
                "metrics": {
                    "resources_created_count": 1,
                    "resources_failed_count": 1,
                    "debug_pod_count": 1,
                    "llm_latency_seconds": 2.5,
                    "llm_confidence_score": 0.95
                },
                "artifacts": {
                    "debug_summary.txt": "Pod frontend-7c85d8b-xyz is in ImagePullBackOff state.\nMessage: Back-off pulling image \"nginx:not-exist\".\nSecret token <REDACTED> found in env.",
                    "root_cause.txt": "The deployment specifies an invalid container image tag 'nginx:not-exist'.",
                    "changes.txt": "- Updated image from 'nginx:not-exist' to 'nginx:latest'",
                    "corrected.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: frontend\n  namespace: autofix-agent-env\nspec:\n  template:\n    spec:\n      containers:\n      - name: web\n        image: nginx:latest\n"
                }
            }
        ],
        "final_success": True
    },
    {
        "name": "kube-autofix-demo-CrashLoopBackOff",
        "params": {
            "manifest_name": "backend-api.yaml",
            "model_name": "gpt-4o",
            "namespace": "autofix-agent-env",
            "max_iterations": 5,
            "dry_run": False,
            "mlflow_log_prompts": True,
            "project_name": "kube-autofix"
        },
        "tags": {
            "agent_type": "kubernetes_sre_agent",
            "safety_model": "pydantic_yaml_validation_namespace_lock",
            "integration": "databricks_mlflow",
            "incident_type": "CrashLoopBackOff"
        },
        "iterations": [
            {
                "duration": 6.8,
                "metrics": {
                    "resources_created_count": 2,
                    "resources_failed_count": 1,
                    "debug_pod_count": 1,
                    "llm_latency_seconds": 3.8,
                    "llm_confidence_score": 0.82
                },
                "artifacts": {
                    "debug_summary.txt": "Pod backend-api-98ab-123 is crashing.\nLogs: KeyError: 'DATABASE_URL'.",
                    "root_cause.txt": "The container requires a DATABASE_URL environment variable but it is missing from the spec.",
                    "changes.txt": "- Added missing env var DATABASE_URL using a ConfigMap reference.",
                    "corrected.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: backend-api\n  namespace: autofix-agent-env\nspec:\n  template:\n    spec:\n      containers:\n      - name: api\n        image: my-backend:1.0\n        env:\n        - name: DATABASE_URL\n          valueFrom:\n            configMapKeyRef:\n              name: app-config\n              key: db_url\n"
                }
            },
            {
                "duration": 3.5,
                "metrics": {
                    "resources_created_count": 2,
                    "resources_failed_count": 0,
                    "debug_pod_count": 0,
                },
                "artifacts": {}
            }
        ],
        "final_success": True
    },
    {
        "name": "kube-autofix-demo-ResourceLimits",
        "params": {
            "manifest_name": "data-processor.yaml",
            "model_name": "gpt-4o",
            "namespace": "autofix-agent-env",
            "max_iterations": 5,
            "dry_run": False,
            "mlflow_log_prompts": True,
            "project_name": "kube-autofix"
        },
        "tags": {
            "agent_type": "kubernetes_sre_agent",
            "safety_model": "pydantic_yaml_validation_namespace_lock",
            "integration": "databricks_mlflow",
            "incident_type": "ResourceQuota"
        },
        "iterations": [
            {
                "duration": 5.1,
                "metrics": {
                    "resources_created_count": 0,
                    "resources_failed_count": 1,
                    "debug_pod_count": 0,
                    "llm_latency_seconds": 2.2,
                    "llm_confidence_score": 0.99
                },
                "artifacts": {
                    "debug_summary.txt": "Failed to create pod: pods \"data-processor\" is forbidden: failed quota: compute-resources: must specify limits.cpu, limits.memory.",
                    "root_cause.txt": "The namespace enforces a ResourceQuota but the manifest does not specify resource limits.",
                    "changes.txt": "- Added reasonable default CPU and memory requests/limits.",
                    "corrected.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: data-processor\n  namespace: autofix-agent-env\nspec:\n  template:\n    spec:\n      containers:\n      - name: worker\n        image: worker:latest\n        resources:\n          requests:\n            cpu: 100m\n            memory: 128Mi\n          limits:\n            cpu: 500m\n            memory: 512Mi\n"
                }
            }
        ],
        "final_success": True
    }
]


def run_demo_population():
    print("Populating synthetic MLflow traces...")
    
    # Create temp dir for artifacts
    temp_dir = Path("temp_artifacts")
    temp_dir.mkdir(exist_ok=True)
    
    for i, run_data in enumerate(DEMO_RUNS):
        with mlflow.start_run(run_name=run_data["name"]):
            print(f"  -> Generating run: {run_data['name']}")
            
            mlflow.log_params(run_data["params"])
            mlflow.set_tags(run_data["tags"])
            
            total_iterations = len(run_data["iterations"])
            
            for iter_idx, iter_data in enumerate(run_data["iterations"], start=1):
                step = iter_idx
                mlflow.log_metric("iteration_duration_seconds", iter_data["duration"], step=step)
                for metric_name, metric_val in iter_data["metrics"].items():
                    mlflow.log_metric(metric_name, metric_val, step=step)
                
                # Write artifacts and log them
                if iter_data["artifacts"]:
                    iter_folder = temp_dir / f"iteration_{step}"
                    iter_folder.mkdir(exist_ok=True)
                    
                    for filename, content in iter_data["artifacts"].items():
                        file_path = iter_folder / filename
                        file_path.write_text(content, encoding="utf-8")
                    
                    mlflow.log_artifacts(str(iter_folder), artifact_path=f"iteration_{step}")
            
            mlflow.log_metric("total_iterations", total_iterations)
            mlflow.log_metric("success", 1 if run_data["final_success"] else 0)
            
    print("Done! You can now run `mlflow ui --backend-store-uri ./mlruns` to view the synthetic data.")

if __name__ == "__main__":
    run_demo_population()
