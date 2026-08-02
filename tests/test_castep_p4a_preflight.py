from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from materials_studio_mcp import castep_p4a_preflight as p4a
from materials_studio_mcp.capability_registry import load_capability_registry
from materials_studio_mcp.public_registry import PUBLIC_TOOLS


class CastepP4APreflightTests(unittest.TestCase):
    def test_child_locale_policy_does_not_mutate_parent(self) -> None:
        parent = {"LC_ALL": "C.UTF-8", "LC_CTYPE": "C.UTF-8", "LANG": "C.UTF-8", "KEEP": "value"}
        before = dict(parent)
        child, receipt = p4a.build_materials_studio_perl_environment(parent)
        self.assertEqual(parent, before)
        self.assertIsNot(child, parent)
        self.assertEqual({key: child[key] for key in p4a._LOCALE_KEYS}, {
            "LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C",
        })
        self.assertEqual(child["KEEP"], "value")
        self.assertFalse(receipt["parent_environment_mutated"])
        self.assertFalse(receipt["windows_system_or_user_environment_mutated"])

    def test_missing_locale_values_are_added_only_to_child(self) -> None:
        parent = {"KEEP": "value"}
        child, receipt = p4a.build_materials_studio_perl_environment(parent)
        self.assertEqual(parent, {"KEEP": "value"})
        self.assertEqual(receipt["locale_before"], {
            "LC_ALL": None, "LC_CTYPE": None, "LANG": None,
        })
        self.assertTrue(all(child[key] == "C" for key in p4a._LOCALE_KEYS))

    def test_non_string_environment_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            p4a.build_materials_studio_perl_environment({"LANG": 936})  # type: ignore[dict-item]

    def test_real_ms_perl_locale_audit_is_clean_and_harmless(self) -> None:
        result = p4a.audit_materials_studio_perl_locale()
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["process"]["exit_code"], 0)
        self.assertFalse(result["process"]["shell"])
        self.assertFalse(result["castep_or_license_started"])
        self.assertEqual(result["stderr_bytes"], 0)
        self.assertEqual(result["locale_warning_markers"], [])
        self.assertEqual(result["environment_policy"]["locale_after"], {
            "LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C",
        })

    def test_perl_hash_drift_fails_before_subprocess(self) -> None:
        with patch.object(p4a, "_MATERIALS_STUDIO_PERL_SHA256", "0" * 64), patch.object(
            p4a.subprocess, "run"
        ) as run:
            with self.assertRaises(PermissionError):
                p4a.audit_materials_studio_perl_locale()
        run.assert_not_called()

    def test_fixed_profile_preflight_is_nonexecuting_and_not_public(self) -> None:
        result = p4a.build_fixed_profile_publication_preflight()
        self.assertEqual(result["status"], "blocked_pending_p4b_public_api_review")
        self.assertFalse(result["execution_allowed"])
        self.assertFalse(result["public_tool_added"])
        self.assertEqual(result["profile"]["cores"], 4)
        self.assertEqual(result["profile"]["timeout_seconds"], 600)
        self.assertEqual(result["locale_policy"]["values"], {
            "LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C",
        })

    def test_p4a_does_not_change_public_registry_or_general_capabilities(self) -> None:
        self.assertEqual(len(PUBLIC_TOOLS), 49)
        capabilities = {item["id"]: item for item in load_capability_registry()["capabilities"]}
        self.assertFalse(capabilities["castep.calculation"]["verified"])
        self.assertFalse(capabilities["results.castep_parsing"]["verified"])

    def test_parent_process_locale_is_unchanged_after_real_audit(self) -> None:
        before = {key: os.environ.get(key) for key in p4a._LOCALE_KEYS}
        p4a.audit_materials_studio_perl_locale()
        after = {key: os.environ.get(key) for key in p4a._LOCALE_KEYS}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
