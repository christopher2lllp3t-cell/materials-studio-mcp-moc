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


async def run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    source = Path(args.input_surface_structure)
    parameters = {
        "project_directory": args.project_directory,
        "input_surface_structure": str(source),
        "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest().upper(),
        "sites": [{
            "oxygen_atom_index": args.oxygen_atom_index,
            "oxygen_atom_name": args.oxygen_atom_name,
            "expected_oxygen_fractional_xyz": args.oxygen_xyz,
            "oxygen_from_formal_charge_e": args.oxygen_from_formal_charge,
            "oxygen_to_formal_charge_e": args.oxygen_to_formal_charge,
            "hydrogen_name": args.hydrogen_name,
            "hydrogen_fractional_xyz": args.hydrogen_xyz,
            "hydrogen_formal_charge_e": args.hydrogen_formal_charge,
            "surface_side": args.surface_side,
        }],
        "output_slot": args.output_slot,
        "min_oh_bond_length_angstrom": args.min_oh_bond_length,
        "max_oh_bond_length_angstrom": args.max_oh_bond_length,
        "min_nonbonded_distance_angstrom": args.min_nonbonded_distance,
        "max_atoms": args.max_atoms,
        "idempotency_key": f"{args.output_slot}-hydroxylation-v1",
        "timeout_seconds": args.timeout_seconds,
    }
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "materials_studio_mcp.server"],
        cwd=str(root),
        env=dict(os.environ),
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            confirmation = _payload(
                await session.call_tool(
                    "md_prepare_production_confirmation",
                    {
                        "tool_name": "ms_geology_apply_hydroxylation_ledger",
                        "parameters": parameters,
                        "ttl_seconds": 300,
                    },
                    read_timeout_seconds=timedelta(seconds=30),
                )
            )
            result = _payload(
                await session.call_tool(
                    "ms_geology_apply_hydroxylation_ledger",
                    {**parameters, "confirmation_token": confirmation["confirmation_token"]},
                    read_timeout_seconds=timedelta(seconds=args.timeout_seconds + 30),
                )
            )
    return {"server_tool_count": len(tools.tools), "result": result}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run a real stdio MCP surface-hydroxylation smoke test.")
    parser.add_argument("--project-directory", required=True)
    parser.add_argument("--input-surface-structure", required=True)
    parser.add_argument("--oxygen-atom-index", type=int, required=True)
    parser.add_argument("--oxygen-atom-name", required=True)
    parser.add_argument("--oxygen-xyz", type=float, nargs=3, required=True)
    parser.add_argument("--oxygen-from-formal-charge", type=float, default=0.0)
    parser.add_argument("--oxygen-to-formal-charge", type=float, default=0.0)
    parser.add_argument("--hydrogen-name", required=True)
    parser.add_argument("--hydrogen-xyz", type=float, nargs=3, required=True)
    parser.add_argument("--hydrogen-formal-charge", type=float, default=0.0)
    parser.add_argument("--surface-side", choices=("top", "bottom"), required=True)
    parser.add_argument("--output-slot", required=True)
    parser.add_argument("--min-oh-bond-length", type=float, default=0.8)
    parser.add_argument("--max-oh-bond-length", type=float, default=1.2)
    parser.add_argument("--min-nonbonded-distance", type=float, default=0.7)
    parser.add_argument("--max-atoms", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    result = anyio.run(run, args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["result"].get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
