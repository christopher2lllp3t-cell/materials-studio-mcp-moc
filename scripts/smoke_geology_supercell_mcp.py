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
    project_directory = str(Path(args.projects_root) / args.project_id)
    idempotency_key = f"{args.project_id}-{args.output_slot}-supercell-v1"
    parameters = {
        "project_directory": project_directory,
        "input_structure": args.input_structure,
        "input_sha256": args.input_sha256,
        "repeat_a": args.repeat_a,
        "repeat_b": args.repeat_b,
        "repeat_c": args.repeat_c,
        "output_slot": args.output_slot,
        "max_atoms": args.max_atoms,
        "idempotency_key": idempotency_key,
        "timeout_seconds": args.timeout_seconds,
    }
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            created = _payload(
                await session.call_tool(
                    "md_project_initialize",
                    {
                        "project_id": args.project_id,
                        "projects_root": args.projects_root,
                        "title": args.title,
                    },
                    read_timeout_seconds=timedelta(seconds=30),
                )
            )
            confirmation = _payload(
                await session.call_tool(
                    "md_prepare_production_confirmation",
                    {"tool_name": "ms_geology_build_supercell", "parameters": parameters, "ttl_seconds": 300},
                    read_timeout_seconds=timedelta(seconds=30),
                )
            )
            result = _payload(
                await session.call_tool(
                    "ms_geology_build_supercell",
                    {**parameters, "confirmation_token": confirmation["confirmation_token"]},
                    read_timeout_seconds=timedelta(seconds=args.timeout_seconds + 30),
                )
            )
    return {
        "server_tool_count": len(tools.tools),
        "project": created,
        "result": result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a real stdio MCP geology-supercell smoke test.")
    parser.add_argument("--projects-root", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--title", default="Geology supercell MCP smoke")
    parser.add_argument("--input-structure", required=True)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--output-slot", required=True)
    parser.add_argument("--repeat-a", type=int, default=2)
    parser.add_argument("--repeat-b", type=int, default=1)
    parser.add_argument("--repeat-c", type=int, default=1)
    parser.add_argument("--max-atoms", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    result = anyio.run(run, args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["result"].get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
