from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import patch

from materials_studio_mcp import server


SERVER_PATH = Path(server.__file__).resolve()


def public_tool_names() -> set[str]:
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8-sig"))
    result: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
            ):
                result.add(node.name)
    return result


class ServerSecurityTests(unittest.TestCase):
    def test_arbitrary_materialsscript_is_not_public(self) -> None:
        self.assertNotIn("ms_run_materialsscript", public_tool_names())
        self.assertNotIn(
            "ms_run_materialsscript",
            {entry["tool"] for entry in server.ms_task_catalog()["workflows"]},
        )

    def test_natural_language_request_is_plan_only(self) -> None:
        fake = {
            "recommended_workflows": [
                {"id": "energy", "tool": "ms_forcite_energy", "reason": "test"}
            ]
        }
        with patch.object(server, "_recommend_workflows", return_value=fake), patch.object(
            server, "ms_forcite_energy", side_effect=AssertionError("executor must not be called")
        ):
            result = server.ms_execute_task_request("calculate energy", input_structure="outside.xsd")
        self.assertTrue(result["success"])
        self.assertFalse(result["executed"])
        self.assertEqual(result["mode"], "plan_only")
        self.assertEqual(result["selected_tool"], "ms_forcite_energy")

    def test_open_forcite_module_settings_are_rejected_before_execution(self) -> None:
        tools = (
            (server.ms_forcite_energy, {"input_structure": "x.xsd"}),
            (
                server.ms_forcite_geometry_optimization,
                {"input_structure": "x.xsd", "output_structure_path": "out.xsd"},
            ),
            (
                server.ms_forcite_dynamics,
                {"input_structure": "x.xsd", "output_trajectory_path": "out.xtd"},
            ),
        )
        with patch.object(server, "_run_forcite_task", side_effect=AssertionError("must not execute")):
            for tool, kwargs in tools:
                with self.subTest(tool=tool.__name__), self.assertRaisesRegex(ValueError, "disabled"):
                    tool(module_settings={"NumberOfSteps": 999999999}, **kwargs)

    def test_pipeline_config_is_redacted(self) -> None:
        secret_path = r"C:\Users\secret\token\program.exe"
        fake_config = {
            "software": {
                "materials_studio": {"root": secret_path, "run_mat_script": secret_path},
                "lammps": {"executable": secret_path, "msi2lmp": secret_path},
                "mpi": {"executable": secret_path},
                "vmd": {"executable": secret_path},
                "packmol": {"executable": None},
            },
            "policy": {
                "schema_version": 1,
                "workspace_roots": [secret_path],
                "scratch_root": secret_path,
                "limits": {"max_parallel_jobs": 1},
                "execution": {"overwrite_existing_outputs": False},
                "preflight": {"reject_lost_atoms": True},
            },
            "paths": {"software": secret_path, "policy": secret_path},
        }
        with patch.object(server, "load_pipeline_config", return_value=fake_config):
            result = server.md_pipeline_get_config()
        rendered = repr(result)
        self.assertTrue(result["redacted"])
        self.assertNotIn(secret_path, rendered)
        self.assertNotIn("workspace_roots", result)
        self.assertFalse(result["components"]["packmol"]["configured"])


if __name__ == "__main__":
    unittest.main()
