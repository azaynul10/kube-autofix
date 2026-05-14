"""Tests for the MLflowTracker."""

import unittest
from unittest.mock import MagicMock, patch

from config import Settings
from core.models import DebugBundle, PodDebugInfo, LLMDiagnosis
from observability.mlflow_tracker import MLflowTracker

class TestMLflowTracker(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            openai_api_key="test",
            enable_mlflow=True,
            mlflow_tracking_uri="file:./mlruns",
            mlflow_experiment_name="test-exp"
        )
        self.mock_mlflow = MagicMock()
        
    def test_disabled_tracker_is_noop(self):
        with patch.dict('sys.modules', {'mlflow': self.mock_mlflow}):
            self.settings.enable_mlflow = False
            tracker = MLflowTracker(self.settings)
            
            self.assertFalse(tracker.enabled)
            
            # Ensure methods don't crash and don't call mlflow
            tracker.start_loop_run("test.yaml", "gpt-4o", "default", 5, False)
            tracker.log_iteration_start(1)
            tracker.log_final_result(True, 3)
            tracker.end_run()
            
            self.mock_mlflow.start_run.assert_not_called()

    def test_tracker_initializes_when_enabled(self):
        with patch.dict('sys.modules', {'mlflow': self.mock_mlflow}):
            tracker = MLflowTracker(self.settings)
            self.assertTrue(tracker.enabled)
            self.mock_mlflow.set_tracking_uri.assert_called_with("file:./mlruns")
            self.mock_mlflow.set_experiment.assert_called_with("test-exp")

    def test_tracker_handles_missing_mlflow(self):
        # We simulate ImportError by removing mlflow from sys.modules and letting import fail
        # Or mock __import__ to raise ImportError for 'mlflow'
        with patch('builtins.__import__', side_effect=ImportError("No module named 'mlflow'")):
            tracker = MLflowTracker(self.settings)
            self.assertFalse(tracker.enabled)

    def test_log_llm_result_does_not_crash(self):
        with patch.dict('sys.modules', {'mlflow': self.mock_mlflow}):
            tracker = MLflowTracker(self.settings)
            
            diagnosis = LLMDiagnosis(
                root_cause="Bad image",
                reasoning="Image is bad",
                confidence_score=0.9,
                corrected_yaml="apiVersion: v1",
                changes_made=["Changed image"]
            )
            
            tracker.log_llm_result(1, diagnosis, 2.5)
            
            self.mock_mlflow.log_metric.assert_any_call("llm_confidence_score", 0.9, step=1)
            self.mock_mlflow.log_text.assert_any_call("Bad image", "iteration_1/root_cause.txt")

    def test_log_debug_bundle_redacts_secrets(self):
        with patch.dict('sys.modules', {'mlflow': self.mock_mlflow}):
            self.settings.mlflow_log_prompts = True
            tracker = MLflowTracker(self.settings)
            
            bundle = DebugBundle(
                namespace="default",
                pod_debug_infos=[],
                namespace_events="",
                summary="Found a secret: mysecretpassword in logs"
            )
            
            tracker.log_debug_bundle(1, bundle)
            
            self.mock_mlflow.log_metric.assert_any_call("debug_pod_count", 0, step=1)
            
            # Check if text was logged with redaction
            call_args = self.mock_mlflow.log_text.call_args
            self.assertIsNotNone(call_args)
            logged_text = call_args[0][0]
            self.assertIn("secret: <REDACTED>", logged_text)
            self.assertNotIn("mysecretpassword", logged_text)

    def test_tracker_catches_and_suppresses_mlflow_exceptions(self):
        with patch.dict('sys.modules', {'mlflow': self.mock_mlflow}):
            tracker = MLflowTracker(self.settings)
            
            self.mock_mlflow.log_metric.side_effect = Exception("MLflow is down")
            
            # Should not raise exception
            try:
                tracker.log_final_result(True, 5)
            except Exception as e:
                self.fail(f"Tracker raised exception: {e}")

if __name__ == "__main__":
    unittest.main()
