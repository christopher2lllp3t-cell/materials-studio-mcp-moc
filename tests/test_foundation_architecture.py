from __future__ import annotations

import ast
import json
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from materials_studio_mcp import server
from materials_studio_mcp.project_manager import _read_manifest, initialize_project
from materials_studio_mcp.public_registry import INTERNAL_TOOL_PROFILES, PUBLIC_TOOLS, api_catalog, public_tool_names


SERVER_PATH = Path(server.__file__).resolve()
CONTRACT_PATH = Path(r"D:\分子动力学模拟\07_mcp_materials_studio\mcp_v1_tool_contract.schema.json")
WORKSPACE_ROOT = Path(r"D:\分子动力学模拟")


def _server_tree() -> ast.Module:
    return ast.parse(SERVER_PATH.read_text(encoding="utf-8-sig"))


def _public_tools() -> set[str]:
    names: set[str] = set()
    for node in _server_tree().body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
            ):
                names.add(node.name)
    return names


class FoundationArchitectureTests(unittest.TestCase):
    def test_public_registry_exactly_matches_mcp_registration(self) -> None:
        self.assertEqual(_public_tools(), set(public_tool_names()))
        catalog = api_catalog()
        self.assertEqual(catalog["api_version"], "1.0")
        self.assertEqual(catalog["result_schema_version"], "1.0")

    def test_internal_profiles_are_not_mcp_registered(self) -> None:
        hidden = {tool for tools in INTERNAL_TOOL_PROFILES.values() for tool in tools}
        self.assertEqual(_public_tools() & hidden, set())

    def test_deprecated_replacements_are_registered_public_tools(self) -> None:
        public = public_tool_names()
        dangling = {
            item.name: item.replacement
            for item in PUBLIC_TOOLS
            if item.lifecycle == "deprecated" and item.replacement and item.replacement not in public
        }
        self.assertEqual(dangling, {})

    def test_top_level_function_names_do_not_shadow_each_other(self) -> None:
        names = [
            node.name
            for node in _server_tree().body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
        self.assertEqual(duplicates, [], f"shadowed top-level functions: {duplicates}")

    def test_catalog_only_recommends_registered_public_tools(self) -> None:
        public = _public_tools()
        catalog = server.ms_task_catalog()["workflows"]
        names = [entry["tool"] for entry in catalog]
        self.assertEqual(len(names), len(set(names)), "workflow catalog contains duplicate tools")
        self.assertEqual(sorted(set(names) - public), [])

    def test_public_tool_names_match_contract_naming_rule(self) -> None:
        schema = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        pattern = re.compile(schema["properties"]["tool"]["pattern"])
        invalid = sorted(name for name in _public_tools() if pattern.fullmatch(name) is None)
        self.assertEqual(invalid, [])

    def test_manifest_schema_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="manifest_schema_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            project = Path(initialize_project("schema-test", "Schema", projects_root=temporary)["project_directory"])
            manifest_path = project / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 999
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unsupported project manifest schema_version"):
                _read_manifest(manifest_path)

    def test_new_manifest_does_not_persist_local_executable_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="manifest_provenance_", dir=WORKSPACE_ROOT / "tmp") as temporary:
            result = initialize_project("provenance-test", "Provenance", projects_root=temporary)
            rendered = json.dumps(result["manifest"]["provenance"]["software"], ensure_ascii=False)
            self.assertNotIn(":\\", rendered)
            self.assertNotIn("executable", rendered.lower())
            self.assertNotIn("run_mat_script", rendered.lower())


if __name__ == "__main__":
    unittest.main()
