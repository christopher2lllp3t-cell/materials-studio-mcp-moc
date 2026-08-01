from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def release_manifest_path() -> Path:
    """Locate the authoritative release manifest without embedding a version."""
    candidates: list[Path] = []
    override = os.environ.get("MS_MOC_MCP_ROOT")
    if override:
        candidates.append(Path(override).expanduser().resolve() / "release-manifest.json")
    here = Path(__file__).resolve()
    candidates.extend(parent / "release-manifest.json" for parent in here.parents)
    cwd = Path.cwd().resolve()
    candidates.extend(parent / "release-manifest.json" for parent in (cwd, *cwd.parents))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("Authoritative release-manifest.json could not be located")


def load_release_manifest() -> dict[str, Any]:
    path = release_manifest_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read authoritative release manifest: {path}") from exc
    release = data.get("release")
    if not isinstance(release, dict) or not isinstance(release.get("version"), str):
        raise RuntimeError(f"Authoritative release manifest has no release.version: {path}")
    return data


def release_identity() -> dict[str, str]:
    path = release_manifest_path()
    data = load_release_manifest()
    return {
        "version": str(data["release"]["version"]),
        "api_version": str(data["release"].get("api_version", "")),
        "manifest_path": str(path),
    }


__all__ = ["load_release_manifest", "release_identity", "release_manifest_path"]
