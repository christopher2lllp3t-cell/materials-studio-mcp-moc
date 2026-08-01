from __future__ import annotations

import argparse
from datetime import timedelta
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
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "materials_studio_mcp.server"],
        cwd=str(root),
        env=dict(os.environ),
    )
    idempotency_key = f"{args.output_slot}-surface-enumeration-v1"
    parameters = {
        "project_directory": args.project_directory,
        "input_bulk_structure": args.input_bulk_structure,
        "input_sha256": args.input_sha256,
        "miller_h": args.miller_h,
        "miller_k": args.miller_k,
        "miller_l": args.miller_l,
        "thickness_angstrom": args.thickness_angstrom,
        "top_positions": args.top_positions,
        "output_slot": args.output_slot,
        "max_candidates": args.max_candidates,
        "idempotency_key": idempotency_key,
        "timeout_seconds": args.timeout_seconds,
    }
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            confirmation = _payload(
                await session.call_tool(
                    "md_prepare_production_confirmation",
                    {
                        "tool_name": "ms_geology_enumerate_surface_terminations",
                        "parameters": parameters,
                        "ttl_seconds": 300,
                    },
                    read_timeout_seconds=timedelta(seconds=30),
                )
            )
            result = _payload(
                await session.call_tool(
                    "ms_geology_enumerate_surface_terminations",
                    {**parameters, "confirmation_token": confirmation["confirmation_token"]},
                    read_timeout_seconds=timedelta(seconds=args.timeout_seconds + 30),
                )
            )
    return {"server_tool_count": len(tools.tools), "result": result}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a real stdio MCP surface-enumeration smoke test.")
    parser.add_argument("--project-directory", required=True)
    parser.add_argument("--input-bulk-structure", required=True)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--miller-h", type=int, required=True)
    parser.add_argument("--miller-k", type=int, required=True)
    parser.add_argument("--miller-l", type=int, required=True)
    parser.add_argument("--thickness-angstrom", type=float, required=True)
    parser.add_argument("--top-positions", type=float, nargs="+", required=True)
    parser.add_argument("--output-slot", required=True)
    parser.add_argument("--max-candidates", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    result = anyio.run(run, args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["result"].get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
