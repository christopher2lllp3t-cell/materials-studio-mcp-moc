from __future__ import annotations

import argparse
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if isinstance(text, str):
            value = json.loads(text)
            if isinstance(value, dict):
                return value
    raise RuntimeError("MCP result did not contain a JSON object")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


async def _confirmed_call(
    session: ClientSession, tool_name: str, parameters: dict[str, Any], timeout_seconds: int
) -> dict[str, Any]:
    confirmation = _payload(
        await session.call_tool(
            "md_prepare_production_confirmation",
            {"tool_name": tool_name, "parameters": parameters, "ttl_seconds": 300},
            read_timeout_seconds=timedelta(seconds=30),
        )
    )
    return _payload(
        await session.call_tool(
            tool_name,
            {**parameters, "confirmation_token": confirmation["confirmation_token"]},
            read_timeout_seconds=timedelta(seconds=timeout_seconds + 30),
        )
    )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "materials_studio_mcp.server"],
        cwd=str(root),
        env=dict(os.environ),
    )
    project_directory = Path(args.projects_root) / args.project_id
    source = Path(args.input_structure)
    substitution_parameters = {
        "project_directory": str(project_directory),
        "input_structure": str(source),
        "input_sha256": _sha256(source),
        "substitutions": [{
            "atom_index": 0,
            "atom_name": "NA1",
            "expected_fractional_xyz": [0.0, 0.0, 0.0],
            "from_element": "Na",
            "to_element": "K",
            "from_formal_charge_e": 1.0,
            "to_formal_charge_e": 1.0,
        }],
        "output_slot": "nacl_k_substitution_smoke",
        "idempotency_key": f"{args.project_id}-substitution-v1",
        "timeout_seconds": args.timeout_seconds,
    }
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            created = _payload(
                await session.call_tool(
                    "md_project_initialize",
                    {"project_id": args.project_id, "projects_root": args.projects_root, "title": "Geology mutations MCP smoke"},
                    read_timeout_seconds=timedelta(seconds=30),
                )
            )
            substitution = await _confirmed_call(
                session, "ms_geology_apply_substitutions", substitution_parameters, args.timeout_seconds
            )
            if not substitution.get("ok"):
                return {"server_tool_count": len(tools.tools), "project": created, "substitution": substitution}
            substituted = Path(substitution["data"]["output_path"])
            counterion_parameters = {
                "project_directory": str(project_directory),
                "input_structure": str(substituted),
                "input_sha256": _sha256(substituted),
                "placements": [{
                    "atom_name": "LI_SMOKE_1",
                    "element": "Li",
                    "formal_charge_e": 1.0,
                    "fractional_xyz": [0.125, 0.225, 0.225],
                }],
                "output_slot": "nacl_k_li_counterion_smoke",
                "min_framework_distance_angstrom": 2.0,
                "min_counterion_distance_angstrom": 2.0,
                "max_atoms": 32,
                "idempotency_key": f"{args.project_id}-counterion-v1",
                "timeout_seconds": args.timeout_seconds,
            }
            counterion = await _confirmed_call(
                session, "ms_geology_place_counterions", counterion_parameters, args.timeout_seconds
            )
    return {
        "server_tool_count": len(tools.tools),
        "project": created,
        "substitution": substitution,
        "counterion": counterion,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run real stdio MCP substitution and counterion smoke tests.")
    parser.add_argument("--projects-root", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--input-structure", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    result = anyio.run(run, args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("substitution", {}).get("ok") and result.get("counterion", {}).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
