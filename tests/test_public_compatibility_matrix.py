from __future__ import annotations

import asyncio
import hashlib
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import materials_studio_mcp
from materials_studio_mcp import server
from materials_studio_mcp.pipeline_config import load_pipeline_config
from materials_studio_mcp.public_registry import PUBLIC_TOOLS
from materials_studio_mcp.version_source import release_identity


class PublicCompatibilityMatrixTests(unittest.TestCase):
    def test_registry_schema_and_python_signatures_match(self) -> None:
        listed = asyncio.run(server.mcp.list_tools())
        schemas = {tool.name: tool.inputSchema for tool in listed}
        registry = {item.name: item.risk for item in PUBLIC_TOOLS}
        self.assertEqual(set(schemas), set(registry))
        for name in registry:
            signature_names = set(inspect.signature(getattr(server, name)).parameters)
            schema_names = set((schemas[name] or {}).get("properties", {}))
            with self.subTest(tool=name):
                self.assertEqual(schema_names, signature_names)

    def test_every_mutating_public_tool_is_dry_run_by_default(self) -> None:
        for item in PUBLIC_TOOLS:
            if item.risk == "R0" or item.name == "md_prepare_production_confirmation":
                continue
            signature = inspect.signature(getattr(server, item.name))
            with self.subTest(tool=item.name, risk=item.risk):
                self.assertIn("dry_run", signature.parameters)
                self.assertIs(signature.parameters["dry_run"].default, True)

    def test_every_confirmation_target_has_a_consumable_public_token(self) -> None:
        issuer = server.mcp._tool_manager.get_tool(
            "md_prepare_production_confirmation"
        )
        for item in PUBLIC_TOOLS:
            signature = inspect.signature(getattr(server, item.name))
            if "confirmation_token" not in signature.parameters:
                continue
            parameters = {"compatibility_probe": item.name}
            with self.subTest(tool=item.name):
                issued = issuer.fn(item.name, parameters, 60)
                token = issued.get("confirmation_token")
                self.assertIsInstance(token, str)
                self.assertNotEqual(token, "[REDACTED]")
                consumed = server.confirmation_manager.consume(
                    token, item.name, parameters
                )
                self.assertTrue(consumed["confirmed"])

    def test_local_engine_profiles_are_explicit_and_version_consistent(self) -> None:
        config = load_pipeline_config()
        prepare_cores = inspect.signature(
            server.ms_prepare_castep_pl_package
        ).parameters["cores"].default
        readiness_cores = inspect.signature(
            server.ms_castep_gateway_readiness
        ).parameters["requested_cores"].default
        self.assertEqual(prepare_cores, 4)
        self.assertEqual(readiness_cores, 12)
        self.assertEqual(config["policy"]["limits"]["max_mpi_processes"], 8)
        identity = release_identity()["version"]
        self.assertEqual(materials_studio_mcp.__version__, identity)
        self.assertEqual(server.mcp._mcp_server.version, identity)

    def test_analysis_target_listing_is_hash_bound_and_dry_run_by_default(self) -> None:
        workspace_tmp = Path(r"D:\分子动力学模拟\tmp")
        with tempfile.TemporaryDirectory(dir=workspace_tmp) as temporary:
            source = Path(temporary) / "analysis_input.xsd"
            source.write_text(
                "<XSD><Atom3d ID='1'/></XSD>" + " " * 300,
                encoding="utf-8",
            )
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with patch.object(server, "_run_materialsscript_job") as execute:
                result = server.ms_list_analysis_targets(str(source), digest)
            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["status"], "dry_run")
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
