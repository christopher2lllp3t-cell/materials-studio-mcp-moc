from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any, Iterable
import zipfile

from . import __version__
from .pipeline_config import discover_project_root, load_pipeline_config
from .public_registry import API_VERSION, PUBLIC_TOOLS


PROJECT_ROOT = (
    Path(os.environ["MS_MOC_MCP_ROOT"]).expanduser().resolve()
    if os.environ.get("MS_MOC_MCP_ROOT")
    else discover_project_root(__file__)
)
DEFAULT_MANIFEST = PROJECT_ROOT / "release-manifest.json"
WORKSPACE_ROOT = Path(r"D:\分子动力学模拟")
MOC_FILES = (
    WORKSPACE_ROOT / "tools" / "ms_moc.py",
    WORKSPACE_ROOT / "tools" / "ms_mcp_bridge.py",
    WORKSPACE_ROOT / "MS_MOC_INTERFACE.md",
    WORKSPACE_ROOT / "MS_MOC_STATUS.md",
    WORKSPACE_ROOT / "07_mcp_materials_studio" / "SCIENCE_ENVIRONMENT.md",
    WORKSPACE_ROOT / "07_mcp_materials_studio" / "science-requirements.lock",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _release_files() -> Iterable[tuple[str, Path]]:
    root_files = (
        "pyproject.toml",
        "requirements.lock",
        "install.ps1",
        "mcp-config.example.json",
        "README.md",
    )
    for relative in root_files:
        yield relative, PROJECT_ROOT / relative
    for directory in ("src", "config", "scripts", "tests"):
        root = PROJECT_ROOT / directory
        if not root.is_dir():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if (
                "__pycache__" in path.parts
                or any(part.endswith(".egg-info") for part in path.parts)
                or path.suffix.lower() in {".pyc", ".pyo"}
            ):
                continue
            yield path.relative_to(PROJECT_ROOT).as_posix(), path
    for path in MOC_FILES:
        yield f"workspace/{path.name}", path


def _runtime_binaries() -> list[dict[str, Any]]:
    config = load_pipeline_config()
    software = config["software"]
    candidates = {
        "RunMatScript": software["materials_studio"].get("run_mat_script"),
        "MatStudio": str(Path(software["materials_studio"]["root"]) / "bin" / "MatStudio.exe"),
        "LAMMPS": software["lammps"].get("executable"),
        "msi2lmp": software["lammps"].get("msi2lmp"),
        "VMD": software["vmd"].get("executable"),
        "Packmol": software["packmol"].get("executable"),
        "PowerShell": str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
    }
    result: list[dict[str, Any]] = []
    for name, value in candidates.items():
        path = Path(str(value)).resolve() if value else None
        exists = bool(path and path.is_file())
        result.append({
            "name": name,
            "path": str(path) if path else None,
            "exists": exists,
            "bytes": path.stat().st_size if exists else None,
            "sha256": sha256_file(path) if exists else None,
        })
    return result


def build_release_manifest() -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    missing: list[str] = []
    for label, path in _release_files():
        if not path.is_file():
            missing.append(label)
            continue
        files.append({
            "label": label,
            "path": str(path.resolve()),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    runtime = _runtime_binaries()
    return {
        "schema_version": 1,
        "release": {
            "name": "materials-studio-mcp-moc",
            "version": __version__,
            "api_version": API_VERSION,
            "channel": "v1-release-candidate",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "production_science_released": False,
        },
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "public_tools": [item.as_dict() for item in PUBLIC_TOOLS],
        "public_tool_count": len(PUBLIC_TOOLS),
        "files": files,
        "missing_files": missing,
        "runtime_binaries": runtime,
    }


def write_release_manifest(path: Path, *, force: bool = False) -> dict[str, Any]:
    destination = path.resolve()
    if destination.exists() and not force:
        raise FileExistsError(f"Release manifest already exists: {destination}")
    data = build_release_manifest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return {"status": "written", "path": str(destination), "sha256": sha256_file(destination), "manifest": data}


def verify_release_manifest(path: Path) -> dict[str, Any]:
    source = path.resolve(strict=True)
    data = json.loads(source.read_text(encoding="utf-8"))
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("Unsupported release manifest schema")
    release = data.get("release", {})
    if release.get("version") != __version__ or release.get("api_version") != API_VERSION:
        errors.append("Installed package/API version differs from release manifest")
    if data.get("public_tool_count") != len(PUBLIC_TOOLS):
        errors.append("Public tool count differs from release manifest")
    recorded_names = [item.get("name") for item in data.get("public_tools", [])]
    if recorded_names != [item.name for item in PUBLIC_TOOLS]:
        errors.append("Public tool registry differs from release manifest")
    file_results: list[dict[str, Any]] = []
    for item in data.get("files", []):
        file_path = Path(str(item.get("path", "")))
        exists = file_path.is_file()
        actual = sha256_file(file_path) if exists else None
        matches = exists and actual == item.get("sha256")
        file_results.append({"label": item.get("label"), "exists": exists, "hash_matches": matches})
        if not matches:
            errors.append(f"Release file missing or changed: {item.get('label')}")
    runtime_results: list[dict[str, Any]] = []
    for item in data.get("runtime_binaries", []):
        binary = Path(str(item.get("path", "")))
        exists = binary.is_file()
        actual = sha256_file(binary) if exists else None
        matches = exists and actual == item.get("sha256")
        runtime_results.append({"name": item.get("name"), "exists": exists, "hash_matches": matches})
        if not matches:
            errors.append(f"Runtime binary missing or changed: {item.get('name')}")
    return {
        "schema_version": 1,
        "status": "pass" if not errors else "fail",
        "manifest_path": str(source),
        "manifest_sha256": sha256_file(source),
        "release_version": release.get("version"),
        "api_version": release.get("api_version"),
        "public_tool_count": data.get("public_tool_count"),
        "file_checks": file_results,
        "runtime_checks": runtime_results,
        "errors": errors,
    }


def verify_deployment(root: Path) -> dict[str, Any]:
    deployment = root.resolve(strict=True)
    bundle_path = deployment / "release-bundle.json"
    manifest_path = deployment / "release-manifest.json"
    errors: list[str] = []
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "schema_version": 1,
            "status": "fail",
            "deployment_root": str(deployment),
            "errors": [f"Deployment metadata is unreadable: {exc}"],
        }
    if bundle.get("schema_version") != 1 or bundle.get("version") != __version__:
        errors.append("Deployment bundle version differs from the installed package")
    if sha256_file(manifest_path) != bundle.get("source_manifest_sha256"):
        errors.append("Deployment source manifest hash differs from the bundle")
    if (manifest.get("release") or {}).get("version") != __version__:
        errors.append("Deployment source manifest version differs from the installed package")
    entries = {str(item.get("path")): item for item in bundle.get("files", []) if isinstance(item, dict)}
    critical = [
        "requirements.lock",
        "mcp-config.example.json",
        "config/policy.json",
        "config/materialsscript-capabilities.json",
        "config/project-manifest.template.json",
        "config/project-manifest.schema.v2.json",
        "config/qualification-profiles.json",
        "config/scientific-gate-intake.schema.v1.json",
        "config/science-contract.schema.json",
        "config/software.local.json",
        "moc/ms_moc.py",
        "moc/ms_mcp_bridge.py",
        "moc/MS_MOC_INTERFACE.md",
        "moc/MS_MOC_STATUS.md",
        "moc/SCIENCE_ENVIRONMENT.md",
        "moc/science-requirements.lock",
    ]
    wheel_entries = [path for path in entries if path.startswith("wheelhouse/materials_studio_mcp-") and path.endswith(".whl")]
    if len(wheel_entries) != 1:
        errors.append("Deployment bundle does not contain exactly one project wheel")
    else:
        critical.append(wheel_entries[0])
    checks: list[dict[str, Any]] = []
    for relative in critical:
        entry = entries.get(relative)
        path = deployment / Path(relative)
        matches = bool(entry and path.is_file() and sha256_file(path) == entry.get("sha256"))
        checks.append({"path": relative, "hash_matches": matches})
        if not matches:
            errors.append(f"Deployment critical file is missing or changed: {relative}")
    test_entries = sorted(path for path in entries if path.startswith("tests/") and path.endswith(".py"))
    if not test_entries:
        errors.append("Deployment bundle does not contain the regression suite")
    for relative in test_entries:
        entry = entries[relative]
        path = deployment / Path(relative)
        matches = path.is_file() and sha256_file(path) == entry.get("sha256")
        checks.append({"path": relative, "hash_matches": matches})
        if not matches:
            errors.append(f"Deployment regression file is missing or changed: {relative}")
    wheel_install_checks: list[dict[str, Any]] = []
    if len(wheel_entries) == 1 and (deployment / wheel_entries[0]).is_file():
        wheel = deployment / wheel_entries[0]
        site_packages = deployment / ".venv" / "Lib" / "site-packages"
        with zipfile.ZipFile(wheel) as archive:
            for info in archive.infolist():
                if info.is_dir() or info.filename.endswith(".dist-info/RECORD"):
                    continue
                installed = site_packages / Path(info.filename)
                expected = base64.urlsafe_b64encode(hashlib.sha256(archive.read(info.filename)).digest()).rstrip(b"=").decode("ascii")
                actual = base64.urlsafe_b64encode(hashlib.sha256(installed.read_bytes()).digest()).rstrip(b"=").decode("ascii") if installed.is_file() else None
                matches = actual == expected
                wheel_install_checks.append({"path": info.filename, "hash_matches": matches})
                if not matches:
                    errors.append(f"Installed wheel file is missing or changed: {info.filename}")
    runtime_checks: list[dict[str, Any]] = []
    for item in manifest.get("runtime_binaries", []):
        path = Path(str(item.get("path", "")))
        matches = path.is_file() and sha256_file(path) == item.get("sha256")
        runtime_checks.append({"name": item.get("name"), "hash_matches": matches})
        if not matches:
            errors.append(f"Deployment runtime binary is missing or changed: {item.get('name')}")
    return {
        "schema_version": 1,
        "status": "pass" if not errors else "fail",
        "deployment_root": str(deployment),
        "version": bundle.get("version"),
        "bundle_sha256": sha256_file(bundle_path),
        "critical_file_checks": checks,
        "installed_wheel_file_checks": wheel_install_checks,
        "runtime_checks": runtime_checks,
        "errors": errors,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Build or verify the Materials Studio MCP/MOC v1 release manifest.")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    build.add_argument("--force", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    verify_deployed = sub.add_parser("verify-deployment")
    verify_deployed.add_argument("--root", required=True)
    args = parser.parse_args()
    try:
        if args.command == "build":
            result = write_release_manifest(Path(args.manifest), force=args.force)
        elif args.command == "verify":
            result = verify_release_manifest(Path(args.manifest))
        else:
            result = verify_deployment(Path(args.root))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"written", "pass"} else 1
    except Exception as exc:
        print(json.dumps({
            "schema_version": 1,
            "status": "error",
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
