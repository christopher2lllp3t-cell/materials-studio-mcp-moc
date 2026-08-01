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


BRIDGE_DIRECTORY = Path(__file__).resolve().parent
SOURCE_MCP_ROOT = Path(r"E:\ms_mcp\ms_mcp_runtime\materials_studio_2023")
_mcp_override = os.environ.get("MS_MOC_MCP_ROOT")
_deployment_candidate = BRIDGE_DIRECTORY.parent
MCP_ROOT = (
    Path(_mcp_override).expanduser().resolve()
    if _mcp_override
    else _deployment_candidate
    if (_deployment_candidate / "release-bundle.json").is_file() and (_deployment_candidate / ".venv" / "Scripts" / "python.exe").is_file()
    else SOURCE_MCP_ROOT
)
REQUIRED_BRIDGE_TOOLS = (
    "md_pipeline_health_check",
    "ms_geology_assess_nanopore_contract",
    "ms_moc_get_status",
    "ms_moc_open_document",
)


def payload(result: Any) -> dict[str, Any]:
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


def status_payload(names: list[str], health: dict[str, Any]) -> dict[str, Any]:
    required = {name: name in names for name in REQUIRED_BRIDGE_TOOLS}
    pipeline_ready = (
        health.get("status") == "ready"
        and health.get("ready_for_ms_lammps_vmd") is True
    )
    return {
        "schema_version": 1,
        "status": "ready" if pipeline_ready and all(required.values()) else "degraded",
        "tool_count": len(names),
        "required_bridge_tools": required,
        "pipeline_ready": pipeline_ready,
        "pipeline_health": health,
    }


def require_successful_tool_result(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok") is not True:
        error = result.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        raise RuntimeError(f"{tool_name} failed: {message or 'unknown MCP error'}")
    return result


def require_valid_nanopore_assessment(result: dict[str, Any]) -> dict[str, Any]:
    require_successful_tool_result("ms_geology_assess_nanopore_contract", result)
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Nanopore assessment did not return a data object")
    errors = data.get("errors")
    if data.get("status") == "fail" or (isinstance(errors, list) and errors):
        detail = "; ".join(str(item) for item in errors[:3]) if isinstance(errors, list) else ""
        raise ValueError(f"Nanopore contract is invalid: {detail or 'validation failed'}")
    return result


def root_exception(exc: BaseException) -> BaseException:
    current = exc
    while isinstance(current, BaseExceptionGroup) and current.exceptions:
        current = current.exceptions[0]
    return current


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    server_env = dict(os.environ)
    server_env["MATERIALS_STUDIO_MCP_ROOT"] = str(MCP_ROOT)
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "materials_studio_mcp.server"],
        cwd=str(MCP_ROOT),
        env=server_env,
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            if args.command == "list-tools":
                return {"schema_version": 1, "status": "ready", "tool_count": len(names), "tools": names}
            if args.command == "status":
                health = payload(
                    await session.call_tool(
                        "md_pipeline_health_check",
                        {"run_version_probes": args.run_version_probes},
                        read_timeout_seconds=timedelta(seconds=120),
                    )
                )
                return status_payload(names, health)
            if args.command == "assess-nanopore":
                result = payload(
                    await session.call_tool(
                        "ms_geology_assess_nanopore_contract",
                        {"contract_path": str(Path(args.contract).resolve())},
                        read_timeout_seconds=timedelta(seconds=120),
                    )
                )
                require_valid_nanopore_assessment(result)
                return {"schema_version": 1, "status": "completed", "tool_count": len(names), "result": result}
    raise RuntimeError(f"Unsupported bridge command: {args.command}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Fresh stdio bridge from the MS MOC CLI to the Materials Studio MCP server.")
    sub = parser.add_subparsers(dest="command", required=True)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--run-version-probes", action="store_true")
    sub.add_parser("list-tools")
    assess = sub.add_parser("assess-nanopore")
    assess.add_argument("contract")
    args = parser.parse_args()
    try:
        result = anyio.run(execute, args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        root = root_exception(exc)
        print(json.dumps({
            "schema_version": 1,
            "status": "error",
            "error": {"type": type(root).__name__, "message": str(root)},
        }, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
