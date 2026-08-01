from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


MOC_SCRIPT_ENV = "MS_MOC_SCRIPT"
MOC_MCP_ROOT_ENV = "MS_MOC_MCP_ROOT"
LEGACY_MOC_SCRIPT = Path(r"D:\分子动力学模拟\tools\ms_moc.py")


def discover_moc_script(module_file: Path | None = None) -> Path:
    override = os.environ.get(MOC_SCRIPT_ENV)
    if override:
        return Path(override).expanduser().resolve()
    origin = (module_file or Path(__file__)).resolve()
    for ancestor in origin.parents:
        candidate = ancestor / "moc" / "ms_moc.py"
        if (ancestor / "release-bundle.json").is_file() and candidate.is_file():
            return candidate.resolve()
    return LEGACY_MOC_SCRIPT.resolve()


MOC_SCRIPT = discover_moc_script()
MOC_ROOT = MOC_SCRIPT.parents[1]
MOC_DOCUMENT_SUFFIXES = frozenset({".xsd", ".xtd", ".stp", ".car", ".mdf", ".cif"})


def _moc_environment() -> dict[str, str]:
    environment = os.environ.copy()
    deployment_root = MOC_SCRIPT.parents[1]
    if (deployment_root / "release-bundle.json").is_file():
        environment[MOC_MCP_ROOT_ENV] = str(deployment_root)
    return environment


def _run_moc(arguments: list[str], timeout_seconds: int = 60) -> dict[str, Any]:
    if not MOC_SCRIPT.is_file():
        raise FileNotFoundError(f"MOC control script is missing: {MOC_SCRIPT}")
    try:
        result = subprocess.run(
            [sys.executable, str(MOC_SCRIPT), *arguments],
            cwd=str(MOC_ROOT),
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
            encoding="utf-8",
            errors="replace",
            close_fds=True,
            env=_moc_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"MOC command timed out after {timeout_seconds} seconds") from exc
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MOC returned invalid JSON: {result.stdout[:500]}") from exc
    if result.returncode != 0:
        raise RuntimeError(json.dumps(data, ensure_ascii=False))
    if result.stderr:
        data["stderr_tail"] = result.stderr[-2000:]
    return data


def get_moc_status() -> dict[str, Any]:
    return _run_moc(["status", "--json"], timeout_seconds=30)


def launch_document(document_path: Path, *, dry_run: bool) -> dict[str, Any]:
    arguments = ["launch", str(document_path), "--json"]
    if dry_run:
        arguments.append("--dry-run")
    return _run_moc(arguments, timeout_seconds=60)
