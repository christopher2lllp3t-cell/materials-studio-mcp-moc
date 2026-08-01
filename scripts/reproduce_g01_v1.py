from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from materials_studio_mcp.g01_reproduction import DEFAULT_PROJECTS_ROOT, reproduce_g01


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run a fresh, fail-closed G01 PCFF v1 reproduction project.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--projects-root", default=str(DEFAULT_PROJECTS_ROOT))
    args = parser.parse_args()
    try:
        result = reproduce_g01(args.project_id, Path(args.projects_root))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({
            "status": "error",
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
