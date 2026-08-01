from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from materials_studio_mcp.api_contract import RecordedExecutionError, error_result, invoke_with_contract, validate_operation_contract
from materials_studio_mcp.operations import begin_operation, finish_operation, run_idempotent
from materials_studio_mcp.project_manager import initialize_project
from materials_studio_mcp import server
from materials_studio_mcp.version_source import load_release_manifest


WORKSPACE_ROOT = Path(r"D:\分子动力学模拟")


def valid_contract() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "tool": "md_project_validate",
        "risk": "R0",
        "project_directory": str(WORKSPACE_ROOT / "projects" / "demo"),
        "inputs": [{"role": "manifest", "path": "manifest.json", "sha256": "a" * 64}],
        "output_slot": "reports/validation",
        "budget": {"wall_seconds": 30, "max_output_bytes": 100000},
    }


class ApiContractAndOperationTests(unittest.TestCase):
    def test_recorded_execution_error_returns_preserved_evidence_data(self) -> None:
        result = error_result(
            "ms_forcite_calculation_checked",
            RecordedExecutionError("typing failed", {"evidence_directory": "reports/failed"}),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "runtime_error")
        self.assertEqual(result["data"]["evidence_directory"], "reports/failed")
        for key in ("status", "artifact_ids", "evidence_ids", "blockers", "next_actions"):
            self.assertIn(key, result)

    def test_contract_validation_and_versioned_result_envelope(self) -> None:
        contract = valid_contract()
        self.assertEqual(validate_operation_contract(contract)["tool"], "md_project_validate")
        result = invoke_with_contract(contract, lambda: {"status": "valid"}, operation_id="op-1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["schema_version"], "1.0")
        self.assertEqual(result["operation_id"], "op-1")
        self.assertEqual(result["data"]["status"], "valid")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["artifact_ids"], [])
        self.assertEqual(result["evidence_ids"], [])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["next_actions"], [])

    def test_registered_public_tool_is_normalized_at_mcp_boundary(self) -> None:
        registered = server.mcp._tool_manager.get_tool("md_pipeline_get_config")
        result = registered.fn()
        self.assertEqual(result["tool"], "md_pipeline_get_config")
        for key in ("status", "artifact_ids", "evidence_ids", "blockers", "next_actions"):
            self.assertIn(key, result)

    def test_runtime_version_is_read_from_authoritative_release_manifest(self) -> None:
        self.assertEqual(__import__("materials_studio_mcp").__version__, load_release_manifest()["release"]["version"])

    def test_mcp_protocol_identity_uses_server_release_version(self) -> None:
        self.assertEqual(
            server.mcp._mcp_server.version,
            __import__("materials_studio_mcp").__version__,
        )

    def test_public_confirmation_is_enabled_for_castep_runtime_preflight(self) -> None:
        parameters = {
            "package_directory": r"E:\ms_mcp\ms_mcp_jobs\example",
            "package_manifest_sha256": "a" * 64,
            "task_name": "example_s0_12c",
            "timeout_seconds": 120,
        }
        confirmation = server.md_prepare_production_confirmation(
            "ms_castep_preflight_checked", parameters
        )
        self.assertEqual(confirmation["tool_name"], "ms_castep_preflight_checked")
        self.assertTrue(confirmation["confirmation_token"])

    def test_registered_confirmation_boundary_returns_a_consumable_single_use_token(self) -> None:
        parameters = {
            "package_directory": r"E:\ms_mcp\ms_mcp_jobs\boundary_example",
            "package_manifest_sha256": "b" * 64,
            "task_name": "boundary_s0_12c",
            "timeout_seconds": 120,
        }
        registered = server.mcp._tool_manager.get_tool(
            "md_prepare_production_confirmation"
        )
        result = registered.fn(
            "ms_castep_preflight_checked", parameters, 300
        )
        token = result["confirmation_token"]
        self.assertNotEqual(token, "[REDACTED]")
        consumed = server.confirmation_manager.consume(
            token, "ms_castep_preflight_checked", parameters
        )
        self.assertTrue(consumed["confirmed"])

    def test_contract_errors_are_structured_and_redacted(self) -> None:
        contract = valid_contract()
        contract["tool"] = "ms_run_materialsscript"
        result = invoke_with_contract(contract, lambda: None, operation_id="op-2")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertFalse(result["error"]["retryable"])
        self.assertNotIn("traceback", json.dumps(result).lower())

    def test_risk_contract_requirements_fail_closed(self) -> None:
        contract = valid_contract()
        contract.update({"tool": "ms_forcite_calculation_checked", "risk": "R3"})
        with self.assertRaisesRegex(ValueError, "confirmation_token"):
            validate_operation_contract(contract)
        contract.update({"confirmation_token": "x" * 24, "gate_evidence_ids": []})
        with self.assertRaisesRegex(ValueError, "R3 budget"):
            validate_operation_contract(contract)

    def test_durable_idempotency_replays_result_and_rejects_key_rebinding(self) -> None:
        with tempfile.TemporaryDirectory(prefix="operations_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project = initialize_project("operations", "Operations", projects_root=temporary)["project_directory"]
            calls = 0

            def implementation() -> dict[str, int]:
                nonlocal calls
                calls += 1
                return {"value": calls}

            first, replayed = run_idempotent(project, "request-1", "md_project_validate", {"x": 1}, implementation)
            self.assertFalse(replayed)
            second, replayed = run_idempotent(project, "request-1", "md_project_validate", {"x": 1}, implementation)
            self.assertTrue(replayed)
            self.assertEqual(first, second)
            self.assertEqual(calls, 1)
            with self.assertRaisesRegex(ValueError, "different operation"):
                begin_operation(project, "request-1", "md_project_validate", {"x": 2})
            records = list((Path(project) / ".operations").glob("*.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0].read_text(encoding="utf-8"))["state"], "succeeded")

    def test_operation_completion_requires_owner_token(self) -> None:
        with tempfile.TemporaryDirectory(prefix="operation_owner_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project = initialize_project("owner", "Owner", projects_root=temporary)["project_directory"]
            begun = begin_operation(project, "request-owner", "md_project_validate", {})
            with self.assertRaisesRegex(PermissionError, "ownership"):
                finish_operation(project, "request-owner", "wrong", result={"ok": True})
            finished = finish_operation(
                project, "request-owner", begun["operation_token"], result={"ok": True}
            )
            self.assertEqual(finished["state"], "succeeded")

    def test_idempotency_key_cannot_escape_operation_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="operation_key_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project = initialize_project("key", "Key", projects_root=temporary)["project_directory"]
            with self.assertRaisesRegex(ValueError, "idempotency_key"):
                begin_operation(project, "../escape", "md_project_validate", {})

    def test_public_transition_uses_versioned_success_and_error_envelopes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="transition_envelope_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project = initialize_project("transition-envelope", "Transition", projects_root=temporary)["project_directory"]
            success = server.md_project_transition(project, "specified", "reviewed")
            self.assertTrue(success["ok"])
            self.assertEqual(success["schema_version"], "1.0")
            failure = server.md_project_transition(project, "production", "skip stages")
            self.assertFalse(failure["ok"])
            self.assertEqual(failure["error"]["code"], "invalid_request")

    def test_public_artifact_registration_replays_by_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory(prefix="artifact_idempotency_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project = Path(initialize_project("artifact-replay", "Artifact", projects_root=temporary)["project_directory"])
            artifact = project / "request" / "input.txt"
            artifact.write_text("same bytes", encoding="utf-8")
            first = server.md_project_register_artifact(
                str(project), str(artifact), "input", idempotency_key="artifact-request-1",
                dry_run=False,
            )
            second = server.md_project_register_artifact(
                str(project), str(artifact), "input", idempotency_key="artifact-request-1",
                dry_run=False,
            )
            self.assertFalse(first["replayed"])
            self.assertTrue(second["replayed"])
            manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["artifacts"]), 1)


if __name__ == "__main__":
    unittest.main()
