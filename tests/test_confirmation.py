from __future__ import annotations

import unittest
from unittest.mock import patch

from materials_studio_mcp.confirmation import ConfirmationManager
from materials_studio_mcp import server


class MutableClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class ConfirmationTests(unittest.TestCase):
    def test_token_is_single_use_and_parameter_bound(self) -> None:
        manager = ConfirmationManager(secret=b"x" * 32)
        params = {"steps": 1000, "temperature": 300.0}
        issued = manager.issue("tool_a", params, 60)
        result = manager.consume(issued["confirmation_token"], "tool_a", params)
        self.assertTrue(result["confirmed"])
        with self.assertRaisesRegex(PermissionError, "already used"):
            manager.consume(issued["confirmation_token"], "tool_a", params)

    def test_tool_and_parameters_must_match(self) -> None:
        manager = ConfirmationManager(secret=b"x" * 32)
        first = manager.issue("tool_a", {"steps": 10}, 60)
        with self.assertRaisesRegex(PermissionError, "different tool"):
            manager.consume(first["confirmation_token"], "tool_b", {"steps": 10})
        second = manager.issue("tool_a", {"steps": 10}, 60)
        with self.assertRaisesRegex(PermissionError, "parameters do not match"):
            manager.consume(second["confirmation_token"], "tool_a", {"steps": 11})

    def test_expired_and_tampered_tokens_are_rejected(self) -> None:
        clock = MutableClock()
        manager = ConfirmationManager(clock=clock, secret=b"x" * 32)
        issued = manager.issue("tool_a", {}, 2)
        clock.value += 2
        with self.assertRaisesRegex(PermissionError, "expired"):
            manager.consume(issued["confirmation_token"], "tool_a", {})
        tampered = issued["confirmation_token"][:-1] + ("0" if issued["confirmation_token"][-1] != "0" else "1")
        with self.assertRaisesRegex(PermissionError, "Invalid"):
            manager.consume(tampered, "tool_a", {})

    def test_production_dynamics_requires_exact_confirmation(self) -> None:
        manager = ConfirmationManager(secret=b"y" * 32)
        parameters = {
            "input_structure": "input.xsd",
            "output_trajectory_path": "output.xtd",
            "output_structure_path": None,
            "module_settings": None,
            "report_properties": None,
            "job_name": "forcite_dynamics",
            "timeout_seconds": 1200,
            "keep_job_dir": True,
            "production": True,
        }
        with patch.object(server, "confirmation_manager", manager), patch.object(
            server, "_run_forcite_task", return_value={"success": True}
        ) as executor:
            with self.assertRaisesRegex(PermissionError, "required"):
                server.ms_forcite_dynamics("input.xsd", "output.xtd", production=True)
            token = manager.issue("ms_forcite_dynamics", parameters)["confirmation_token"]
            result = server.ms_forcite_dynamics(
                "input.xsd", "output.xtd", production=True, confirmation_token=token
            )
        self.assertTrue(result["success"])
        executor.assert_called_once()

    def test_confirmation_issuer_has_a_strict_tool_allowlist(self) -> None:
        with self.assertRaisesRegex(ValueError, "not enabled"):
            server.md_prepare_production_confirmation("ms_run_materialsscript", {}, 60)


if __name__ == "__main__":
    unittest.main()
