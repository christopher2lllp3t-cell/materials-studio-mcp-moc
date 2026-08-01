from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable


PUBLIC_GATEWAY_FIELDS = frozenset(
    {
        "version", "revision", "osname", "osversion", "mpiversion",
        "versionmajor", "versionminor", "installedmemory", "cpu",
        "corespercpu", "cpucorestotal", "gpuavailable", "queuingsystem",
        "jobpriority", "licensewaitoverride",
    }
)


def _key_value_file(path: Path, allowed: frozenset[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        if key in allowed:
            values[key] = value.strip()
    return values


def _windows_service_running(name: str) -> bool | None:
    try:
        import win32service
        import win32serviceutil

        return win32serviceutil.QueryServiceStatus(name)[1] == win32service.SERVICE_RUNNING
    except Exception:
        return None


def inspect_castep_gateway_readiness(
    materials_studio_root: Path,
    requested_cores: int = 12,
    *,
    service_probe: Callable[[str], bool | None] = _windows_service_running,
) -> dict[str, Any]:
    if isinstance(requested_cores, bool) or not isinstance(requested_cores, int) or not 1 <= requested_cores <= 4096:
        raise ValueError("requested_cores must be an integer between 1 and 4096")
    materials_studio_root = materials_studio_root.resolve()
    location_path = materials_studio_root / "etc" / "Gateway" / "gwlocation.cfg"
    if not location_path.is_file():
        raise FileNotFoundError(f"Materials Studio Gateway location file is missing: {location_path}")
    location = _key_value_file(location_path, frozenset({"jobsdocroot"}))
    jobsdocroot_text = location.get("jobsdocroot")
    if not jobsdocroot_text:
        raise ValueError("Gateway location does not define jobsdocroot")
    jobsdocroot = Path(jobsdocroot_text).expanduser().resolve()
    if not jobsdocroot.is_absolute() or "BIOVIA" not in {part.upper() for part in jobsdocroot.parts}:
        raise ValueError("Gateway jobsdocroot is not an absolute BIOVIA-managed path")
    info_path = jobsdocroot / "dsd" / "conf" / "gw-info.sbd"
    if not info_path.is_file():
        raise FileNotFoundError(f"Gateway public information file is missing: {info_path}")
    info = _key_value_file(info_path, PUBLIC_GATEWAY_FIELDS)
    core_match = re.search(r"\d+", info.get("cpucorestotal", ""))
    available_cores = int(core_match.group()) if core_match else None
    queue_system = info.get("queuingsystem", "").strip()
    queue_configured = bool(queue_system and queue_system.lower() not in {"[none]", "none", "undefined"})
    gateway_service = service_probe("MaterialsStudioGateway")
    license_service = service_probe("BIOVIA License Server")
    local_gateway_ready = gateway_service is True and info_path.is_file()
    requested_cores_fit_local = available_cores is not None and requested_cores <= available_cores
    common_blockers: list[dict[str, str]] = []
    if gateway_service is not True:
        common_blockers.append({"code": "GATEWAY_SERVICE_NOT_CONFIRMED", "detail": "MaterialsStudioGateway is not confirmed running."})
    if license_service is not True:
        common_blockers.append({"code": "LICENSE_SERVICE_NOT_CONFIRMED", "detail": "BIOVIA License Server is not confirmed running."})
    local_blockers = list(common_blockers)
    remote_blockers = list(common_blockers)
    if not queue_configured:
        remote_blockers.append({"code": "REMOTE_QUEUE_NOT_CONFIGURED", "detail": "Gateway reports no configured queuing system."})
    if not requested_cores_fit_local:
        local_blockers.append(
            {
                "code": "REQUESTED_CORES_EXCEED_LOCAL_CAPACITY",
                "detail": f"Requested {requested_cores} cores but the local Gateway reports {available_cores or 'unknown'} cores.",
            }
        )
    local_submission_candidate = not local_blockers
    remote_submission_ready = not remote_blockers
    available_modes = [
        mode
        for mode, ready in (
            ("local", local_submission_candidate),
            ("remote", remote_submission_ready),
        )
        if ready
    ]
    blockers: list[dict[str, str]] = []
    if not available_modes:
        seen_codes: set[str] = set()
        for blocker in (*local_blockers, *remote_blockers):
            if blocker["code"] not in seen_codes:
                blockers.append(blocker)
                seen_codes.add(blocker["code"])
    return {
        "schema_version": 1,
        "status": "ready" if available_modes else "blocked",
        "materials_studio_root": str(materials_studio_root),
        "gateway_location_path": str(location_path),
        "gateway_jobsdocroot": str(jobsdocroot),
        "gateway_info_path": str(info_path),
        "gateway_service_running": gateway_service,
        "license_service_running": license_service,
        "gateway": info,
        "requested_cores": requested_cores,
        "available_local_cores": available_cores,
        "requested_cores_fit_local": requested_cores_fit_local,
        "local_gateway_ready": local_gateway_ready,
        "remote_queue_configured": queue_configured,
        "remote_submission_ready": remote_submission_ready,
        "local_submission_candidate": local_submission_candidate,
        "available_modes": available_modes,
        "runtime_preflight_ready": local_gateway_ready,
        "blockers": blockers,
        "local_blockers": local_blockers,
        "remote_blockers": remote_blockers,
        "limitations": [
            "A running license service does not prove that a CASTEP license seat is currently available.",
            "Local submission remains disabled until the user explicitly requests it.",
            "Remote submission requires a reviewed Gateway queue profile and a separate confirmation.",
        ],
    }
