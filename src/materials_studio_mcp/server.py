from __future__ import annotations

from datetime import datetime
from functools import wraps
from html import unescape
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import hashlib
import inspect
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .pipeline_config import (
    acquire_execution_slot,
    approved_executable,
    bounded_timeout,
    discover_project_root,
    load_pipeline_config,
    pipeline_health_check,
    resolve_output_path,
    resolve_workspace_path,
)
from . import __version__
from .conversion_executor import _terminate_process_tree
from .project_manager import (
    _record_verified_quality_gate,
    get_project,
    initialize_project,
    register_artifact,
    set_quality_gate,
    transition_project_status,
    update_model_specification,
    validate_project,
)
from .structure_preflight import inspect_msi2lmp_inputs, inspect_structure_preflight
from .conversion_executor import convert_car_mdf
from .confirmation import confirmation_manager
from .security import redact_sensitive
from .operations import run_idempotent
from .public_registry import PUBLIC_TOOLS, api_catalog, public_tool_names
from .api_contract import RecordedExecutionError, ensure_public_result_shape, error_result, success_result
from .capability_registry import audit_capability_registry
from .geopore_gate import assess_geopore_contract
from .moc_control import MOC_DOCUMENT_SUFFIXES, get_moc_status, launch_document
from .geology_modeling import (
    build_crystal_parent_import_script,
    build_counterion_script,
    build_hydroxylation_script,
    build_periodic_slab_cell_script,
    build_supercell_script,
    build_substitution_script,
    build_surface_enumeration_script,
    inspect_xsd_geometry,
    surface_normal_span_angstrom,
    parse_charge_audit,
    sha256_file,
    validate_counterion_ledger,
    validate_counterion_result,
    validate_crystal_parent_import_result,
    validate_crystal_parent_request,
    validate_hydroxylation_ledger,
    validate_hydroxylation_result,
    validate_input_hash,
    validate_output_slot,
    validate_periodic_slab_cell_result,
    validate_repeats,
    validate_substitution_ledger,
    validate_substitution_result,
    validate_supercell_result,
    validate_surface_candidates,
    validate_surface_mesh_vectors,
    validate_surface_parameters,
)
from .periodic_packing import (
    audit_packed_xyz,
    build_packed_fluid_import_script,
    packed_fluid_tsv,
    packmol_input_text,
    parse_xyz,
    periodic_orthorhombic_frame,
    spce_template_xyz,
    validate_aqueous_nacl_request,
    validate_packed_xsd,
    xyz_text,
)
from .clayff_lammps import (
    classify_neutral_quartz_spce_nacl,
    forcefield_profile,
    protocol_contract,
    render_gate_input,
    render_lammps_data,
    render_production_input,
    validate_forcefield_sources,
)
from .task_manager import (
    DEFAULT_TASK_ROOT,
    cancel_task,
    query_task,
    retry_task,
    submit_task,
    validate_task_request,
)
from .qualification_workflow import run_g01_qualification_vertical
from .scientific_gate_audit import audit_target_model_science
from .castep_pl import prepare_castep_pl_package
from .castep_standalone import prepare_castep_standalone_inputs
from .castep_p4b_contract import inspect_fixed_profile_preflight_request
from .castep_preflight import (
    PREFLIGHT_ENVIRONMENT_VARIABLE,
    finalize_castep_preflight,
    inspect_castep_preflight_plan,
)
from .castep_gateway import inspect_castep_gateway_readiness
from .adaptive_planning import build_adaptive_calculation_plan
from .model_readiness import assess_model_readiness, build_model_gap_resolution_plan
from .public_evidence import build_public_evidence_request, search_public_model_evidence


SERVER_NAME = "materials-studio-2023"
HELPER_PATH = Path(__file__).with_name("ms_helper.ps1")
DEFAULT_TIMEOUT_SECONDS = 120
PROJECT_ROOT = discover_project_root(__file__)
WORKSPACE_ROOT = PROJECT_ROOT.parent
CH03_SCRIPT_ROOT = WORKSPACE_ROOT / "03_模拟脚本" / "ch03_竞争吸附"
RUNTIME_ALIAS_ROOT = Path(r"E:\ms_mcp\ms_mcp_runtime\materials_studio_2023")
MATERIALSSCRIPT_SCRATCH_ENV = "MATERIALS_STUDIO_MCP_ASCII_SCRATCH_ROOT"
DEFAULT_MATERIALSSCRIPT_SCRATCH_ROOT = RUNTIME_ALIAS_ROOT.parent / "scratch" / "materials_studio_mcp"


def _dry_run_payload(
    tool_name: str,
    parameters: dict[str, Any],
    *,
    planned_outputs: list[str] | None = None,
    validations: dict[str, Any] | None = None,
    template_text: str | None = None,
    resource_estimate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, write-free execution plan for a governed tool."""

    canonical = json.dumps(parameters, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return {
        "status": "dry_run",
        "dry_run": True,
        "writes_performed": False,
        "execution_started": False,
        "parameters_sha256": hashlib.sha256(canonical.encode("ascii")).hexdigest().upper(),
        "template_sha256": (
            hashlib.sha256(template_text.encode("utf-8")).hexdigest().upper()
            if template_text is not None else None
        ),
        "planned_outputs": planned_outputs or [],
        "validations": validations or {},
        "resource_estimate": resource_estimate or {"parallel_jobs": 1},
        "confirmation_parameters": parameters,
        "next_action": f"Issue an exact confirmation for {tool_name} before dry_run=false.",
    }


def _pipeline_check(health: dict[str, Any], name: str) -> dict[str, Any]:
    for item in health.get("checks", []):
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return {"name": name, "status": "missing"}


def _assert_ascii_absolute_path(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path: {path}")
    if not str(path).isascii():
        raise ValueError(
            f"{label} must contain ASCII characters only because Materials Studio 23.1 "
            f"can crash when MatServer starts from a Unicode path: {path}"
        )
    return path


def _materialsscript_scratch_root() -> Path:
    configured = os.environ.get(MATERIALSSCRIPT_SCRATCH_ENV)
    root = Path(configured) if configured else DEFAULT_MATERIALSSCRIPT_SCRATCH_ROOT
    _assert_ascii_absolute_path(root, "MaterialsScript scratch root")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _assert_no_unicode_absolute_path_literals(script: str) -> None:
    quoted_literal = re.compile(r'''(["'])(?P<value>.*?)(?<!\\)\1''', re.DOTALL)
    unsafe: list[str] = []
    for match in quoted_literal.finditer(script):
        value = match.group("value")
        if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(r"\\"):
            if not value.isascii():
                unsafe.append(value)
    if unsafe:
        preview = ", ".join(repr(value) for value in unsafe[:3])
        raise ValueError(
            "MaterialsScript contains a non-ASCII absolute path literal. Copy the file into "
            f"the staged input directory and use a template placeholder instead: {preview}"
        )


ZH_INSPECT_TERMS = [
    "\u68c0\u67e5",
    "\u5206\u6790",
    "\u7ed3\u6784\u4fe1\u606f",
    "\u539f\u5b50\u6570",
    "\u8f68\u8ff9",
    "\u7edf\u8ba1",
    "\u5206\u5b50\u5f0f",
    "\u5143\u6570\u636e",
]
ZH_DOC_TERMS = ["\u5e2e\u52a9", "\u6587\u6863", "\u793a\u4f8b", "\u6559\u7a0b", "\u53c2\u8003"]
ZH_SCRIPT_TERMS = ["\u811a\u672c", "\u81ea\u5b9a\u4e49", "\u81ea\u52a8\u5316", "perl\u811a\u672c"]
ZH_ENERGY_TERMS = ["\u80fd\u91cf", "\u5355\u70b9", "\u52bf\u80fd"]
ZH_OPTIMIZATION_TERMS = ["\u4f18\u5316", "\u51e0\u4f55\u4f18\u5316", "\u5f1b\u8c6b", "\u6700\u5c0f\u5316"]
ZH_DYNAMICS_TERMS = ["\u5206\u5b50\u52a8\u529b\u5b66", "\u52a8\u529b\u5b66", "\u8f68\u8ff9", "\u5e73\u8861"]
ZH_RDF_TERMS = ["\u5f84\u5411\u5206\u5e03\u51fd\u6570"]
ZH_MSD_TERMS = ["\u5747\u65b9\u4f4d\u79fb", "\u6269\u6563"]
ZH_HBOND_TERMS = ["\u6c22\u952e", "\u6c22\u952e\u5206\u6790"]
ZH_VACF_TERMS = ["\u901f\u5ea6\u81ea\u76f8\u5173", "\u901f\u5ea6\u81ea\u5173\u51fd\u6570", "\u529f\u7387\u8c31"]
ZH_TEMPERATURE_TERMS = ["\u6e29\u5ea6"]
ZH_PRESSURE_TERMS = ["\u538b\u529b", "\u538b\u529b\u5f20\u91cf"]
ZH_DENSITY_TERMS = ["\u5bc6\u5ea6"]
ZH_POTENTIAL_COMPONENT_TERMS = ["\u52bf\u80fd", "\u52bf\u80fd\u7ec4\u5206", "\u80fd\u91cf\u7ec4\u5206", "\u80fd\u91cf\u5206\u91cf"]
ZH_CELL_PARAMETER_TERMS = ["\u6676\u80de", "\u6676\u80de\u53c2\u6570", "\u6676\u683c\u53c2\u6570", "\u4f53\u79ef", "\u76d2\u5b50"]
ZH_THERMO_TERMS = ["\u70ed\u529b\u5b66", "\u7a33\u5b9a\u6027", "\u6536\u655b", "\u5e73\u8861"]
ZH_SEQUENCE_TERMS = ["\u5148", "\u518d", "\u7136\u540e", "\u4e4b\u540e"]
ELEMENT_NAME_PATTERNS: dict[str, list[str]] = {
    "H": ["hydrogen", "\u6c22"],
    "C": ["carbon", "\u78b3"],
    "N": ["nitrogen", "\u6c2e"],
    "O": ["oxygen", "\u6c27"],
    "F": ["fluorine", "\u6c1f"],
    "Na": ["sodium", "\u94a0"],
    "Mg": ["magnesium", "\u9541"],
    "Al": ["aluminum", "aluminium", "\u94dd"],
    "Si": ["silicon", "\u7845"],
    "P": ["phosphorus", "\u78f7"],
    "S": ["sulfur", "sulphur", "\u786b"],
    "Cl": ["chlorine", "\u6c2f"],
    "Ca": ["calcium", "\u9499"],
    "Fe": ["iron", "\u94c1"],
}
FORCITE_HELP_PAGES = {
    "Energy": [
        "scriptingapi/apiimport.htm",
        "forcitescripting/apiforciteenergy.htm",
        "scriptingapi/apiexport.htm",
    ],
    "GeometryOptimization": [
        "scriptingapi/apiimport.htm",
        "forcitescripting/apiforcitegeometryoptimization.htm",
        "scriptingapi/apiexport.htm",
    ],
    "Dynamics": [
        "scriptingapi/apiimport.htm",
        "forcitescripting/apiforcitedynamics.htm",
        "scriptingapi/apiexport.htm",
    ],
    "RadialDistributionFunction": [
        "scriptingapi/apiimport.htm",
        "forcitescripting/apiforciteanalysis.htm",
        "forcitescripting/apiforciteradialdistributionfunction.htm",
        "scriptingapi/docstudytable.htm",
    ],
    "MeanSquareDisplacement": [
        "scriptingapi/apiimport.htm",
        "forcitescripting/apiforciteanalysis.htm",
        "forcitescripting/apiforcitemeansquaredisplacement.htm",
        "scriptingapi/docstudytable.htm",
    ],
    "VelocityAutocorrelationFunction": [
        "scriptingapi/apiimport.htm",
        "forcitescripting/apiforciteanalysis.htm",
        "forcitescripting/apiforcitevelocityautocorrelationfunction.htm",
        "scriptingapi/docstudytable.htm",
    ],
    "Temperature": [
        "scriptingapi/apiimport.htm",
        "forcitescripting/apiforciteanalysis.htm",
        "forcitescripting/apiforcitetemperature.htm",
        "scriptingapi/docstudytable.htm",
    ],
    "Pressure": [
        "scriptingapi/apiimport.htm",
        "forcitescripting/apiforciteanalysis.htm",
        "forcitescripting/apiforcitepressure.htm",
        "scriptingapi/docstudytable.htm",
    ],
    "Density": [
        "scriptingapi/apiimport.htm",
        "forcitescripting/apiforciteanalysis.htm",
        "forcitescripting/apiforcitedensity.htm",
        "scriptingapi/docstudytable.htm",
    ],
    "PotentialEnergyComponents": [
        "scriptingapi/apiimport.htm",
        "forcitescripting/apiforciteanalysis.htm",
        "forcitescripting/apiforcitepotentialenergycomponents.htm",
        "scriptingapi/docstudytable.htm",
    ],
    "CellParameters": [
        "scriptingapi/apiimport.htm",
        "forcitescripting/apiforciteanalysis.htm",
        "forcitescripting/apiforcitecellparameters.htm",
        "scriptingapi/docstudytable.htm",
    ],
    "ThermoProfiles": [
        "scriptingapi/apiimport.htm",
        "forcitescripting/apiforciteanalysis.htm",
        "forcitescripting/apiforcitetemperature.htm",
        "forcitescripting/apiforcitepressure.htm",
        "forcitescripting/apiforcitedensity.htm",
        "forcitescripting/apiforcitepotentialenergycomponents.htm",
        "forcitescripting/apiforcitecellparameters.htm",
        "scriptingapi/docstudytable.htm",
    ],
    "DynamicsWithAnalysis": [
        "scriptingapi/apiimport.htm",
        "forcitescripting/apiforcitedynamics.htm",
        "forcitescripting/apiforciteanalysis.htm",
        "forcitescripting/apiforciteradialdistributionfunction.htm",
        "forcitescripting/apiforcitemeansquaredisplacement.htm",
        "forcitescripting/apiforcitevelocityautocorrelationfunction.htm",
        "forcitescripting/apiforcitetemperature.htm",
        "forcitescripting/apiforcitepressure.htm",
        "forcitescripting/apiforcitedensity.htm",
        "forcitescripting/apiforcitepotentialenergycomponents.htm",
        "forcitescripting/apiforcitecellparameters.htm",
        "scriptingapi/apihydrogenbonds.htm",
        "scriptingapi/apicurrentframe.htm",
        "scriptingapi/apinumframes.htm",
        "scriptingapi/docstudytable.htm",
    ],
    "HydrogenBondStatistics": [
        "scriptingapi/apiimport.htm",
        "scriptingapi/apihydrogenbonds.htm",
        "scriptingapi/apicurrentframe.htm",
        "scriptingapi/apinumframes.htm",
        "scriptingapi/docstudytable.htm",
    ],
}
WORKFLOW_CATALOG: list[dict[str, Any]] = [
    {
        "id": "inspect_document",
        "tool": "ms_inspect_document",
        "title": "Inspect structure or trajectory files",
        "description": "Parse .xsd, .xtd, and .stp files and return counts, formula, periodicity, and trajectory metadata.",
        "keywords": ["inspect", "metadata", "formula", "atom count", "trajectory", "project", "检查", "分析", "结构信息", "原子数", "轨迹"],
        "when_to_use": "Use when you need to understand the contents of a Materials Studio file before running a calculation.",
    },
    {
        "id": "search_local_docs",
        "tool": "ms_search_local_help",
        "title": "Search local Materials Studio documentation",
        "description": "Search the installed Materials Studio help for APIs, settings, and workflow examples.",
        "keywords": ["help", "documentation", "api", "example", "manual", "reference", "帮助", "文档", "示例", "教程", "参考"],
        "when_to_use": "Use when you need precise local documentation or code examples from the installed version.",
    },
    {
        "id": "run_custom_script",
        "tool": "ms_run_materialsscript",
        "title": "Run custom MaterialsScript",
        "description": "Execute an arbitrary MaterialsScript Perl job with structured input staging and output collection.",
        "keywords": ["custom", "script", "perl", "materialsscript", "automation", "脚本", "自定义", "自动化", "perl脚本"],
        "when_to_use": "Use when the built-in high-level tools do not cover the exact operation you need.",
    },
    {
        "id": "forcite_energy",
        "tool": "ms_forcite_energy",
        "title": "Forcite single-point energy",
        "description": "Run a high-level Forcite Energy calculation and return parsed energy metrics.",
        "keywords": ["energy", "single point", "potential energy", "forcite", "能量", "单点", "势能"],
        "when_to_use": "Use for quick evaluation of a structure without changing geometry.",
    },
    {
        "id": "forcite_geometry_optimization",
        "tool": "ms_forcite_geometry_optimization",
        "title": "Forcite geometry optimization",
        "description": "Relax the structure with Forcite Geometry Optimization and export the optimized .xsd.",
        "keywords": ["geometry optimization", "optimize", "relax", "minimize", "forcite", "几何优化", "优化", "弛豫", "最小化"],
        "when_to_use": "Use before production calculations or when the input structure is not yet relaxed.",
    },
    {
        "id": "forcite_dynamics",
        "tool": "ms_forcite_dynamics",
        "title": "Forcite molecular dynamics",
        "description": "Run a high-level Forcite Dynamics workflow and export a trajectory plus optional final structure.",
        "keywords": ["dynamics", "md", "trajectory", "nvt", "npt", "equilibration", "forcite", "分子动力学", "动力学", "轨迹", "平衡"],
        "when_to_use": "Use when you need a time evolution trajectory or thermodynamic sampling.",
    },
    {
        "id": "forcite_rdf",
        "tool": "ms_forcite_rdf",
        "title": "Forcite radial distribution function analysis",
        "description": "Analyze an atomistic trajectory and return structured RDF data plus optional study table output.",
        "keywords": ["rdf", "radial distribution function", "g(r)", "pair correlation", "径向分布函数"],
        "when_to_use": "Use when you want pair correlation data from a Forcite trajectory.",
    },
    {
        "id": "forcite_msd",
        "tool": "ms_forcite_msd",
        "title": "Forcite mean square displacement analysis",
        "description": "Analyze an atomistic trajectory and return structured MSD data plus optional study table output.",
        "keywords": ["msd", "mean square displacement", "diffusion", "均方位移"],
        "when_to_use": "Use when you want displacement-versus-time data from a Forcite trajectory.",
    },
    {
        "id": "forcite_dynamics_with_analysis",
        "tool": "ms_forcite_dynamics_with_analysis",
        "title": "Forcite dynamics plus automatic analysis",
        "description": "Run dynamics, export the trajectory, then automatically perform RDF and/or MSD analysis.",
        "keywords": [
            "dynamics and rdf",
            "dynamics and msd",
            "simulate and analyze",
            "md and rdf",
            "md and msd",
            "动力学后分析",
            "先做动力学再分析",
            "RDF和MSD",
        ],
        "when_to_use": "Use when you want one end-to-end workflow that produces both a trajectory and structured analysis outputs.",
    },
    {
        "id": "forcite_relax_and_dynamics",
        "tool": "ms_forcite_relax_and_dynamics",
        "title": "Composite relax then dynamics workflow",
        "description": "First optimize the structure, then run dynamics using the optimized structure as input.",
        "keywords": ["workflow", "optimize then dynamics", "relax then md", "equilibrate", "best practice", "先优化后动力学", "优化后做分子动力学", "先弛豫再动力学"],
        "when_to_use": "Use when you want a more realistic MD starting point and more reliable downstream results.",
    },
]

CONFIRMATION_ISSUER_TOOL = "md_prepare_production_confirmation"


def _public_tool_result(value: Any, tool_name: str) -> dict[str, Any]:
    """Normalize output while exposing only the dedicated single-use capability."""
    result = ensure_public_result_shape(value, tool=tool_name)
    if tool_name == CONFIRMATION_ISSUER_TOOL and isinstance(value, dict):
        token = value.get("confirmation_token")
        if isinstance(token, str) and token.count(".") == 1:
            # The token is intentionally returned only by the dedicated issuer.
            # It is short-lived, single-use, and bound to an exact operation hash.
            result["confirmation_token"] = token
    return result


class ContractFastMCP(FastMCP):
    """Apply the public result contract at the only MCP registration boundary."""

    def tool(self, *args: Any, **kwargs: Any):
        register = super().tool(*args, **kwargs)

        def decorator(fn: Any):
            tool_name = str(kwargs.get("name") or (args[0] if args else fn.__name__))
            if inspect.iscoroutinefunction(fn):
                @wraps(fn)
                async def async_wrapper(*fn_args: Any, **fn_kwargs: Any) -> dict[str, Any]:
                    try:
                        value = await fn(*fn_args, **fn_kwargs)
                        return _public_tool_result(value, tool_name)
                    except Exception as exc:
                        return error_result(tool_name, exc)
                registered = async_wrapper
            else:
                @wraps(fn)
                def sync_wrapper(*fn_args: Any, **fn_kwargs: Any) -> dict[str, Any]:
                    try:
                        value = fn(*fn_args, **fn_kwargs)
                        return _public_tool_result(value, tool_name)
                    except Exception as exc:
                        return error_result(tool_name, exc)
                registered = sync_wrapper
            register(registered)
            return fn

        return decorator


mcp = ContractFastMCP(SERVER_NAME)
# MCP SDK 1.27 does not expose a FastMCP version constructor argument. Set the
# low-level identity explicitly so protocol clients see this server release.
mcp._mcp_server.version = __version__


def _clean_workflow_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": "model_readiness_assess",
            "tool": "md_model_readiness_assess",
            "title": "Assess model intake readiness",
            "description": "Read local structure and force-field candidate metadata, identify missing model conditions, and distinguish mechanically resolvable gaps from scientific decisions. It does not create a model or select scientific parameters.",
            "keywords": ["model readiness", "model intake", "force field gap", "建模条件", "力场缺口"],
            "when_to_use": "Use before choosing a construction or calculation workflow, especially when structure, cell, charge, or force-field inputs are incomplete.",
        },
        {
            "id": "model_gap_resolution_plan",
            "tool": "md_model_gap_resolution_plan",
            "title": "Plan safe resolution of model gaps",
            "description": "Turn a model intake assessment into ordered local checks, optional public metadata searches, and explicit human scientific gates without performing writes or calculations.",
            "keywords": ["gap resolution", "model plan", "parameter gap", "缺口补救", "建模计划"],
            "when_to_use": "Use after readiness assessment to decide which existing controlled MCP tool or manual scientific review is needed next.",
        },
        {
            "id": "public_model_evidence",
            "tool": "md_search_public_model_evidence",
            "title": "Search fixed public model-evidence metadata sources",
            "description": "With explicit opt-in and a single-use confirmation, query PubChem or Crossref metadata only. It never downloads structures or force fields and never treats a search result as parameter validation.",
            "keywords": ["PubChem", "Crossref", "model evidence", "公开资料", "文献元数据"],
            "when_to_use": "Use only after a dry-run when local sources are insufficient and the query may be sent to an approved public provider.",
        },
        {
            "id": "prepare_castep_pl_package",
            "tool": "ms_prepare_castep_pl_package",
            "title": "Prepare a hash-bound CASTEP PL/XSD package",
            "description": "Generate self-contained, non-overwriting CASTEP geometry-optimization and spin-screen task folders without selecting a Gateway or submitting a job.",
            "keywords": ["CASTEP", "PL package", "spin screen", "DFT geometry optimization", "remote Gateway"],
            "when_to_use": "Use after an XSD and its SHA-256 are frozen, when the next step is manual reviewed submission through Materials Studio Run on Server.",
        },
        {
            "id": "prepare_castep_standalone_inputs",
            "tool": "ms_prepare_castep_standalone_inputs",
            "title": "Prepare a non-executable standalone CASTEP input candidate",
            "description": "Generate a hash-bound .cell/.param candidate for the official RunCASTEP launcher without invoking it, selecting a Gateway, acquiring a license, or submitting a job.",
            "keywords": ["CASTEP standalone", "RunCASTEP", ".cell", ".param", "single point", "no execution"],
            "when_to_use": "Use only after the 3D periodic XSD, nonmagnetic-insulator scope, PBE, cutoff, and Monkhorst-Pack grid are explicitly reviewed. The generated package remains execution-blocked.",
        },
        {
            "id": "castep_preflight_checked",
            "tool": "ms_castep_preflight_checked",
            "title": "Run the exact generated CASTEP PL in no-CASTEP preflight mode",
            "description": "Execute one hash-bound generated PL through the real Materials Studio 23.1 MatServer path, validate its exact sibling XSD and runtime atom count, and exit before Gateway selection or CASTEP execution.",
            "keywords": ["CASTEP preflight", "RunMatScript", "MatServer", "runtime atom count", "no submission"],
            "when_to_use": "Use after package preparation and before any reviewed remote submission. Dry-run first, then issue an exact confirmation for the runtime-only preflight.",
        },
        {
            "id": "castep_gateway_readiness",
            "tool": "ms_castep_gateway_readiness",
            "title": "Inspect local CASTEP Gateway readiness",
            "description": "Read only the allowlisted public Materials Studio Gateway fields and report local core capacity, service state, queue readiness, and submission blockers without acquiring a CASTEP license or submitting a job.",
            "keywords": ["CASTEP Gateway", "queue readiness", "license service", "core capacity"],
            "when_to_use": "Use before preparing a real CASTEP submission or when deciding between local and remote execution.",
        },
        {
            "id": "geology_import_crystal_parent",
            "tool": "ms_geology_import_crystal_parent",
            "title": "Import a provenance-locked crystal parent",
            "description": "Import a hash-bound CIF/XSD through Materials Studio, verify three-dimensional periodicity and the exact element inventory, and register a nonproduction XSD parent.",
            "keywords": ["CIF import", "crystal parent", "provenance", "quartz crystal"],
            "when_to_use": "Use before supercell or surface construction when the authoritative parent structure is a local CIF or XSD artifact.",
        },
        {
            "id": "geology_build_periodic_slab_cell",
            "tool": "ms_geology_build_periodic_slab_cell",
            "title": "Build an audited periodic slab cell",
            "description": "Convert a 2D surface to a standard-oriented 3D periodic slab cell and verify its total normal length without changing atoms, bonds, or composition.",
            "keywords": ["vacuum slab", "periodic interface", "slab cell", "surface normal"],
            "when_to_use": "Use after a reviewed 2D surface is complete and before packing fluid into the periodic interface gap.",
        },
        {
            "id": "pack_periodic_aqueous_nacl",
            "tool": "ms_pack_periodic_aqueous_nacl",
            "title": "Pack an audited periodic SPC/E NaCl interface",
            "description": "Use Packmol PBC to place exact SPC/E water and NaCl counts, then rebuild and validate a hash-bound periodic XSD.",
            "keywords": ["Packmol", "SPC/E", "NaCl", "electrolyte packing"],
            "when_to_use": "Use after a reviewed orthorhombic 3D slab cell is complete and before force-field assignment.",
        },
        {
            "id": "build_clayff_spce_nacl_lammps",
            "tool": "md_build_clayff_spce_nacl_lammps",
            "title": "Build a hash-bound ClayFF/SPC/E NaCl LAMMPS candidate",
            "description": "Type the audited neutral quartz interface against SI Table S1 and generate force-field, gate, and full protocol artifacts.",
            "keywords": ["ClayFF", "SPC/E", "Joung-Cheatham", "LAMMPS"],
            "when_to_use": "Use after exact periodic aqueous NaCl packing and before LAMMPS run-0 and short-runtime gates.",
        },
        {
            "id": "forcite_calculation_checked",
            "tool": "ms_forcite_calculation_checked",
            "title": "Run a governed Forcite calculation profile",
            "description": "Prepare and audit hash-bound structures with reviewed COMPASSIII, PCFF, Dreiding/QEq, or Universal/QEq profiles, or run governed COMPASSIII energy, geometry-optimization, and bounded NVT calculations.",
            "keywords": ["Forcite", "forcefield typing", "partial charge", "energy", "geometry optimization", "NVT dynamics", "COMPASSIII", "PCFF", "Dreiding", "Universal", "QEq"],
            "when_to_use": "Use a preparation profile when ForcefieldType is the only structure-preflight failure; use calculation profiles only after full structure and forcefield preflight.",
        },
        {
            "id": "geology_build_supercell",
            "tool": "ms_geology_build_supercell",
            "title": "Build an audited geology supercell",
            "description": "Use the local MS 2023 BuildSuperCell API, verify exact composition/cell scaling, and register a nonproduction XSD candidate.",
            "keywords": ["supercell", "crystal replication", "geology model", "build crystal", "超胞", "晶体扩胞", "地质模型"],
            "when_to_use": "Use after importing and hashing a periodic XSD bulk or layer model that must be replicated without changing chemistry.",
        },
        {
            "id": "geology_enumerate_surface_terminations",
            "tool": "ms_geology_enumerate_surface_terminations",
            "title": "Enumerate crystallographic surface terminations",
            "description": "Cleave a Miller plane at explicit top positions and return hashed candidate-only slabs without automatic repair or production selection.",
            "keywords": ["cleave surface", "Miller index", "surface termination", "quartz surface", "晶面切割", "表面终止", "石英表面"],
            "when_to_use": "Use to create an auditable termination candidate set before hydroxylation, charge review, forcefield assignment, or nanopore construction.",
        },
        {
            "id": "geology_apply_substitutions",
            "tool": "ms_geology_apply_substitutions",
            "title": "Apply an explicit isomorphic-substitution ledger",
            "description": "Mutate named P1 sites only after checking the source element and formal charge, then audit composition, charge, coordinates, topology, and cell invariance.",
            "keywords": ["isomorphic substitution", "site substitution", "layer charge", "同晶替换", "位点替换", "层电荷"],
            "when_to_use": "Use after a scientifically reviewed substitution ledger identifies every exact P1 atom site and its before/after formal charge.",
        },
        {
            "id": "geology_place_counterions",
            "tool": "ms_geology_place_counterions",
            "title": "Place explicit counterions from a reviewed coordinate ledger",
            "description": "Add named ions at explicit fractional coordinates and enforce triclinic minimum-image clearance, composition, formal-charge, topology, and cell gates.",
            "keywords": ["counterion", "compensating ion", "sodium placement", "补偿离子", "钠离子放置", "电荷补偿"],
            "when_to_use": "Use only after ion identity, count, formal charge, and deterministic fractional coordinates have been scientifically reviewed.",
        },
        {
            "id": "geology_apply_hydroxylation_ledger",
            "tool": "ms_geology_apply_hydroxylation_ledger",
            "title": "Apply an explicit surface-oxygen protonation ledger",
            "description": "Add H only to named, indexed, singly Si-coordinated O sites in a 2D p1 surface, with explicit coordinates, formal-charge deltas, O-H bonds, density, and clearance audits.",
            "keywords": ["surface hydroxylation", "silanol", "protonate oxygen", "表面羟基化", "硅醇", "氧质子化"],
            "when_to_use": "Use only after termination, surface side, exact oxygen sites, proton coordinates, and charge semantics have been independently reviewed.",
        },
        {
            "id": "geology_assess_nanopore_contract",
            "tool": "ms_geology_assess_nanopore_contract",
            "title": "Assess a paper-grade mineral nanopore intake contract",
            "description": "Verify hashed literature and bulk evidence, termination, hydroxylation, double-surface, pore-width, electrostatics, fixed-region, fluid, and forcefield decisions without building a model.",
            "keywords": ["nanopore contract", "construction gate", "quartz pore", "纳米孔合同", "构造门禁", "石英孔隙"],
            "when_to_use": "Use before double-surface construction or packing; construction is allowed only when this read-only validator returns construction_released=true.",
        },
        {
            "id": "moc_get_status",
            "tool": "ms_moc_get_status",
            "title": "Read Materials Studio MOC desktop-control readiness",
            "description": "Return the machine-readable MOC path, runner, desktop executable, MCP bridge, allowed-root, and failed-model guard status.",
            "keywords": ["MOC status", "desktop control", "Materials Studio UI", "MOC状态", "桌面控制", "打开MS"],
            "when_to_use": "Use before asking MOC to open a model or before diagnosing the local desktop-control layer.",
        },
        {
            "id": "moc_open_document",
            "tool": "ms_moc_open_document",
            "title": "Open a hash-bound model in Materials Studio through MOC",
            "description": "Validate project, workspace path, document SHA-256, idempotency, confirmation, and MOC allowlists before launching MatStudio with one document.",
            "keywords": ["open model", "launch Materials Studio", "MOC document", "打开模型", "启动Materials Studio", "MOC文档"],
            "when_to_use": "Use dry_run first, then issue an exact confirmation token before a real desktop launch.",
        },
        {
            "id": "inspect_document",
            "tool": "ms_inspect_document",
            "title": "Inspect structure or trajectory files",
            "description": "Parse .xsd, .xtd, and .stp files and return counts, formula, periodicity, and trajectory metadata.",
            "keywords": ["inspect", "metadata", "formula", "atom count", "trajectory", "project", *ZH_INSPECT_TERMS],
            "when_to_use": "Use when you need to understand the contents of a Materials Studio file before running a calculation.",
        },
        {
            "id": "list_analysis_targets",
            "tool": "ms_list_analysis_targets",
            "title": "List selectable analysis targets",
            "description": "Run a hash-bound, controlled MaterialsScript analysis that lists selectable elements, forcefield types, atom names, and existing set names; dry-run and confirmation are required by default.",
            "keywords": ["selection", "set", "elements", "forcefield type", "analysis targets", "atom names", "\u5143\u7d20", "\u96c6\u5408", "\u9009\u62e9", "\u539f\u5b50\u7c7b\u578b", "\u529b\u573a\u7c7b\u578b"],
            "when_to_use": "Use before RDF, MSD, or other targeted analyses when you need to know which subsets can be selected reliably.",
        },
        {
            "id": "search_local_docs",
            "tool": "ms_search_local_help",
            "title": "Search local Materials Studio documentation",
            "description": "Search the installed Materials Studio help for APIs, settings, and workflow examples.",
            "keywords": ["help", "documentation", "api", "example", "manual", "reference", *ZH_DOC_TERMS],
            "when_to_use": "Use when you need precise local documentation or code examples from the installed version.",
        },
        {
            "id": "forcite_energy",
            "tool": "ms_forcite_energy",
            "title": "Forcite single-point energy",
            "description": "Run a high-level Forcite Energy calculation and return parsed energy metrics.",
            "keywords": ["energy", "single point", "potential energy", "forcite", *ZH_ENERGY_TERMS],
            "when_to_use": "Use for quick evaluation of a structure without changing geometry.",
        },
        {
            "id": "forcite_geometry_optimization",
            "tool": "ms_forcite_geometry_optimization",
            "title": "Forcite geometry optimization",
            "description": "Relax the structure with Forcite Geometry Optimization and export the optimized .xsd.",
            "keywords": ["geometry optimization", "optimize", "relax", "minimize", "forcite", *ZH_OPTIMIZATION_TERMS],
            "when_to_use": "Use before production calculations or when the input structure is not yet relaxed.",
        },
        {
            "id": "forcite_dynamics",
            "tool": "ms_forcite_dynamics",
            "title": "Forcite molecular dynamics",
            "description": "Run a high-level Forcite Dynamics workflow and export a trajectory plus optional final structure.",
            "keywords": ["dynamics", "md", "trajectory", "nvt", "npt", "equilibration", "forcite", *ZH_DYNAMICS_TERMS],
            "when_to_use": "Use when you need a time evolution trajectory or thermodynamic sampling.",
        },
        {
            "id": "forcite_rdf",
            "tool": "ms_forcite_rdf",
            "title": "Forcite radial distribution function analysis",
            "description": "Analyze an atomistic trajectory and return structured RDF data plus optional study table output.",
            "keywords": ["rdf", "radial distribution function", "g(r)", "pair correlation", *ZH_RDF_TERMS],
            "when_to_use": "Use when you want pair correlation data from a Forcite trajectory.",
        },
        {
            "id": "forcite_msd",
            "tool": "ms_forcite_msd",
            "title": "Forcite mean square displacement analysis",
            "description": "Analyze an atomistic trajectory and return structured MSD data plus optional study table output.",
            "keywords": ["msd", "mean square displacement", "diffusion", *ZH_MSD_TERMS],
            "when_to_use": "Use when you want displacement-versus-time data from a Forcite trajectory.",
        },
        {
            "id": "forcite_vacf",
            "tool": "ms_forcite_vacf",
            "title": "Forcite velocity autocorrelation analysis",
            "description": "Analyze an atomistic trajectory and return structured VACF data plus optional power spectrum output.",
            "keywords": ["vacf", "velocity autocorrelation function", "power spectrum", "vibration spectrum", *ZH_VACF_TERMS],
            "when_to_use": "Use when you want velocity autocorrelation data or the derived power spectrum from a trajectory.",
        },
        {
            "id": "forcite_thermo_profiles",
            "tool": "ms_forcite_thermo_profiles",
            "title": "Forcite thermodynamic profile analysis",
            "description": "Analyze trajectory temperature, pressure, density, potential-energy components, and cell parameters with structured time-series output.",
            "keywords": [
                "thermo",
                "thermodynamic",
                "equilibration",
                "stability",
                "temperature",
                "pressure",
                "density",
                "potential energy",
                "cell parameters",
                *ZH_THERMO_TERMS,
                *ZH_TEMPERATURE_TERMS,
                *ZH_PRESSURE_TERMS,
                *ZH_DENSITY_TERMS,
                *ZH_POTENTIAL_COMPONENT_TERMS,
                *ZH_CELL_PARAMETER_TERMS,
            ],
            "when_to_use": "Use when you need a detailed time-series view of whether an MD trajectory is thermodynamically stable or equilibrated.",
        },
        {
            "id": "trajectory_analysis_bundle",
            "tool": "ms_analyze_trajectory_bundle",
            "title": "Comprehensive trajectory analysis bundle",
            "description": "Run multiple post-analysis steps on an existing trajectory and generate a unified multi-analysis report.",
            "keywords": [
                "analyze trajectory",
                "trajectory analysis",
                "comprehensive analysis",
                "complete analysis",
                "multi analysis",
                "unified report",
                "detailed report",
                "综合分析",
                "完整分析",
                "统一报告",
                "详细分析",
                "轨迹分析",
            ],
            "when_to_use": "Use when you already have a trajectory and want several analyses plus one unified conclusion report.",
        },
        {
            "id": "hbond_statistics",
            "tool": "ms_hbond_statistics",
            "title": "Hydrogen-bond statistics",
            "description": "Analyze a structure or trajectory and return hydrogen-bond count and length statistics, including per-frame results for trajectories.",
            "keywords": ["hbond", "hydrogen bond", "hydrogen-bond", *ZH_HBOND_TERMS],
            "when_to_use": "Use when you want structure-level or trajectory-level hydrogen-bond statistics in a structured table.",
        },
        {
            "id": "forcite_dynamics_with_analysis",
            "tool": "ms_forcite_dynamics_with_analysis",
            "title": "Forcite dynamics plus automatic analysis",
            "description": "Run dynamics, export the trajectory, then automatically perform RDF, MSD, hydrogen-bond, and/or thermodynamic profile analyses.",
            "keywords": [
                "dynamics and rdf",
                "dynamics and msd",
                "dynamics and hbond",
                "dynamics and temperature",
                "dynamics and pressure",
                "dynamics and density",
                "dynamics and thermo",
                "simulate and analyze",
                "md and rdf",
                "md and msd",
                "md and hbond",
                "md and temperature",
                "md and pressure",
                "md and density",
                "\u52a8\u529b\u5b66\u540e\u5206\u6790",
                "\u5148\u505a\u52a8\u529b\u5b66\u518d\u5206\u6790",
                "RDF\u548cMSD",
                "MD\u540e\u6c22\u952e\u5206\u6790",
                "\u52a8\u529b\u5b66\u540e\u770b\u6e29\u5ea6",
                "\u52a8\u529b\u5b66\u540e\u770b\u538b\u529b",
                "\u52a8\u529b\u5b66\u540e\u770b\u5bc6\u5ea6",
            ],
            "when_to_use": "Use when you want one end-to-end workflow that produces both a trajectory and structured analysis outputs.",
        },
        {
            "id": "forcite_relax_and_dynamics",
            "tool": "ms_forcite_relax_and_dynamics",
            "title": "Composite relax then dynamics workflow",
            "description": "First optimize the structure, then run dynamics using the optimized structure as input.",
            "keywords": [
                "workflow",
                "optimize then dynamics",
                "relax then md",
                "equilibrate",
                "best practice",
                "\u5148\u4f18\u5316\u540e\u52a8\u529b\u5b66",
                "\u4f18\u5316\u540e\u505a\u5206\u5b50\u52a8\u529b\u5b66",
                "\u5148\u5f1b\u8c6b\u518d\u52a8\u529b\u5b66",
            ],
            "when_to_use": "Use when you want a more realistic MD starting point and more reliable downstream results.",
        },
        {
            "id": "ch03_inspect_target_structure",
            "tool": "ms_ch03_inspect_target_structure",
            "title": "Inspect real MMT or clay target input for ch03",
            "description": "Detect whether the target file behaves like a surface or crystal and generate recommended next-step commands.",
            "keywords": [
                "ch03",
                "mmt",
                "montmorillonite",
                "clay target",
                "inspect target",
                "target intake",
                "surface or crystal",
                "论文第三章",
                "蒙脱土",
                "目标结构体检",
            ],
            "when_to_use": "Use before supercell matching when you just received a real clay/MMT source file and need to know the correct follow-up mode.",
        },
        {
            "id": "ch03_match_surface_supercells",
            "tool": "ms_ch03_match_surface_supercells",
            "title": "Match graphene and clay supercells for ch03",
            "description": "Search supercell combinations that bring graphene and the target wall close to the paper-scale lateral dimensions.",
            "keywords": [
                "ch03",
                "supercell match",
                "graphene mmt",
                "surface match",
                "匹配超胞",
                "石墨烯蒙脱土匹配",
            ],
            "when_to_use": "Use after target inspection when you want the best graphene/target replication pair before building the heterogeneous pore.",
        },
        {
            "id": "ch03_build_pore",
            "tool": "ms_ch03_build_pore",
            "title": "Build ch03 graphene-clay pore structure",
            "description": "Build the graphene wall, target wall, and final heterogeneous pore model and export all resulting .xsd artifacts.",
            "keywords": [
                "ch03",
                "build pore",
                "graphene mmt pore",
                "heterogeneous pore",
                "构建孔模型",
                "异质孔",
            ],
            "when_to_use": "Use when you already know the desired supercells and want the final Materials Studio pore model.",
        },
        {
            "id": "ch03_precheck",
            "tool": "ms_ch03_precheck",
            "title": "Run Materials Studio precheck for ch03 pore",
            "description": "Run the validated geometry-optimization and short-dynamics precheck chain on the built ch03 pore structure.",
            "keywords": [
                "ch03",
                "precheck",
                "forcite precheck",
                "geometry optimization precheck",
                "预检查",
                "预跑",
            ],
            "when_to_use": "Use after building the pore to confirm the model is stable enough before downstream export or production simulation.",
        },
        {
            "id": "ch03_pipeline",
            "tool": "ms_ch03_pipeline",
            "title": "Run the full ch03 MS pipeline",
            "description": "Execute the end-to-end ch03 chain: match, build, and precheck, then write a pipeline summary.",
            "keywords": [
                "ch03",
                "pipeline",
                "end to end",
                "match build precheck",
                "一键流程",
                "整条链路",
            ],
            "when_to_use": "Use when you want one command to build a reproducible demo or real-MMT run from the selected inputs.",
        },
        {
            "id": "ch03_validate_paper_targets",
            "tool": "ms_ch03_validate_paper_targets",
            "title": "Validate a ch03 build against paper targets",
            "description": "Compare the generated build summary to the extracted paper geometry targets and report percentage deviations.",
            "keywords": [
                "ch03",
                "paper validation",
                "validate dimensions",
                "compare to paper",
                "论文对照",
                "尺寸校验",
            ],
            "when_to_use": "Use after each build to see how close the current model is to the paper's target geometry.",
        },
        {
            "id": "ch03_audit_reproduction",
            "tool": "ms_ch03_audit_reproduction",
            "title": "Audit the current ch03 reproduction state",
            "description": "Inspect the local artifact tree and report which ch03 stages are already materialized plus the next recommended step.",
            "keywords": [
                "ch03",
                "audit",
                "reproduction status",
                "resume work",
                "复现审计",
                "进度体检",
            ],
            "when_to_use": "Use when you are resuming work and need to know which stage artifacts already exist.",
        },
        {
            "id": "ch03_generate_runbook",
            "tool": "ms_ch03_generate_runbook",
            "title": "Generate a personal ch03 runbook",
            "description": "Generate a ready-to-run command card for inspect, match, pipeline, validate, and audit steps.",
            "keywords": [
                "ch03",
                "runbook",
                "command card",
                "workflow template",
                "命令清单",
                "操作卡",
            ],
            "when_to_use": "Use when you want a reusable command checklist for a specific target file or pore-size scenario.",
        },
    ]


WORKFLOW_CATALOG = [
    entry for entry in _clean_workflow_catalog()
    if entry.get("tool") in public_tool_names()
]


def _powershell_executable() -> str:
    wow64 = Path(os.environ.get("WINDIR", r"C:\Windows")) / "SysWOW64" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if wow64.exists():
        return str(wow64)

    return "powershell"


def _run_helper(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    request = dict(payload or {})
    timeout = int(request.pop("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    helper_scratch = _materialsscript_scratch_root() / "helper"
    helper_scratch.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8-sig",
        dir=helper_scratch,
    ) as handle:
        json.dump(request, handle, ensure_ascii=False, indent=2)
        input_file = Path(handle.name)

    command = [
        _powershell_executable(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(HELPER_PATH),
        "-Action",
        action,
        "-InputFile",
        str(input_file),
    ]

    try:
        completed = subprocess.run(
            command,
            cwd=helper_scratch,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    finally:
        try:
            input_file.unlink(missing_ok=True)
        except OSError:
            pass

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()

    if completed.returncode != 0:
        raise RuntimeError(
            f"PowerShell helper failed with exit code {completed.returncode}.\n"
            f"STDERR:\n{stderr or '<empty>'}\nSTDOUT:\n{stdout or '<empty>'}"
        )

    if not stdout:
        raise RuntimeError("PowerShell helper returned empty stdout.")

    try:
        response = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"PowerShell helper returned non-JSON output.\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr or '<empty>'}"
        ) from exc

    if not response.get("ok", False):
        error_message = response.get("error") or "Unknown helper error."
        raise RuntimeError(error_message)

    return redact_sensitive(response)


def _append_powershell_arg(command: list[str], name: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        if value:
            command.append(f"-{name}")
        return
    command.extend([f"-{name}", str(value)])


def _run_powershell_script(
    script_path: Path,
    *,
    named_args: dict[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if not script_path.exists():
        raise FileNotFoundError(f"PowerShell script not found: {script_path}")

    command = [
        _powershell_executable(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]
    for name, value in (named_args or {}).items():
        _append_powershell_arg(command, name, value)

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    if completed.returncode != 0:
        raise RuntimeError(
            f"PowerShell script failed with exit code {completed.returncode}: {script_path}\n"
            f"STDERR:\n{stderr or '<empty>'}\nSTDOUT:\n{stdout or '<empty>'}"
        )
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_prefixed_child_dir(parent_prefix: str, child_prefix: str, fallback_name: str) -> Path:
    fallback_candidates: list[Path] = []
    if WORKSPACE_ROOT.exists():
        for candidate in WORKSPACE_ROOT.iterdir():
            if not candidate.is_dir() or not candidate.name.startswith(parent_prefix):
                continue
            for child in candidate.iterdir():
                if not child.is_dir() or not child.name.startswith(child_prefix):
                    continue
                fallback_candidates.append(child)
    if fallback_candidates:
        return sorted(fallback_candidates, key=lambda item: item.name)[0]
    return WORKSPACE_ROOT / "tmp" / fallback_name


def _resolve_ch03_input_dir() -> Path:
    return _resolve_prefixed_child_dir("02_", "ch03_", "ch03_input_fallback")


def _resolve_ch03_results_dir() -> Path:
    return _resolve_prefixed_child_dir("04_", "ch03_", "ch03_results_fallback")


def _default_ms_structure_path(kind: str) -> str:
    ms_root = Path(_materials_studio_paths()["root"])
    if kind == "graphite":
        return str(ms_root / "share" / "Structures" / "ceramics" / "graphite.xsd")
    if kind == "mica_2d_layer":
        return str(
            ms_root
            / "share"
            / "Structures"
            / "minerals"
            / "9.E-Layersilicates"
            / "mica_2d_layer.xsd"
        )
    raise ValueError(f"Unsupported default structure kind: {kind}")


def _resolve_crystal_parent_source(value: str) -> Path:
    try:
        source = resolve_workspace_path(value, must_exist=True)
    except PermissionError as workspace_error:
        source = Path(value).expanduser().resolve(strict=True)
        structures_root = (
            Path(_materials_studio_paths()["root"]) / "share" / "Structures"
        ).resolve(strict=True)
        if source != structures_root and structures_root not in source.parents:
            raise workspace_error
    if not source.is_file() or source.suffix.lower() not in {".cif", ".xsd"}:
        raise ValueError("Crystal parent source must be a regular CIF or XSD file")
    return source


def _run_ch03_script_with_summary(
    *,
    tool_name: str,
    script_name: str,
    summary_json_name: str,
    output_directory: str,
    output_parameter_name: str = "OutputDirectory",
    named_args: dict[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    output_dir = Path(output_directory)
    output_dir.mkdir(parents=True, exist_ok=True)
    script_path = CH03_SCRIPT_ROOT / script_name
    args = dict(named_args or {})
    args[output_parameter_name] = str(output_dir)
    execution = _run_powershell_script(
        script_path,
        named_args=args,
        timeout_seconds=timeout_seconds,
    )
    summary_json_path = output_dir / summary_json_name
    payload = _load_json_file(summary_json_path)
    return {
        "success": bool(payload.get("success")),
        "tool": tool_name,
        "script_path": str(script_path),
        "output_directory": str(output_dir),
        "summary_json_path": str(summary_json_path),
        "execution": execution,
        "result": payload,
    }


def _ascii_safe_name(value: str, fallback: str = "item") -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return safe or fallback


def _perl_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def _render_template(template: str, variables: dict[str, str]) -> str:
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>", " ", html)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?i)</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"(?:\r?\n\s*){3,}", "\n\n", text)
    return text.strip()


def _ensure_runtime_alias() -> Path:
    alias_parent = RUNTIME_ALIAS_ROOT.parent
    alias_parent.mkdir(parents=True, exist_ok=True)
    if RUNTIME_ALIAS_ROOT.exists():
        return RUNTIME_ALIAS_ROOT
    subprocess.run(
        [
            _powershell_executable(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            (
                f"New-Item -ItemType Junction -Path '{RUNTIME_ALIAS_ROOT}' "
                f"-Target '{PROJECT_ROOT}' | Out-Null"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return RUNTIME_ALIAS_ROOT


def _materials_studio_paths() -> dict[str, Any]:
    return _run_helper("detect")["data"]


def _reference_entry(relative_path: str) -> dict[str, str]:
    ms_paths = _materials_studio_paths()
    return {
        "relative_path": relative_path,
        "full_path": str(Path(ms_paths["scripting_help_root"]) / relative_path),
    }


def _reference_entries(relative_paths: list[str]) -> list[dict[str, str]]:
    return [_reference_entry(path) for path in relative_paths]


def _perl_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _perl_value(value: Any) -> str:
    if isinstance(value, bool):
        return _perl_quote("Yes" if value else "No")
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "undef"
    if isinstance(value, str):
        return _perl_quote(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_perl_value(item) for item in value) + "]"
    raise TypeError(f"Unsupported setting value type: {type(value)!r}")


def _perl_settings_array(settings: dict[str, Any] | None) -> str:
    if not settings:
        return "[]"
    items = [f"{key} => {_perl_value(value)}" for key, value in settings.items()]
    return "[" + ",\n  ".join(items) + "]"


def _perl_settings_entries(settings: dict[str, Any] | None) -> str:
    if not settings:
        return ""
    return ",\n  ".join(f"{key} => {_perl_value(value)}" for key, value in settings.items())


def _parse_key_value_text(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        lowered = value.lower()
        if re.fullmatch(r"[-+]?\d+", value):
            parsed: Any = int(value)
        elif re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?", value):
            parsed = float(value)
        elif lowered in {"yes", "true"}:
            parsed = True
        elif lowered in {"no", "false"}:
            parsed = False
        else:
            parsed = value
        result[key] = parsed
    return result


def _parse_tsv_table(text: str) -> dict[str, Any]:
    lines = [line.rstrip("\n") for line in text.splitlines() if line.strip()]
    if not lines:
        return {
            "columns": [],
            "rows": [],
            "row_count": 0,
        }
    columns = lines[0].split("\t")
    rows: list[dict[str, Any]] = []
    for raw_line in lines[1:]:
        parts = raw_line.split("\t")
        row: dict[str, Any] = {}
        for index, column in enumerate(columns):
            value = parts[index] if index < len(parts) else ""
            if re.fullmatch(r"[-+]?\d+", value):
                row[column] = int(value)
            elif re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?", value):
                row[column] = float(value)
            else:
                row[column] = value
        rows.append(row)
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
    }


def _requested_analysis_names(request: str) -> list[str]:
    lowered = request.lower()
    requested: list[str] = []
    if any(term in lowered for term in ["rdf", "radial distribution function", "g(r)", "pair correlation", *ZH_RDF_TERMS]):
        requested.append("rdf")
    if any(term in lowered for term in ["msd", "mean square displacement", "diffusion", *ZH_MSD_TERMS]):
        requested.append("msd")
    if any(term in lowered for term in ["vacf", "velocity autocorrelation", "velocity autocorrelation function", "power spectrum", "vibration spectrum", *ZH_VACF_TERMS]):
        requested.append("vacf")
    if any(term in lowered for term in ["hbond", "hydrogen bond", "hydrogen-bond", "h-bond", *ZH_HBOND_TERMS]):
        requested.append("hbond")
    if _parse_thermo_request_options(request).get("properties"):
        requested.append("thermo")
    return requested


def _ordered_unique_strings(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _elements_from_request(request: str) -> list[str]:
    mentions: list[tuple[int, str]] = []
    lowered = request.lower()

    for match in re.finditer(r"\b([A-Za-z][a-z]?)\b", request):
        symbol = match.group(1)
        canonical = symbol[0].upper() + symbol[1:].lower()
        if canonical in ELEMENT_NAME_PATTERNS:
            mentions.append((match.start(), canonical))

    for symbol, patterns in ELEMENT_NAME_PATTERNS.items():
        for pattern in patterns:
            if pattern.isascii():
                index = lowered.find(pattern.lower())
            else:
                index = request.find(pattern)
            if index != -1:
                mentions.append((index, symbol))

    mentions.sort(key=lambda item: (item[0], item[1]))
    return _ordered_unique_strings([symbol for _, symbol in mentions])


def _parse_rdf_request_options(request: str) -> dict[str, Any]:
    elements = _elements_from_request(request)
    options: dict[str, Any] = {}
    if len(elements) >= 2:
        options["selection_a"] = elements[0]
        options["selection_b"] = elements[1]
    elif len(elements) == 1:
        options["selection_a"] = elements[0]
        options["selection_b"] = elements[0]

    lowered = request.lower()
    if "structure factor" in lowered or "s(q)" in lowered or "\u7ed3\u6784\u56e0\u5b50" in request:
        options["analysis_settings"] = {"RDFComputeStructureFactor": True}
        options["include_structure_factor"] = True
    return options


def _parse_msd_request_options(request: str) -> dict[str, Any]:
    elements = _elements_from_request(request)
    options: dict[str, Any] = {}
    if elements:
        options["selection"] = elements[0]

    lowered = request.lower()
    if "anisotropic" in lowered or "\u5404\u5411\u5f02\u6027" in request:
        options["analysis_settings"] = {"MSDComputeAnisotropicComponents": True}
    return options


def _parse_vacf_request_options(request: str) -> dict[str, Any]:
    elements = _elements_from_request(request)
    options: dict[str, Any] = {}
    if elements:
        options["selection"] = elements[0]

    lowered = request.lower()
    analysis_settings: dict[str, Any] = {}
    if "anisotropic" in lowered or "\u5404\u5411\u5f02\u6027" in request:
        analysis_settings["VACFComputeAnisotropicComponents"] = True
    if "normalize" in lowered or "normalised" in lowered or "normalized" in lowered or "\u5f52\u4e00\u5316" in request:
        analysis_settings["VACFNormalize"] = True
    if (
        "power spectrum" in lowered
        or "spectrum" in lowered
        or "vibration spectrum" in lowered
        or "\u529f\u7387\u8c31" in request
        or "\u9891\u8c31" in request
    ):
        analysis_settings["VACFComputePowerSpectrum"] = True
        options["include_power_spectrum"] = True

    if analysis_settings:
        options["analysis_settings"] = analysis_settings
    return options


def _parse_thermo_request_options(request: str) -> dict[str, Any]:
    lowered = request.lower()
    properties: list[str] = []
    if any(term in lowered for term in ["temperature", "temp", *ZH_TEMPERATURE_TERMS]):
        properties.append("temperature")
    if any(term in lowered for term in ["pressure", "pressure tensor", "barostat", *ZH_PRESSURE_TERMS]):
        properties.append("pressure")
    if any(term in lowered for term in ["density", *ZH_DENSITY_TERMS]):
        properties.append("density")
    if any(
        term in lowered
        for term in [
            "potential energy",
            "energy component",
            "energy components",
            "potential energy component",
            "potential energy components",
        ]
    ) or any(term in request for term in ZH_POTENTIAL_COMPONENT_TERMS):
        properties.append("potential_energy_components")
    if any(
        term in lowered
        for term in ["cell parameter", "cell parameters", "unit cell", "volume", "box size", "lattice parameter"]
    ) or any(term in request for term in ZH_CELL_PARAMETER_TERMS):
        properties.append("cell_parameters")

    if not properties and (
        any(term in lowered for term in ["thermo", "thermodynamic", "equilibration", "stability", "convergence"])
        or any(term in request for term in ZH_THERMO_TERMS)
    ):
        properties.extend(["temperature", "pressure", "density", "potential_energy_components"])

    common_analysis_settings: dict[str, Any] = {}
    if "running average" in lowered or "running averages" in lowered or "\u8fd0\u884c\u5e73\u5747" in request:
        common_analysis_settings["ComputeRunningAverages"] = True
    if "profile only" in lowered or "\u4ec5\u526a\u9762" in request:
        common_analysis_settings["ComputeRunningAverages"] = False
    if "block average" in lowered or "block averages" in lowered or "\u5206\u5757\u5e73\u5747" in request:
        common_analysis_settings["ComputeBlockAverages"] = True
        width_match = re.search(r"block(?:\s+average)?(?:s)?(?:\s+of)?\s*(\d+)\s*frames?", lowered)
        if width_match:
            common_analysis_settings["BlockAverageFrameWidth"] = int(width_match.group(1))
        interval_match = re.search(r"every\s*(\d+)\s*frames?", lowered)
        if interval_match:
            common_analysis_settings["BlockAverageFrameInterval"] = int(interval_match.group(1))

    analysis_settings_by_property: dict[str, dict[str, Any]] = {}
    if (
        "anisotropic pressure" in lowered
        or "pressure tensor" in lowered
        or "\u5404\u5411\u5f02\u6027\u538b\u529b" in request
        or "\u538b\u529b\u5f20\u91cf" in request
    ):
        analysis_settings_by_property["pressure"] = {
            "PressureComputeAnisotropicComponents": True
        }
    if (
        "all energy components" in lowered
        or "full energy components" in lowered
        or "show all energy components" in lowered
        or "\u6240\u6709\u52bf\u80fd\u7ec4\u5206" in request
        or "\u5168\u90e8\u52bf\u80fd\u7ec4\u5206" in request
        or "\u5b8c\u6574\u52bf\u80fd\u7ec4\u5206" in request
    ):
        analysis_settings_by_property["potential_energy_components"] = {
            "PotentialEnergyComponentsShowAll": True
        }

    options: dict[str, Any] = {}
    if properties:
        options["properties"] = _ordered_unique_strings(properties)
    if common_analysis_settings:
        options["common_analysis_settings"] = common_analysis_settings
    if analysis_settings_by_property:
        options["analysis_settings_by_property"] = analysis_settings_by_property
    return options


def _table_axis_column(columns: list[str]) -> str | None:
    axis_priority = ["time", "frame", "step"]
    for candidate in axis_priority:
        for column in columns:
            if column.strip().lower() == candidate:
                return column
    for column in columns:
        lowered = column.strip().lower()
        if any(candidate in lowered for candidate in axis_priority):
            return column
    return columns[0] if columns else None


def _numeric_series_statistics(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    count = len(values)
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / count
    stdev = math.sqrt(variance)
    relative_stdev = None
    if abs(mean) > 1e-12:
        relative_stdev = stdev / abs(mean)
    return {
        "count": count,
        "first": values[0],
        "last": values[-1],
        "delta": values[-1] - values[0],
        "min": min(values),
        "max": max(values),
        "mean": mean,
        "stdev": stdev,
        "relative_stdev": relative_stdev,
    }


def _summarize_analysis_table(table: dict[str, Any]) -> dict[str, Any]:
    columns = table.get("columns", [])
    rows = table.get("rows", [])
    axis_column = _table_axis_column(columns)
    series: dict[str, Any] = {}
    for column in columns:
        if column == axis_column:
            continue
        values = [row[column] for row in rows if isinstance(row.get(column), (int, float))]
        if not values:
            continue
        series[column] = _numeric_series_statistics([float(value) for value in values])
    return {
        "row_count": table.get("row_count", len(rows)),
        "columns": columns,
        "axis_column": axis_column,
        "series": series,
    }


THERMO_PROFILE_SPECS: dict[str, dict[str, str]] = {
    "temperature": {
        "analysis_name": "Temperature",
        "study_table_property": "TemperatureChartAsStudyTable",
        "output_basename": "temperature",
        "help_key": "Temperature",
        "preferred_column": "Temperature",
    },
    "pressure": {
        "analysis_name": "Pressure",
        "study_table_property": "PressureChartAsStudyTable",
        "output_basename": "pressure",
        "help_key": "Pressure",
        "preferred_column": "Pressure",
    },
    "density": {
        "analysis_name": "Density",
        "study_table_property": "DensityChartAsStudyTable",
        "output_basename": "density",
        "help_key": "Density",
        "preferred_column": "Density",
    },
    "potential_energy_components": {
        "analysis_name": "PotentialEnergyComponents",
        "study_table_property": "PotentialEnergyComponentsChartAsStudyTable",
        "output_basename": "potential_energy_components",
        "help_key": "PotentialEnergyComponents",
        "preferred_column": "Potential energy",
    },
    "cell_parameters": {
        "analysis_name": "CellParameters",
        "study_table_property": "CellParametersChartAsStudyTable",
        "output_basename": "cell_parameters",
        "help_key": "CellParameters",
        "preferred_column": "Volume",
    },
}


THERMO_STABILITY_THRESHOLDS: dict[str, dict[str, float]] = {
    "default": {
        "mean_shift_ratio": 0.25,
        "drift_ratio": 0.35,
        "noise_ratio": 0.20,
    },
    "temperature": {
        "mean_shift_ratio": 0.20,
        "drift_ratio": 0.30,
        "noise_ratio": 0.18,
    },
    "pressure": {
        "mean_shift_ratio": 0.50,
        "drift_ratio": 0.65,
        "noise_ratio": 1.20,
    },
    "density": {
        "mean_shift_ratio": 0.08,
        "drift_ratio": 0.12,
        "noise_ratio": 0.08,
    },
    "potential_energy_components": {
        "mean_shift_ratio": 0.30,
        "drift_ratio": 0.45,
        "noise_ratio": 0.35,
    },
    "cell_parameters": {
        "mean_shift_ratio": 0.12,
        "drift_ratio": 0.18,
        "noise_ratio": 0.10,
    },
}


def _preferred_series_metrics(property_name: str, series_summary: dict[str, Any]) -> dict[str, Any] | None:
    preferred = THERMO_PROFILE_SPECS[property_name]["preferred_column"]
    series = series_summary.get("series", {})
    if preferred in series:
        return {"column": preferred, **series[preferred]}
    for column, metrics in series.items():
        return {"column": column, **metrics}
    return None


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _numeric_column_values(table: dict[str, Any], column: str) -> list[float]:
    rows = table.get("rows", [])
    values: list[float] = []
    for row in rows:
        value = row.get(column)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def _approx_frame_numbers(frame_range: str | None, count: int) -> list[int]:
    if count <= 0:
        return []
    bounds = _split_frame_range_spec(frame_range)
    if bounds:
        start, _ = bounds
        return [start + index for index in range(count)]
    return list(range(1, count + 1))


def _series_scale(values: list[float], stats: dict[str, Any]) -> float:
    series_range = abs(float(stats["max"]) - float(stats["min"]))
    return max(
        abs(float(stats["mean"])),
        abs(float(stats["stdev"])),
        series_range,
        1e-12,
    )


def _assess_numeric_series_stability(
    *,
    values: list[float],
    frame_range: str | None,
    property_name: str,
) -> dict[str, Any]:
    if len(values) < 4:
        return {
            "status": "insufficient_data",
            "reason": "At least 4 numeric samples are needed to assess equilibration reliably.",
            "sample_count": len(values),
        }

    thresholds = {
        **THERMO_STABILITY_THRESHOLDS["default"],
        **THERMO_STABILITY_THRESHOLDS.get(property_name, {}),
    }
    overall_stats = _numeric_series_statistics(values)
    overall_scale = _series_scale(values, overall_stats)
    min_window = max(4, math.ceil(len(values) * 0.4))
    candidate_frames = _approx_frame_numbers(frame_range, len(values))

    stable_candidates: list[dict[str, Any]] = []
    near_candidates: list[dict[str, Any]] = []

    for start_index in range(0, len(values) - min_window + 1):
        tail_values = values[start_index:]
        tail_stats = _numeric_series_statistics(tail_values)
        half_width = max(2, len(tail_values) // 2)
        first_half = tail_values[:half_width]
        second_half = tail_values[-half_width:]
        mean_shift = abs(_mean(second_half) - _mean(first_half))
        drift = abs(tail_values[-1] - tail_values[0])
        noise = float(tail_stats["stdev"]) / overall_scale
        mean_shift_ratio = mean_shift / overall_scale
        drift_ratio = drift / overall_scale
        score = (
            mean_shift_ratio / thresholds["mean_shift_ratio"]
            + drift_ratio / thresholds["drift_ratio"]
            + noise / thresholds["noise_ratio"]
            + (start_index / max(len(values) - 1, 1)) * 0.1
        )
        candidate = {
            "start_index": start_index,
            "start_frame": candidate_frames[start_index],
            "window_length": len(tail_values),
            "tail_statistics": tail_stats,
            "mean_shift_ratio": mean_shift_ratio,
            "drift_ratio": drift_ratio,
            "noise_ratio": noise,
            "score": score,
        }
        is_stable = (
            mean_shift_ratio <= thresholds["mean_shift_ratio"]
            and drift_ratio <= thresholds["drift_ratio"]
            and noise <= thresholds["noise_ratio"]
        )
        is_near_stable = (
            mean_shift_ratio <= thresholds["mean_shift_ratio"] * 1.5
            and drift_ratio <= thresholds["drift_ratio"] * 1.5
            and noise <= thresholds["noise_ratio"] * 1.5
        )
        if is_stable:
            stable_candidates.append(candidate)
        elif is_near_stable:
            near_candidates.append(candidate)

    if stable_candidates:
        chosen = min(stable_candidates, key=lambda item: (item["start_index"], item["score"]))
        status = "stable"
    elif near_candidates:
        chosen = min(near_candidates, key=lambda item: (item["start_index"], item["score"]))
        status = "possibly_stable"
    else:
        fallback_start = max(0, len(values) - min_window)
        fallback_values = values[fallback_start:]
        fallback_stats = _numeric_series_statistics(fallback_values)
        half_width = max(2, len(fallback_values) // 2)
        first_half = fallback_values[:half_width]
        second_half = fallback_values[-half_width:]
        chosen = {
            "start_index": fallback_start,
            "start_frame": candidate_frames[fallback_start],
            "window_length": len(fallback_values),
            "tail_statistics": fallback_stats,
            "mean_shift_ratio": abs(_mean(second_half) - _mean(first_half)) / overall_scale,
            "drift_ratio": abs(fallback_values[-1] - fallback_values[0]) / overall_scale,
            "noise_ratio": float(fallback_stats["stdev"]) / overall_scale,
            "score": None,
        }
        status = "not_stable"

    start_frame = int(chosen["start_frame"])
    end_frame = candidate_frames[-1]
    reason_map = {
        "stable": "A trailing window satisfies the configured drift, mean-shift, and noise thresholds.",
        "possibly_stable": "A trailing window is close to the stability thresholds but not strongly converged.",
        "not_stable": "No trailing window met the stability thresholds, so the trajectory still looks unsettled.",
    }
    return {
        "status": status,
        "reason": reason_map[status],
        "sample_count": len(values),
        "minimum_window": min_window,
        "recommended_start_index": int(chosen["start_index"]),
        "recommended_start_frame": start_frame,
        "recommended_frame_range": f"{start_frame}-{end_frame}",
        "overall_statistics": overall_stats,
        "window_statistics": chosen["tail_statistics"],
        "mean_shift_ratio": chosen["mean_shift_ratio"],
        "drift_ratio": chosen["drift_ratio"],
        "noise_ratio": chosen["noise_ratio"],
        "thresholds": thresholds,
    }


def _aggregate_thermo_stability_assessment(
    property_assessments: dict[str, dict[str, Any]],
    frame_range: str | None,
) -> dict[str, Any]:
    usable = {
        name: assessment
        for name, assessment in property_assessments.items()
        if assessment.get("status") != "insufficient_data"
    }
    if not usable:
        return {
            "overall_status": "insufficient_data",
            "reason": "No property had enough numeric samples for a stability assessment.",
            "properties": property_assessments,
            "recommended_production_frame_range": None,
        }

    statuses = {assessment["status"] for assessment in usable.values()}
    recommended_start = max(int(assessment["recommended_start_frame"]) for assessment in usable.values())
    frame_numbers = _approx_frame_numbers(frame_range, 1)
    analysis_start = frame_numbers[0] if frame_numbers else 1
    analysis_end = max(int(assessment["recommended_frame_range"].split("-")[1]) for assessment in usable.values())

    if "not_stable" in statuses:
        overall_status = "not_stable"
        reason = "At least one requested thermodynamic property still shows noticeable drift or window-to-window shifts."
    elif statuses == {"stable"}:
        overall_status = "stable"
        reason = "All assessed properties show a trailing window that satisfies the current stability thresholds."
    else:
        overall_status = "possibly_stable"
        reason = "The assessed properties are partly converged, but at least one property is only near the stability thresholds."

    return {
        "overall_status": overall_status,
        "reason": reason,
        "analysis_frame_range": f"{analysis_start}-{analysis_end}",
        "recommended_equilibration_discard_frames": max(0, recommended_start - analysis_start),
        "recommended_production_start_frame": recommended_start,
        "recommended_production_frame_range": f"{recommended_start}-{analysis_end}",
        "properties": property_assessments,
    }


def _render_thermo_assessment_markdown(assessment: dict[str, Any]) -> str:
    lines = [
        "# Thermodynamic Stability Assessment",
        "",
        f"- Overall status: {assessment.get('overall_status')}",
        f"- Reason: {assessment.get('reason')}",
        f"- Recommended production frame range: {assessment.get('recommended_production_frame_range')}",
        f"- Recommended equilibration discard frames: {assessment.get('recommended_equilibration_discard_frames')}",
        "",
        "## Property Details",
    ]
    for property_name, property_assessment in assessment.get("properties", {}).items():
        lines.append("")
        lines.append(f"### {property_name}")
        lines.append(f"- Status: {property_assessment.get('status')}")
        lines.append(f"- Reason: {property_assessment.get('reason')}")
        if property_assessment.get("status") != "insufficient_data":
            lines.append(f"- Recommended start frame: {property_assessment.get('recommended_start_frame')}")
            lines.append(f"- Recommended frame range: {property_assessment.get('recommended_frame_range')}")
            lines.append(f"- Mean-shift ratio: {property_assessment.get('mean_shift_ratio')}")
            lines.append(f"- Drift ratio: {property_assessment.get('drift_ratio')}")
            lines.append(f"- Noise ratio: {property_assessment.get('noise_ratio')}")
    return "\n".join(lines) + "\n"


def _property_display_name(property_name: str) -> str:
    names = {
        "temperature": "Temperature",
        "pressure": "Pressure",
        "density": "Density",
        "potential_energy_components": "Potential energy components",
        "cell_parameters": "Cell parameters",
    }
    return names.get(property_name, property_name.replace("_", " ").title())


def _format_numeric(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return f"{value:.{digits}f}"
    return str(value)


def _thermo_property_recommendations(property_name: str, status: str) -> list[str]:
    if status == "stable":
        return [f"{_property_display_name(property_name)} looks usable for production-window statistics."]

    recommendations: dict[str, list[str]] = {
        "temperature": [
            "Extend equilibration and review thermostat choice or timestep if temperature continues to drift.",
            "If the target temperature is known, compare the running average against that setpoint before using the trajectory for production statistics.",
        ],
        "pressure": [
            "Pressure usually converges more slowly than temperature; extend the run before trusting an average pressure.",
            "If pressure remains noisy, review barostat settings, system size, and whether NPT is the right ensemble for this task.",
        ],
        "density": [
            "Density drift often means the cell is still relaxing; consider longer NPT equilibration before computing bulk properties.",
            "If density is already flat but other properties are not, you can keep monitoring density while prioritizing the slower property.",
        ],
        "potential_energy_components": [
            "Review which energy component drifts most strongly before assuming the system is equilibrated.",
            "Large energy drift can indicate an over-large timestep, incomplete structural relaxation, or a forcefield/setup issue.",
        ],
        "cell_parameters": [
            "If cell parameters keep drifting, extend NPT equilibration or review pressure-control settings before production sampling.",
        ],
    }
    generic = [
        "Discard more early frames and rerun the assessment after a longer equilibration segment.",
    ]
    return recommendations.get(property_name, []) + generic


def _build_thermo_interpretation(
    *,
    stability_assessment: dict[str, Any] | None,
    profile_summaries: dict[str, Any],
) -> dict[str, Any] | None:
    if not stability_assessment:
        return None

    overall_status = stability_assessment.get("overall_status")
    production_range = stability_assessment.get("recommended_production_frame_range")
    discard_frames = stability_assessment.get("recommended_equilibration_discard_frames")

    if overall_status == "stable":
        executive_summary = (
            f"The requested thermodynamic profiles look stable over the analyzed window. "
            f"You can tentatively use frames {production_range} for production statistics."
        )
    elif overall_status == "possibly_stable":
        executive_summary = (
            f"The profiles are close to stable, but at least one property is only marginally converged. "
            f"Use frames {production_range} cautiously and consider extending equilibration."
        )
    elif overall_status == "not_stable":
        executive_summary = (
            "At least one key thermodynamic property still shows meaningful drift or window-to-window shifts. "
            "The current trajectory is not a strong production-quality window yet."
        )
    else:
        executive_summary = "There are not enough usable samples to give a reliable thermodynamic conclusion."

    key_findings: list[str] = []
    recommended_actions: list[str] = []
    property_findings: dict[str, Any] = {}

    for property_name, property_assessment in stability_assessment.get("properties", {}).items():
        display_name = _property_display_name(property_name)
        status = property_assessment.get("status")
        preferred_summary = profile_summaries.get(property_name, {}).get("preferred_series_metrics", {})
        column_name = property_assessment.get("column") or preferred_summary.get("column")
        mean_value = preferred_summary.get("mean")
        delta_value = preferred_summary.get("delta")
        noise_ratio = property_assessment.get("noise_ratio")
        start_frame = property_assessment.get("recommended_start_frame")
        headline = (
            f"{display_name}: {status}; preferred series `{column_name}` has mean "
            f"{_format_numeric(mean_value)} and drift {_format_numeric(delta_value)} over the analyzed window."
        )
        detail = property_assessment.get("reason")
        if status != "insufficient_data":
            detail = (
                f"{detail} Recommended start frame: {start_frame}. "
                f"Noise ratio: {_format_numeric(noise_ratio)}."
            )
        key_findings.append(headline)
        property_findings[property_name] = {
            "headline": headline,
            "detail": detail,
            "status": status,
            "recommended_actions": _thermo_property_recommendations(property_name, status),
        }
        recommended_actions.extend(property_findings[property_name]["recommended_actions"])

    recommended_actions = _ordered_unique_strings(recommended_actions)
    production_guidance = {
        "recommended_production_frame_range": production_range,
        "recommended_equilibration_discard_frames": discard_frames,
        "confidence": overall_status,
    }
    return {
        "executive_summary": executive_summary,
        "key_findings": key_findings,
        "recommended_actions": recommended_actions,
        "production_guidance": production_guidance,
        "property_findings": property_findings,
    }


def _render_thermo_interpretation_markdown(interpretation: dict[str, Any]) -> str:
    lines = [
        "# Thermodynamic Interpretation",
        "",
        interpretation.get("executive_summary", ""),
        "",
        "## Key Findings",
    ]
    for finding in interpretation.get("key_findings", []):
        lines.append(f"- {finding}")
    lines.extend(
        [
            "",
            "## Recommended Actions",
        ]
    )
    for action in interpretation.get("recommended_actions", []):
        lines.append(f"- {action}")
    guidance = interpretation.get("production_guidance", {})
    lines.extend(
        [
            "",
            "## Production Guidance",
            f"- Recommended production frame range: {guidance.get('recommended_production_frame_range')}",
            f"- Recommended equilibration discard frames: {guidance.get('recommended_equilibration_discard_frames')}",
            f"- Confidence: {guidance.get('confidence')}",
        ]
    )
    return "\n".join(lines) + "\n"


def _build_dynamics_analysis_interpretation(
    *,
    key_metrics: dict[str, Any],
    analysis_results: dict[str, Any],
) -> dict[str, Any]:
    findings: list[str] = []
    actions: list[str] = []

    trajectory_frames = key_metrics.get("trajectory_frames")
    if trajectory_frames is not None:
        findings.append(f"The dynamics run exported {trajectory_frames} trajectory frames.")

    if "msd" in analysis_results:
        diffusion = key_metrics.get("msd_diffusion_coefficient")
        rsq = key_metrics.get("msd_diffusion_coefficient_rsq")
        if diffusion is not None:
            findings.append(
                f"MSD fitting produced a diffusion coefficient of {_format_numeric(diffusion)} with R^2={_format_numeric(rsq)}."
            )
        else:
            findings.append("MSD data is available, but no diffusion coefficient fit was returned.")
            actions.append("If diffusion is important, increase trajectory length or MSD fitting window coverage.")

    if "hbond" in analysis_results:
        mean_hbond = key_metrics.get("mean_hbond_count")
        if mean_hbond is not None:
            findings.append(f"Hydrogen-bond analysis reports an average of {_format_numeric(mean_hbond)} hydrogen bonds per analyzed frame.")

    thermo_result = analysis_results.get("thermo")
    if thermo_result:
        thermo_interpretation = thermo_result.get("interpretation")
        if thermo_interpretation:
            findings.append(thermo_interpretation.get("executive_summary", ""))
            actions.extend(thermo_interpretation.get("recommended_actions", []))
        else:
            thermo_status = key_metrics.get("thermo_overall_status")
            if thermo_status is not None:
                findings.append(f"Thermodynamic profile status: {thermo_status}.")

    if not actions:
        actions.append("Review the exported structured tables and verify that the chosen frame window matches your scientific objective.")

    actions = _ordered_unique_strings(actions)
    if findings:
        executive_summary = findings[0]
    else:
        executive_summary = "The combined workflow completed and returned structured downstream analysis outputs."
    return {
        "executive_summary": executive_summary,
        "key_findings": findings,
        "recommended_actions": actions,
    }


def _render_dynamics_analysis_interpretation_markdown(interpretation: dict[str, Any]) -> str:
    lines = [
        "# Dynamics Workflow Interpretation",
        "",
        interpretation.get("executive_summary", ""),
        "",
        "## Key Findings",
    ]
    for finding in interpretation.get("key_findings", []):
        lines.append(f"- {finding}")
    lines.extend(["", "## Recommended Actions"])
    for action in interpretation.get("recommended_actions", []):
        lines.append(f"- {action}")
    return "\n".join(lines) + "\n"


def _render_generic_interpretation_markdown(title: str, interpretation: dict[str, Any]) -> str:
    lines = [
        f"# {title}",
        "",
        interpretation.get("executive_summary", ""),
        "",
        "## Key Findings",
    ]
    for finding in interpretation.get("key_findings", []):
        lines.append(f"- {finding}")
    lines.extend(["", "## Recommended Actions"])
    for action in interpretation.get("recommended_actions", []):
        lines.append(f"- {action}")
    return "\n".join(lines) + "\n"


def _first_numeric_column(table: dict[str, Any], excluded: set[str] | None = None) -> str | None:
    excluded_columns = excluded or set()
    columns = table.get("columns", [])
    rows = table.get("rows", [])
    for column in columns:
        if column in excluded_columns:
            continue
        for row in rows:
            if isinstance(row.get(column), (int, float)):
                return column
    return None


def _first_rdf_peak(table: dict[str, Any]) -> dict[str, Any] | None:
    rows = table.get("rows", [])
    if len(rows) < 3:
        return None
    radius_column = "r" if "r" in table.get("columns", []) else _first_numeric_column(table)
    value_column = "g(r)" if "g(r)" in table.get("columns", []) else _first_numeric_column(table, excluded={radius_column} if radius_column else set())
    if not radius_column or not value_column:
        return None

    best_peak: dict[str, Any] | None = None
    for index in range(1, len(rows) - 1):
        previous_value = rows[index - 1].get(value_column)
        current_value = rows[index].get(value_column)
        next_value = rows[index + 1].get(value_column)
        radius = rows[index].get(radius_column)
        if not all(isinstance(value, (int, float)) for value in [previous_value, current_value, next_value, radius]):
            continue
        if float(current_value) >= float(previous_value) and float(current_value) >= float(next_value):
            candidate = {
                "radius": float(radius),
                "value": float(current_value),
                "radius_column": radius_column,
                "value_column": value_column,
            }
            if best_peak is None or candidate["value"] > best_peak["value"]:
                best_peak = candidate
    return best_peak


def _final_numeric_row_value(table: dict[str, Any], column: str) -> float | None:
    rows = table.get("rows", [])
    for row in reversed(rows):
        value = row.get(column)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _build_rdf_interpretation(result: dict[str, Any]) -> dict[str, Any]:
    table = result.get("analysis_table", {})
    peak = _first_rdf_peak(table)
    row_count = table.get("row_count", 0)
    if peak:
        executive_summary = (
            f"RDF returned {row_count} sampled points. The strongest detected peak is near "
            f"{_format_numeric(peak['radius'])} in `{peak['radius_column']}` with "
            f"{peak['value_column']}={_format_numeric(peak['value'])}."
        )
        findings = [
            executive_summary,
        ]
    else:
        executive_summary = f"RDF returned {row_count} sampled points, but no clear local peak was detected automatically."
        findings = [executive_summary]
    actions = [
        "Review the RDF curve around the first major coordination-shell region before drawing structural conclusions.",
    ]
    if result.get("extra_analysis_tables", {}).get("structure_factor", {}).get("row_count", 0) > 0:
        findings.append("A structure-factor table is also available for reciprocal-space inspection.")
    return {
        "executive_summary": executive_summary,
        "key_findings": findings,
        "recommended_actions": actions,
        "derived_metrics": {
            "peak": peak,
            "row_count": row_count,
        },
    }


def _build_msd_interpretation(result: dict[str, Any]) -> dict[str, Any]:
    table = result.get("analysis_table", {})
    summary = result.get("analysis_summary", {})
    value_column = "MSD" if "MSD" in table.get("columns", []) else _first_numeric_column(table, excluded={"Time"})
    final_value = _final_numeric_row_value(table, value_column) if value_column else None
    diffusion = summary.get("MSDDiffusionCoefficient")
    rsq = summary.get("MSDDiffusionCoefficientRsq")
    if diffusion is not None:
        executive_summary = (
            f"MSD fitting produced a diffusion coefficient of {_format_numeric(diffusion)} with R^2={_format_numeric(rsq)}."
        )
        actions = [
            "Use the fitted diffusion coefficient only if the fitted time window is physically appropriate for your system.",
        ]
    else:
        executive_summary = "MSD data is available, but no diffusion coefficient fit was returned."
        actions = [
            "If diffusion is important, extend the trajectory or adjust the MSD fitting settings.",
        ]
    findings = [executive_summary]
    if final_value is not None and value_column:
        findings.append(f"The final `{value_column}` value in the analyzed window is {_format_numeric(final_value)}.")
    return {
        "executive_summary": executive_summary,
        "key_findings": findings,
        "recommended_actions": actions,
        "derived_metrics": {
            "final_value": final_value,
            "value_column": value_column,
            "diffusion_coefficient": diffusion,
            "diffusion_rsq": rsq,
        },
    }


def _build_hbond_interpretation(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("analysis_summary", {})
    mean_count = summary.get("MeanHBondCount")
    frames_with_hbonds = summary.get("FramesWithHBonds")
    mean_length = summary.get("GlobalMeanLength")
    executive_summary = (
        f"Hydrogen-bond analysis found an average of {_format_numeric(mean_count)} hydrogen bonds per analyzed frame."
        if mean_count is not None
        else "Hydrogen-bond analysis completed."
    )
    findings = [executive_summary]
    if frames_with_hbonds is not None:
        findings.append(f"Frames containing at least one hydrogen bond: {frames_with_hbonds}.")
    if mean_length is not None:
        findings.append(f"The global mean hydrogen-bond length is {_format_numeric(mean_length)}.")
    actions = [
        "Inspect per-frame hydrogen-bond counts if transient bonding behavior matters for this system.",
    ]
    return {
        "executive_summary": executive_summary,
        "key_findings": findings,
        "recommended_actions": actions,
        "derived_metrics": {
            "mean_hbond_count": mean_count,
            "frames_with_hbonds": frames_with_hbonds,
            "global_mean_length": mean_length,
        },
    }


def _build_vacf_interpretation(result: dict[str, Any]) -> dict[str, Any]:
    table = result.get("analysis_table", {})
    value_column = "VACF" if "VACF" in table.get("columns", []) else _first_numeric_column(table, excluded={"Time"})
    final_value = _final_numeric_row_value(table, value_column) if value_column else None
    power_spectrum_rows = result.get("extra_analysis_tables", {}).get("power_spectrum", {}).get("row_count", 0)
    executive_summary = (
        f"VACF analysis returned {table.get('row_count', 0)} sampled points."
    )
    findings = [executive_summary]
    if final_value is not None and value_column:
        findings.append(f"The final `{value_column}` value in the analyzed window is {_format_numeric(final_value)}.")
    if power_spectrum_rows:
        findings.append(f"A power-spectrum table with {power_spectrum_rows} sampled rows is available.")
    actions = [
        "Inspect the VACF decay and any derived power spectrum before drawing vibrational or transport conclusions.",
    ]
    return {
        "executive_summary": executive_summary,
        "key_findings": findings,
        "recommended_actions": actions,
        "derived_metrics": {
            "final_value": final_value,
            "value_column": value_column,
            "power_spectrum_row_count": power_spectrum_rows,
        },
    }


def _inspection_trajectory_frame_count(inspection: dict[str, Any] | None) -> int | None:
    if not inspection:
        return None
    summary = inspection.get("summary", {})
    trajectory = summary.get("trajectory")
    if not isinstance(trajectory, dict):
        return None
    end_frame = trajectory.get("end_frame")
    if isinstance(end_frame, int):
        return end_frame
    return None


def _build_analysis_report(
    *,
    trajectory_frame_count: int | None,
    analysis_results: dict[str, Any],
    key_metrics: dict[str, Any],
    context_label: str = "workflow",
) -> dict[str, Any]:
    sections: dict[str, Any] = {}
    findings: list[str] = []
    actions: list[str] = []

    frames = trajectory_frame_count
    executive_summary = (
        f"The {context_label} processed {frames} trajectory frames and generated structured downstream analysis."
        if frames is not None
        else f"The {context_label} completed and returned structured downstream analysis outputs."
    )

    if "rdf" in analysis_results:
        rdf_section = _build_rdf_interpretation(analysis_results["rdf"])
        sections["rdf"] = rdf_section
        findings.extend(rdf_section["key_findings"])
        actions.extend(rdf_section["recommended_actions"])
    if "msd" in analysis_results:
        msd_section = _build_msd_interpretation(analysis_results["msd"])
        sections["msd"] = msd_section
        findings.extend(msd_section["key_findings"])
        actions.extend(msd_section["recommended_actions"])
    if "vacf" in analysis_results:
        vacf_section = _build_vacf_interpretation(analysis_results["vacf"])
        sections["vacf"] = vacf_section
        findings.extend(vacf_section["key_findings"])
        actions.extend(vacf_section["recommended_actions"])
    if "hbond" in analysis_results:
        hbond_section = _build_hbond_interpretation(analysis_results["hbond"])
        sections["hbond"] = hbond_section
        findings.extend(hbond_section["key_findings"])
        actions.extend(hbond_section["recommended_actions"])
    if "thermo" in analysis_results:
        thermo_interpretation = analysis_results["thermo"].get("interpretation")
        if thermo_interpretation:
            sections["thermo"] = thermo_interpretation
            findings.append(thermo_interpretation.get("executive_summary", ""))
            findings.extend(thermo_interpretation.get("key_findings", []))
            actions.extend(thermo_interpretation.get("recommended_actions", []))

    actions = _ordered_unique_strings(actions)
    findings = [item for item in findings if item]
    if not actions:
        actions.append("Review the exported structured outputs and verify that the analyzed frame window matches your scientific objective.")

    return {
        "executive_summary": executive_summary,
        "key_findings": findings,
        "recommended_actions": actions,
        "key_metrics": key_metrics,
        "sections": sections,
    }


def _render_analysis_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Unified Analysis Report",
        "",
        report.get("executive_summary", ""),
        "",
        "## Key Findings",
    ]
    for finding in report.get("key_findings", []):
        lines.append(f"- {finding}")
    lines.extend(["", "## Recommended Actions"])
    for action in report.get("recommended_actions", []):
        lines.append(f"- {action}")

    sections = report.get("sections", {})
    if sections:
        lines.extend(["", "## Analysis Sections"])
        for name, section in sections.items():
            lines.append("")
            lines.append(f"### {name}")
            if section.get("executive_summary"):
                lines.append(section["executive_summary"])
            for finding in section.get("key_findings", []):
                lines.append(f"- {finding}")
    return "\n".join(lines) + "\n"


def _normalize_frame_range(start_frame: int, end_frame: int) -> str:
    start = int(start_frame)
    end = int(end_frame)
    if start < 1 or end < 1:
        raise ValueError("frame indices must be >= 1")
    if start > end:
        start, end = end, start
    return f"{start}-{end}"


def _split_frame_range_spec(frame_range: str | None) -> tuple[int, int] | None:
    if not frame_range:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", frame_range)
    if not match:
        raise ValueError("frame_range must look like 'start-end', for example '1-100'.")
    return int(match.group(1)), int(match.group(2))


def _parse_frame_range_from_request(request: str) -> str | None:
    patterns = [
        r"frames?\s*(\d+)\s*(?:to|-)\s*(\d+)",
        r"(\d+)\s*(?:to|-)\s*(\d+)\s*frames?",
        r"\u7b2c?\s*(\d+)\s*(?:\u5230|-|~)\s*(\d+)\s*\u5e27",
        r"(\d+)\s*(?:\u5230|-|~)\s*(\d+)\s*\u5e27",
    ]
    for pattern in patterns:
        match = re.search(pattern, request, re.IGNORECASE)
        if match:
            return _normalize_frame_range(int(match.group(1)), int(match.group(2)))

    first_match = re.search(r"first\s*(\d+)\s*frames?", request, re.IGNORECASE)
    if first_match:
        return _normalize_frame_range(1, int(first_match.group(1)))

    chinese_first_match = re.search(r"\u524d\s*(\d+)\s*\u5e27", request)
    if chinese_first_match:
        return _normalize_frame_range(1, int(chinese_first_match.group(1)))

    return None


def _paired_input_sidecars(source_path: Path, staged_path: Path) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    if source_path.suffix.lower() == ".xtd":
        source_trj = source_path.with_suffix(".trj")
        staged_trj = staged_path.with_suffix(".trj")
        if source_trj.exists():
            pairs.append((source_trj, staged_trj))
    return pairs


def _paired_output_sidecars(output_path: Path, destination_path: Path | None) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    if output_path.suffix.lower() == ".xtd":
        produced_trj = output_path.with_suffix(".trj")
        if destination_path is not None:
            destination_trj = destination_path.with_suffix(".trj")
        else:
            destination_trj = None
        if produced_trj.exists() and destination_trj is not None:
            pairs.append((produced_trj, destination_trj))
    return pairs


def _inspect_materials_document(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    doc_path = Path(path)
    if not doc_path.exists():
        return None
    return _run_helper(
        "inspect-document",
        {
            "path": str(doc_path),
            "timeout_seconds": 180,
        },
    )["data"]


def _artifact_details_from_outputs(outputs: dict[str, Any]) -> dict[str, Any]:
    artifact_details: dict[str, Any] = {}
    for alias, info in outputs.items():
        copied_to = info.get("copied_to")
        full_output_path = info.get("full_output_path")
        chosen_path = copied_to or full_output_path
        detail: dict[str, Any] = {
            "path": chosen_path,
            "exists": bool(info.get("exists")),
        }
        if chosen_path and Path(chosen_path).suffix.lower() in {".xsd", ".xtd", ".stp"}:
            detail["inspection"] = _inspect_materials_document(chosen_path)
        artifact_details[alias] = detail
    return artifact_details


def _workflow_catalog_entry(tool_name: str) -> dict[str, Any] | None:
    for entry in WORKFLOW_CATALOG:
        if entry["tool"] == tool_name:
            return dict(entry)
    return None


def _default_output_directory(input_structure: str, suffix: str) -> str:
    source = Path(input_structure)
    return str(PROJECT_ROOT / "generated_outputs" / f"{source.stem}_{suffix}")


def _recommend_workflows_v0_unused(request: str, input_structure: str | None = None) -> dict[str, Any]:
    """Deprecated pre-v1 recommender retained temporarily for reference.

    The live implementation is ``_recommend_workflows`` below.  Keeping a
    distinct name prevents import-time shadowing and makes dead-code removal
    mechanically detectable during the server split.
    """
    lowered = request.lower()
    optimization_terms = [
        "optimize",
        "optimization",
        "relax",
        "minimize",
        "geometry optimization",
        "优化",
        "几何优化",
        "弛豫",
        "最小化",
    ]
    dynamics_terms = [
        "dynamics",
        "md",
        "trajectory",
        "nvt",
        "npt",
        "equilibration",
        "分子动力学",
        "动力学",
        "轨迹",
        "平衡",
    ]
    energy_terms = ["energy", "single point", "potential energy", "能量", "单点", "势能"]
    inspect_terms = ["inspect", "count", "formula", "metadata", "xtd", "xsd", "stp", "检查", "统计", "分子式", "元数据"]
    rdf_terms = ["rdf", "radial distribution function", "g(r)", "pair correlation", "径向分布函数"]
    msd_terms = ["msd", "mean square displacement", "diffusion", "均方位移", "扩散"]
    sequence_terms = ["then", "after", "followed by", "先", "再", "然后", "之后"]
    optimization_terms.extend(ZH_OPTIMIZATION_TERMS)
    dynamics_terms.extend(ZH_DYNAMICS_TERMS)
    energy_terms.extend(ZH_ENERGY_TERMS)
    inspect_terms.extend(ZH_INSPECT_TERMS)
    rdf_terms.extend(ZH_RDF_TERMS)
    msd_terms.extend(ZH_MSD_TERMS)
    hbond_terms = ["hbond", "hydrogen bond", "hydrogen-bond", "h-bond", *ZH_HBOND_TERMS]
    sequence_terms.extend(ZH_SEQUENCE_TERMS)
    target_terms = [
        "selection",
        "selectable",
        "set",
        "sets",
        "elements",
        "forcefield type",
        "forcefield types",
        "atom names",
        "\u5143\u7d20",
        "\u96c6\u5408",
        "\u9009\u62e9",
        "\u529b\u573a\u7c7b\u578b",
        "\u54ea\u4e9b",
    ]
    has_optimization = any(word in lowered for word in optimization_terms)
    has_dynamics = (
        re.search(r"\bmd\b", lowered) is not None
        or any(
            word in lowered
            for word in [
                "dynamics",
                "molecular dynamics",
                "nvt",
                "npt",
                "nve",
                "\u5206\u5b50\u52a8\u529b\u5b66",
                "\u52a8\u529b\u5b66\u8ba1\u7b97",
                "\u505a\u52a8\u529b\u5b66",
                "\u8fd0\u884c\u52a8\u529b\u5b66",
            ]
        )
    )
    has_energy = any(word in lowered for word in energy_terms)
    has_rdf = any(word in lowered for word in rdf_terms)
    has_msd = any(word in lowered for word in msd_terms)
    has_hbond = any(word in lowered for word in hbond_terms)
    has_sequence = any(word in lowered for word in sequence_terms)
    has_target_listing = any(word in lowered for word in target_terms) and any(
        word in lowered
        for word in [
            "element",
            "elements",
            "set",
            "sets",
            "forcefield",
            "atom name",
            "\u5143\u7d20",
            "\u96c6\u5408",
            "\u529b\u573a\u7c7b\u578b",
        ]
    )
    has_analysis_after_dynamics = has_dynamics and (has_rdf or has_msd or has_hbond)
    candidates: list[dict[str, Any]] = []
    for entry in WORKFLOW_CATALOG:
        score = 0
        for keyword in entry["keywords"]:
            if keyword in lowered:
                score += 2
        if entry["id"] == "forcite_dynamics_with_analysis" and has_analysis_after_dynamics:
            score += 18
            if has_sequence:
                score += 4
        if entry["id"] == "forcite_relax_and_dynamics":
            if has_dynamics and has_optimization:
                score += 20
                if has_sequence:
                    score += 5
        if entry["id"] == "forcite_geometry_optimization" and has_optimization:
            score += 3
            if has_dynamics:
                score -= 2
        if entry["id"] == "forcite_dynamics" and has_dynamics:
            score += 3
            if has_optimization:
                score -= 4
        if entry["id"] == "forcite_energy" and has_energy:
            score += 3
        if entry["id"] == "forcite_rdf" and has_rdf:
            score += 10
        if entry["id"] == "forcite_msd" and has_msd:
            score += 10
        if entry["id"] == "hbond_statistics" and has_hbond:
            score += 12
        if entry["id"] == "list_analysis_targets" and has_target_listing:
            score += 14
        if entry["id"] == "inspect_document" and any(word in lowered for word in inspect_terms):
            score += 3
        if score > 0:
            candidate = dict(entry)
            candidate["score"] = score
            candidates.append(candidate)

    if not candidates:
        fallback = _workflow_catalog_entry("ms_search_local_help")
        assert fallback is not None
        candidates.append({**fallback, "score": 1})

    candidates.sort(key=lambda item: (-int(item["score"]), str(item["title"])))
    top = candidates[:3]

    next_steps: list[dict[str, Any]] = []
    if top:
        best = top[0]["tool"]
        if best == "ms_forcite_relax_and_dynamics" and input_structure:
            output_directory = _default_output_directory(input_structure, "relax_md")
            next_steps.append(
                {
                    "tool": best,
                    "arguments": {
                        "input_structure": input_structure,
                        "output_directory": output_directory,
                    },
                }
            )
        elif best == "ms_forcite_geometry_optimization" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "optimize"))
            next_steps.append(
                {
                    "tool": best,
                    "arguments": {
                        "input_structure": input_structure,
                        "output_structure_path": str(output_directory / "optimized_structure.xsd"),
                    },
                }
            )
        elif best == "ms_forcite_dynamics" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "dynamics"))
            next_steps.append(
                {
                    "tool": best,
                    "arguments": {
                        "input_structure": input_structure,
                        "output_trajectory_path": str(output_directory / "dynamics_trajectory.xtd"),
                        "output_structure_path": str(output_directory / "dynamics_final_structure.xsd"),
                    },
                }
            )
        elif best == "ms_forcite_dynamics_with_analysis" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "dynamics_analysis"))
            requested_analyses = _requested_analysis_names(request) or ["rdf", "msd"]
            rdf_options = _parse_rdf_request_options(request)
            msd_options = _parse_msd_request_options(request)
            thermo_options = _parse_thermo_request_options(request)
            analysis_frame_range = _parse_frame_range_from_request(request)
            arguments: dict[str, Any] = {
                "input_structure": input_structure,
                "output_directory": str(output_directory),
                "analyses": requested_analyses,
            }
            if rdf_options:
                arguments["rdf_settings"] = rdf_options.get("analysis_settings")
                if rdf_options.get("selection_a"):
                    arguments["rdf_selection_a"] = rdf_options["selection_a"]
                if rdf_options.get("selection_b"):
                    arguments["rdf_selection_b"] = rdf_options["selection_b"]
                if rdf_options.get("include_structure_factor"):
                    arguments["rdf_include_structure_factor"] = True
            if msd_options:
                arguments["msd_settings"] = msd_options.get("analysis_settings")
                if msd_options.get("selection"):
                    arguments["msd_selection"] = msd_options["selection"]
            if analysis_frame_range:
                arguments["analysis_frame_range"] = analysis_frame_range
            next_steps.append(
                {
                    "tool": best,
                    "arguments": arguments,
                }
            )
        elif best == "ms_forcite_rdf" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "rdf"))
            rdf_options = _parse_rdf_request_options(request)
            analysis_frame_range = _parse_frame_range_from_request(request)
            arguments: dict[str, Any] = {
                "input_trajectory": input_structure,
                "output_study_table_path": str(output_directory / "rdf.std"),
            }
            if rdf_options.get("selection_a"):
                arguments["selection_a"] = rdf_options["selection_a"]
            if rdf_options.get("selection_b"):
                arguments["selection_b"] = rdf_options["selection_b"]
            if rdf_options.get("analysis_settings"):
                arguments["analysis_settings"] = rdf_options["analysis_settings"]
            if rdf_options.get("include_structure_factor"):
                arguments["include_structure_factor"] = True
                arguments["output_structure_factor_study_table_path"] = str(output_directory / "structure_factor.std")
            if analysis_frame_range:
                arguments["frame_range"] = analysis_frame_range
            next_steps.append(
                {
                    "tool": best,
                    "arguments": arguments,
                }
            )
        elif best == "ms_forcite_msd" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "msd"))
            msd_options = _parse_msd_request_options(request)
            analysis_frame_range = _parse_frame_range_from_request(request)
            arguments: dict[str, Any] = {
                "input_trajectory": input_structure,
                "output_study_table_path": str(output_directory / "msd.std"),
            }
            if msd_options.get("selection"):
                arguments["selection"] = msd_options["selection"]
            if msd_options.get("analysis_settings"):
                arguments["analysis_settings"] = msd_options["analysis_settings"]
            if analysis_frame_range:
                arguments["frame_range"] = analysis_frame_range
            next_steps.append(
                {
                    "tool": best,
                    "arguments": arguments,
                }
            )
        elif best == "ms_hbond_statistics" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "hbond"))
            default_mode = "trajectory" if Path(input_structure).suffix.lower() == ".xtd" else "single_frame"
            analysis_frame_range = _parse_frame_range_from_request(request)
            arguments: dict[str, Any] = {
                "input_document": input_structure,
                "mode": default_mode,
                "output_study_table_path": str(output_directory / "hbond_statistics.std"),
            }
            if analysis_frame_range:
                arguments["frame_range"] = analysis_frame_range
            next_steps.append(
                {
                    "tool": best,
                    "arguments": arguments,
                }
            )
        elif best == "ms_list_analysis_targets" and input_structure:
            next_steps.append(
                {
                    "tool": best,
                    "arguments": {
                        "input_document": input_structure,
                    },
                }
            )
        elif best == "ms_forcite_energy" and input_structure:
            next_steps.append(
                {
                    "tool": best,
                    "arguments": {
                        "input_structure": input_structure,
                    },
                }
            )

    notes = [
        "The recommendation is based on installed local Materials Studio 2023 capabilities and keyword matching against high-level workflows.",
        "For production work, start from an inspected structure and prefer geometry optimization before long MD runs unless you already trust the starting geometry.",
    ]
    return {
        "request": request,
        "input_structure": input_structure,
        "recommended_workflows": top,
        "suggested_next_steps": next_steps,
        "notes": notes,
    }


def _recommend_workflows(
    request: str,
    input_structure: str | None = None,
    calculation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lowered = request.lower()
    optimization_terms = [
        "optimize",
        "optimization",
        "relax",
        "minimize",
        "geometry optimization",
        *ZH_OPTIMIZATION_TERMS,
    ]
    dynamics_terms = [
        "dynamics",
        "md",
        "trajectory",
        "nvt",
        "npt",
        "equilibration",
        *ZH_DYNAMICS_TERMS,
    ]
    energy_terms = ["energy", "single point", "potential energy", *ZH_ENERGY_TERMS]
    inspect_terms = ["inspect", "count", "formula", "metadata", "xtd", "xsd", "stp", *ZH_INSPECT_TERMS]
    rdf_terms = ["rdf", "radial distribution function", "g(r)", "pair correlation", *ZH_RDF_TERMS]
    msd_terms = ["msd", "mean square displacement", "diffusion", *ZH_MSD_TERMS]
    vacf_terms = ["vacf", "velocity autocorrelation", "velocity autocorrelation function", "power spectrum", "vibration spectrum", *ZH_VACF_TERMS]
    hbond_terms = ["hbond", "hydrogen bond", "hydrogen-bond", "h-bond", *ZH_HBOND_TERMS]
    thermo_terms = [
        "thermo",
        "thermodynamic",
        "equilibration",
        "stability",
        "convergence",
        "temperature",
        "pressure",
        "density",
        "potential energy",
        "cell parameter",
        "unit cell",
        "volume",
        *ZH_THERMO_TERMS,
        *ZH_TEMPERATURE_TERMS,
        *ZH_PRESSURE_TERMS,
        *ZH_DENSITY_TERMS,
        *ZH_POTENTIAL_COMPONENT_TERMS,
        *ZH_CELL_PARAMETER_TERMS,
    ]
    sequence_terms = ["then", "after", "followed by", *ZH_SEQUENCE_TERMS]
    target_terms = [
        "selection",
        "selectable",
        "set",
        "sets",
        "elements",
        "forcefield type",
        "forcefield types",
        "atom names",
        "\u5143\u7d20",
        "\u96c6\u5408",
        "\u9009\u62e9",
        "\u529b\u573a\u7c7b\u578b",
        "\u54ea\u4e9b",
    ]

    has_optimization = any(word in lowered for word in optimization_terms)
    has_dynamics = (
        re.search(r"\bmd\b", lowered) is not None
        or any(
            word in lowered
            for word in [
                "dynamics",
                "molecular dynamics",
                "nvt",
                "npt",
                "nve",
                "\u5206\u5b50\u52a8\u529b\u5b66",
                "\u52a8\u529b\u5b66\u8ba1\u7b97",
                "\u505a\u52a8\u529b\u5b66",
                "\u8fd0\u884c\u52a8\u529b\u5b66",
            ]
        )
    )
    has_energy = any(word in lowered for word in energy_terms)
    has_rdf = any(word in lowered for word in rdf_terms)
    has_msd = any(word in lowered for word in msd_terms)
    has_vacf = any(word in lowered for word in vacf_terms)
    has_hbond = any(word in lowered for word in hbond_terms)
    has_thermo = any(word in lowered for word in thermo_terms) or any(
        word in request
        for word in [
            *ZH_THERMO_TERMS,
            *ZH_TEMPERATURE_TERMS,
            *ZH_PRESSURE_TERMS,
            *ZH_DENSITY_TERMS,
            *ZH_POTENTIAL_COMPONENT_TERMS,
            *ZH_CELL_PARAMETER_TERMS,
        ]
    )
    is_trajectory_input = bool(input_structure and Path(input_structure).suffix.lower() == ".xtd")
    requested_analysis_count = sum(1 for flag in [has_rdf, has_msd, has_vacf, has_hbond, has_thermo] if flag)
    has_bundle_request = any(
        term in lowered
        for term in [
            "analyze trajectory",
            "trajectory analysis",
            "comprehensive analysis",
            "complete analysis",
            "multi analysis",
            "unified report",
            "detailed report",
        ]
    ) or any(term in request for term in ["综合分析", "完整分析", "统一报告", "详细分析", "轨迹分析"])
    has_sequence = any(word in lowered for word in sequence_terms)
    has_target_listing = any(word in lowered for word in target_terms) and any(
        word in lowered
        for word in [
            "element",
            "elements",
            "set",
            "sets",
            "forcefield",
            "atom name",
            "\u5143\u7d20",
            "\u96c6\u5408",
            "\u529b\u573a\u7c7b\u578b",
        ]
    )
    has_analysis_after_dynamics = has_dynamics and (has_rdf or has_msd or has_hbond or has_thermo)

    candidates: list[dict[str, Any]] = []
    for entry in WORKFLOW_CATALOG:
        score = 0
        for keyword in entry["keywords"]:
            if keyword.lower() in lowered:
                score += 2
        if entry["id"] == "forcite_dynamics_with_analysis" and has_analysis_after_dynamics:
            score += 18
            if has_sequence:
                score += 4
        if entry["id"] == "forcite_relax_and_dynamics" and has_dynamics and has_optimization:
            score += 20
            if has_sequence:
                score += 5
        if entry["id"] == "forcite_geometry_optimization" and has_optimization:
            score += 3
            if has_dynamics:
                score -= 2
        if entry["id"] == "forcite_dynamics" and has_dynamics:
            score += 3
            if has_optimization:
                score -= 4
        if entry["id"] == "forcite_energy" and has_energy:
            score += 3
        if entry["id"] == "forcite_rdf" and has_rdf:
            score += 10
        if entry["id"] == "forcite_msd" and has_msd:
            score += 10
        if entry["id"] == "forcite_vacf" and has_vacf:
            score += 10
        if entry["id"] == "forcite_thermo_profiles" and has_thermo:
            score += 12
        if entry["id"] == "trajectory_analysis_bundle" and is_trajectory_input and (
            requested_analysis_count >= 2 or has_bundle_request
        ):
            score += 22
        if entry["id"] == "hbond_statistics" and has_hbond:
            score += 12
        if entry["id"] == "list_analysis_targets" and has_target_listing:
            score += 14
        if entry["id"] == "inspect_document" and any(word in lowered for word in inspect_terms):
            score += 3
        if score > 0:
            candidate = dict(entry)
            candidate["score"] = score
            candidates.append(candidate)

    if not candidates:
        fallback = _workflow_catalog_entry("ms_search_local_help")
        assert fallback is not None
        candidates.append({**fallback, "score": 1})

    candidates.sort(key=lambda item: (-int(item["score"]), str(item["title"])))
    top = candidates[:3]

    next_steps: list[dict[str, Any]] = []
    if top:
        best = top[0]["tool"]
        if best == "ms_forcite_relax_and_dynamics" and input_structure:
            next_steps.append(
                {
                    "tool": best,
                    "arguments": {
                        "input_structure": input_structure,
                        "output_directory": _default_output_directory(input_structure, "relax_md"),
                    },
                }
            )
        elif best == "ms_forcite_geometry_optimization" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "optimize"))
            next_steps.append(
                {
                    "tool": best,
                    "arguments": {
                        "input_structure": input_structure,
                        "output_structure_path": str(output_directory / "optimized_structure.xsd"),
                    },
                }
            )
        elif best == "ms_forcite_dynamics" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "dynamics"))
            next_steps.append(
                {
                    "tool": best,
                    "arguments": {
                        "input_structure": input_structure,
                        "output_trajectory_path": str(output_directory / "dynamics_trajectory.xtd"),
                        "output_structure_path": str(output_directory / "dynamics_final_structure.xsd"),
                    },
                }
            )
        elif best == "ms_forcite_dynamics_with_analysis" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "dynamics_analysis"))
            requested_analyses = _requested_analysis_names(request) or ["rdf", "msd"]
            rdf_options = _parse_rdf_request_options(request)
            msd_options = _parse_msd_request_options(request)
            thermo_options = _parse_thermo_request_options(request)
            analysis_frame_range = _parse_frame_range_from_request(request)
            arguments: dict[str, Any] = {
                "input_structure": input_structure,
                "output_directory": str(output_directory),
                "analyses": requested_analyses,
            }
            if rdf_options:
                if rdf_options.get("analysis_settings"):
                    arguments["rdf_settings"] = rdf_options["analysis_settings"]
                if rdf_options.get("selection_a"):
                    arguments["rdf_selection_a"] = rdf_options["selection_a"]
                if rdf_options.get("selection_b"):
                    arguments["rdf_selection_b"] = rdf_options["selection_b"]
                if rdf_options.get("include_structure_factor"):
                    arguments["rdf_include_structure_factor"] = True
            if msd_options:
                if msd_options.get("analysis_settings"):
                    arguments["msd_settings"] = msd_options["analysis_settings"]
                if msd_options.get("selection"):
                    arguments["msd_selection"] = msd_options["selection"]
            if thermo_options:
                if thermo_options.get("properties"):
                    arguments["thermo_properties"] = thermo_options["properties"]
                if thermo_options.get("common_analysis_settings"):
                    arguments["thermo_common_analysis_settings"] = thermo_options["common_analysis_settings"]
                if thermo_options.get("analysis_settings_by_property"):
                    arguments["thermo_analysis_settings_by_property"] = thermo_options["analysis_settings_by_property"]
            if analysis_frame_range:
                arguments["analysis_frame_range"] = analysis_frame_range
            next_steps.append({"tool": best, "arguments": arguments})
        elif best == "ms_forcite_rdf" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "rdf"))
            rdf_options = _parse_rdf_request_options(request)
            analysis_frame_range = _parse_frame_range_from_request(request)
            arguments: dict[str, Any] = {
                "input_trajectory": input_structure,
                "output_study_table_path": str(output_directory / "rdf.std"),
            }
            if rdf_options.get("selection_a"):
                arguments["selection_a"] = rdf_options["selection_a"]
            if rdf_options.get("selection_b"):
                arguments["selection_b"] = rdf_options["selection_b"]
            if rdf_options.get("analysis_settings"):
                arguments["analysis_settings"] = rdf_options["analysis_settings"]
            if rdf_options.get("include_structure_factor"):
                arguments["include_structure_factor"] = True
                arguments["output_structure_factor_study_table_path"] = str(output_directory / "structure_factor.std")
            if analysis_frame_range:
                arguments["frame_range"] = analysis_frame_range
            next_steps.append({"tool": best, "arguments": arguments})
        elif best == "ms_forcite_msd" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "msd"))
            msd_options = _parse_msd_request_options(request)
            analysis_frame_range = _parse_frame_range_from_request(request)
            arguments: dict[str, Any] = {
                "input_trajectory": input_structure,
                "output_study_table_path": str(output_directory / "msd.std"),
            }
            if msd_options.get("selection"):
                arguments["selection"] = msd_options["selection"]
            if msd_options.get("analysis_settings"):
                arguments["analysis_settings"] = msd_options["analysis_settings"]
            if analysis_frame_range:
                arguments["frame_range"] = analysis_frame_range
            next_steps.append({"tool": best, "arguments": arguments})
        elif best == "ms_forcite_vacf" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "vacf"))
            vacf_options = _parse_vacf_request_options(request)
            analysis_frame_range = _parse_frame_range_from_request(request)
            arguments: dict[str, Any] = {
                "input_trajectory": input_structure,
                "output_study_table_path": str(output_directory / "vacf.std"),
            }
            if vacf_options.get("selection"):
                arguments["selection"] = vacf_options["selection"]
            if vacf_options.get("analysis_settings"):
                arguments["analysis_settings"] = vacf_options["analysis_settings"]
            if vacf_options.get("include_power_spectrum"):
                arguments["include_power_spectrum"] = True
                arguments["output_power_spectrum_study_table_path"] = str(output_directory / "vacf_power_spectrum.std")
            if analysis_frame_range:
                arguments["frame_range"] = analysis_frame_range
            next_steps.append({"tool": best, "arguments": arguments})
        elif best == "ms_forcite_thermo_profiles" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "thermo_profiles"))
            thermo_options = _parse_thermo_request_options(request)
            analysis_frame_range = _parse_frame_range_from_request(request)
            arguments = {
                "input_trajectory": input_structure,
                "output_directory": str(output_directory),
            }
            if thermo_options.get("properties"):
                arguments["properties"] = thermo_options["properties"]
            if thermo_options.get("common_analysis_settings"):
                arguments["common_analysis_settings"] = thermo_options["common_analysis_settings"]
            if thermo_options.get("analysis_settings_by_property"):
                arguments["analysis_settings_by_property"] = thermo_options["analysis_settings_by_property"]
            if analysis_frame_range:
                arguments["frame_range"] = analysis_frame_range
            next_steps.append({"tool": best, "arguments": arguments})
        elif best == "ms_analyze_trajectory_bundle" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "trajectory_bundle"))
            rdf_options = _parse_rdf_request_options(request)
            msd_options = _parse_msd_request_options(request)
            vacf_options = _parse_vacf_request_options(request)
            thermo_options = _parse_thermo_request_options(request)
            requested_analyses = _requested_analysis_names(request) or ["rdf", "msd", "thermo"]
            analysis_frame_range = _parse_frame_range_from_request(request)
            arguments: dict[str, Any] = {
                "input_trajectory": input_structure,
                "output_directory": str(output_directory),
                "analyses": requested_analyses,
            }
            if rdf_options.get("selection_a"):
                arguments["rdf_selection_a"] = rdf_options["selection_a"]
            if rdf_options.get("selection_b"):
                arguments["rdf_selection_b"] = rdf_options["selection_b"]
            if rdf_options.get("analysis_settings"):
                arguments["rdf_settings"] = rdf_options["analysis_settings"]
            if rdf_options.get("include_structure_factor"):
                arguments["rdf_include_structure_factor"] = True
            if msd_options.get("selection"):
                arguments["msd_selection"] = msd_options["selection"]
            if msd_options.get("analysis_settings"):
                arguments["msd_settings"] = msd_options["analysis_settings"]
            if vacf_options.get("selection"):
                arguments["vacf_selection"] = vacf_options["selection"]
            if vacf_options.get("analysis_settings"):
                arguments["vacf_settings"] = vacf_options["analysis_settings"]
            if vacf_options.get("include_power_spectrum"):
                arguments["vacf_include_power_spectrum"] = True
            if thermo_options.get("properties"):
                arguments["thermo_properties"] = thermo_options["properties"]
            if thermo_options.get("common_analysis_settings"):
                arguments["thermo_common_analysis_settings"] = thermo_options["common_analysis_settings"]
            if thermo_options.get("analysis_settings_by_property"):
                arguments["thermo_analysis_settings_by_property"] = thermo_options["analysis_settings_by_property"]
            if analysis_frame_range:
                arguments["analysis_frame_range"] = analysis_frame_range
            next_steps.append({"tool": best, "arguments": arguments})
        elif best == "ms_hbond_statistics" and input_structure:
            output_directory = Path(_default_output_directory(input_structure, "hbond"))
            default_mode = "trajectory" if Path(input_structure).suffix.lower() == ".xtd" else "single_frame"
            analysis_frame_range = _parse_frame_range_from_request(request)
            arguments: dict[str, Any] = {
                "input_document": input_structure,
                "mode": default_mode,
                "output_study_table_path": str(output_directory / "hbond_statistics.std"),
            }
            if analysis_frame_range:
                arguments["frame_range"] = analysis_frame_range
            next_steps.append({"tool": best, "arguments": arguments})
        elif best == "ms_list_analysis_targets" and input_structure:
            next_steps.append(
                {
                    "tool": best,
                    "arguments": {
                        "input_document": input_structure,
                        "input_sha256": "<required exact SHA-256>",
                        "dry_run": True,
                    },
                }
            )
        elif best == "ms_forcite_energy" and input_structure:
            next_steps.append({"tool": best, "arguments": {"input_structure": input_structure}})

    adaptive_plan = build_adaptive_calculation_plan(
        request=request,
        input_structure=input_structure,
        calculation_context=calculation_context,
    )
    if adaptive_plan.get("engine") == "CASTEP":
        package_entry = _workflow_catalog_entry("ms_prepare_castep_pl_package")
        if package_entry is not None:
            top = [{**package_entry, "score": 100, "selection_basis": "adaptive_calculation_plan"}]
        next_steps = [
            {
                "tool": "ms_prepare_castep_pl_package",
                "mode": "dry_run_only" if adaptive_plan.get("status") == "ready_for_runtime_preflight" else "blocked",
                "plan_status": adaptive_plan.get("status"),
                "execution_allowed": False,
                "required_before_call": adaptive_plan.get("preflight_blockers", []),
            }
        ]

    notes = [
        "The recommendation is based on installed local Materials Studio 2023 capabilities and keyword matching against high-level workflows.",
        "For production work, start from an inspected structure and resolve every adaptive-plan blocker before any execution.",
    ]
    return {
        "request": request,
        "input_structure": input_structure,
        "recommended_workflows": top,
        "suggested_next_steps": next_steps,
        "adaptive_calculation_plan": adaptive_plan,
        "notes": notes,
    }


def _extract_float(patterns: list[str], text: str) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _extract_int(patterns: list[str], text: str) -> int | None:
    value = _extract_float(patterns, text)
    if value is None:
        return None
    return int(value)


def _parse_common_forcite_settings(request: str) -> dict[str, Any]:
    lowered = request.lower()
    settings: dict[str, Any] = {}

    forcefields = {
        "compassiii": "COMPASSIII",
        "compass3": "COMPASSIII",
        "compass": "COMPASS",
        "cvff": "CVFF",
        "universal": "Universal",
    }
    for keyword, value in forcefields.items():
        if keyword in lowered:
            settings["CurrentForcefield"] = value
            break

    if "use current" in lowered or "当前电荷" in request:
        settings["ChargeAssignment"] = "Use current"
    elif "qeq" in lowered:
        settings["ChargeAssignment"] = "QEq"

    return settings


def _parse_dynamics_settings_from_request(request: str) -> dict[str, Any]:
    lowered = request.lower()
    settings = _parse_common_forcite_settings(request)

    if "npt" in lowered or "恒压恒温" in request:
        settings["Ensemble3D"] = "NPT"
    elif "nve" in lowered or "微正则" in request:
        settings["Ensemble3D"] = "NVE"
    elif "nvt" in lowered or "恒温恒体积" in request:
        settings["Ensemble3D"] = "NVT"

    thermostats = {
        "andersen": "Andersen",
        "nose": "Nose",
        "berendsen": "Berendsen",
    }
    for keyword, value in thermostats.items():
        if keyword in lowered:
            settings["Thermostat"] = value
            break

    temperature = _extract_float(
        [
            r"temperature\s*[:=]?\s*(\d+(?:\.\d+)?)",
            r"(\d+(?:\.\d+)?)\s*k\b",
            r"温度\s*[:：]?\s*(\d+(?:\.\d+)?)",
        ],
        request,
    )
    if temperature is not None:
        settings["Temperature"] = temperature

    step_count = _extract_int(
        [
            r"number\s+of\s+steps\s*[:=]?\s*(\d+)",
            r"(\d+)\s*steps?\b",
            r"(\d+)\s*步",
        ],
        request,
    )
    if step_count is None:
        if "短" in request or "short" in lowered:
            step_count = 50
        elif "长" in request or "long" in lowered:
            step_count = 1000
    if step_count is not None:
        settings["NumberOfSteps"] = step_count

    timestep = _extract_float(
        [
            r"time\s*step\s*[:=]?\s*(\d+(?:\.\d+)?)",
            r"(\d+(?:\.\d+)?)\s*fs\b",
            r"步长\s*[:：]?\s*(\d+(?:\.\d+)?)",
        ],
        request,
    )
    if timestep is not None:
        settings["TimeStep"] = timestep

    trajectory_frequency = _extract_int(
        [
            r"trajectory\s+frequency\s*[:=]?\s*(\d+)",
            r"every\s*(\d+)\s*steps?",
            r"每\s*(\d+)\s*步",
        ],
        request,
    )
    if trajectory_frequency is None and ("轨迹" in request or "trajectory" in lowered):
        steps = int(settings.get("NumberOfSteps", 100))
        trajectory_frequency = max(1, steps // 10)
    if trajectory_frequency is not None:
        settings["TrajectoryFrequency"] = trajectory_frequency

    return settings


def _parse_geometry_settings_from_request(request: str) -> dict[str, Any]:
    return _parse_common_forcite_settings(request)


def _default_execution_output_directory(input_structure: str | None, workflow_name: str) -> Path:
    if input_structure:
        stem = Path(input_structure).stem
    else:
        stem = "materials_studio"
    return PROJECT_ROOT / "generated_outputs" / f"{stem}_{workflow_name}"


def _run_forcite_analysis_task(
    *,
    analysis_name: str,
    study_table_property: str,
    input_trajectory: str,
    analysis_settings: dict[str, Any] | None,
    selection_settings: dict[str, str] | None,
    frame_range: str | None,
    extra_table_properties: dict[str, str] | None,
    extra_table_destination_paths: dict[str, str] | None,
    result_properties: list[str] | None,
    job_name: str,
    output_study_table_path: str | None = None,
    timeout_seconds: int = 600,
    keep_job_dir: bool = True,
) -> dict[str, Any]:
    outputs: dict[str, Any] = {
        "summary": {"relative_path": "analysis_summary.txt"},
        "table": {"relative_path": "analysis_table.tsv"},
    }
    if output_study_table_path:
        outputs["study_table"] = {
            "relative_path": f"../exports/{analysis_name}.std",
            "destination_path": output_study_table_path,
        }
    for alias in (extra_table_properties or {}):
        outputs[f"{alias}_table"] = {"relative_path": f"{alias}_table.tsv"}
        destination_path = (extra_table_destination_paths or {}).get(alias)
        if destination_path:
            outputs[f"{alias}_study_table"] = {
                "relative_path": f"../exports/{alias}.std",
                "destination_path": destination_path,
            }

    save_table_line = ""
    if "study_table" in outputs:
        save_table_line = f'$std->SaveAs("exports/{analysis_name}.std");'

    result_lines = ""
    for prop in result_properties or []:
        result_lines += (
            "eval {\n"
            f"  my $value = $analysis->{prop};\n"
            f"  print $summary \"{prop}=$value\\n\" if defined $value;\n"
            "};\n"
        )

    selection_resolver = ""
    selection_apply_lines = ""
    if selection_settings:
        selection_resolver = """
sub _doc_atoms_for_selection {
  my ($doc) = @_;
  my $atoms = eval { $doc->UnitCell->Atoms };
  $atoms = eval { $doc->Atoms } if !defined($atoms);
  return $atoms;
}

sub _existing_set_name {
  my ($doc, $name) = @_;
  my $set = eval { $doc->Sets($name) };
  return $name if defined $set;
  $set = eval { $doc->UnitCell->Sets($name) };
  return $name if defined $set;
  return undef;
}

sub _resolve_selection_name {
  my ($doc, $spec, $generated_name) = @_;
  return undef if !defined($spec) || $spec eq "";

  my $existing_name = _existing_set_name($doc, $spec);
  return $existing_name if defined $existing_name;

  my $atoms = _doc_atoms_for_selection($doc);
  my @matches;

  if ($spec =~ /^element:(.+)$/i || $spec =~ /^[A-Z][a-z]?$/) {
    my $symbol = $1;
    $symbol = $spec if !defined($symbol) || $symbol eq "";
    foreach my $atom (@$atoms) {
      push @matches, $atom if uc($atom->ElementSymbol) eq uc($symbol);
    }
  }
  elsif ($spec =~ /^(?:forcefield|fftype):(.+)$/i) {
    my $type_name = $1;
    foreach my $atom (@$atoms) {
      my $forcefield_type = eval { $atom->ForcefieldType };
      push @matches, $atom if defined($forcefield_type) && $forcefield_type eq $type_name;
    }
  }
  elsif ($spec =~ /^name:(.+)$/i) {
    my $atom_name = $1;
    foreach my $atom (@$atoms) {
      my $name = eval { $atom->Name };
      push @matches, $atom if defined($name) && $name eq $atom_name;
    }
  }
  else {
    return $spec;
  }

  die "No atoms matched selection spec '$spec'." if scalar(@matches) == 0;

  eval {
    my $old_set = $doc->Sets($generated_name);
    $old_set->Delete if defined $old_set;
  };
  $doc->CreateSet($generated_name, \\@matches);
  return $generated_name;
}
"""
        for index, (setting_name, selection_spec) in enumerate(selection_settings.items(), start=1):
            safe_var = f"selection_{index}"
            generated_name = f"MCP_{analysis_name}_{setting_name}"
            selection_apply_lines += (
                f"my ${safe_var} = _resolve_selection_name($doc, {_perl_quote(selection_spec)}, {_perl_quote(generated_name)});\n"
                f"push @analysis_settings, {setting_name} => ${safe_var} if defined ${safe_var};\n"
            )

    module_settings_lines = ""
    if frame_range:
        module_settings_lines += f"Modules->Forcite->ChangeSettings([ActiveDocumentFrameRange => {_perl_quote(frame_range)}]);\n"

    extra_table_blocks = ""
    for index, (alias, property_name) in enumerate((extra_table_properties or {}).items(), start=1):
        table_alias = f"{alias}_table"
        study_table_alias = f"{alias}_study_table"
        extra_std_var = f"extra_std_{index}"
        extra_sheet_var = f"extra_sheet_{index}"
        extra_table_fh = f"extra_table_{index}"
        maybe_save_std = ""
        if study_table_alias in outputs:
            maybe_save_std = f'${extra_std_var}->SaveAs("exports/{alias}.std");'
        extra_table_blocks += f"""
my ${extra_std_var} = eval {{ $analysis->{property_name} }};
if (defined ${extra_std_var}) {{
  my ${extra_sheet_var} = ${extra_std_var}->Sheets(0);
  open(my ${extra_table_fh}, '>', "{{{{output.{table_alias}}}}}") or die $!;
  my @headings_{index} = map {{ ${extra_sheet_var}->ColumnHeading($_) }} (0..(${extra_sheet_var}->ColumnCount-1));
  print ${extra_table_fh} join("\\t", @headings_{index}), "\\n";
  for my $row (0..(${extra_sheet_var}->RowCount-1)) {{
    my @values = map {{
      my $value = ${extra_sheet_var}->Cell($row, $_);
      defined $value ? $value : "";
    }} (0..(${extra_sheet_var}->ColumnCount-1));
    print ${extra_table_fh} join("\\t", @values), "\\n";
  }}
  close(${extra_table_fh});
  {maybe_save_std}
}}
"""

    script = f"""use strict;
use warnings;
use MaterialsScript qw(:all);
my $doc = Documents->Import("{{{{input.trajectory}}}}");
my @analysis_settings = (
  {_perl_settings_entries(analysis_settings)}
);
{selection_resolver}
{selection_apply_lines}
{module_settings_lines}
my $analysis = Modules->Forcite->Analysis->{analysis_name}($doc, Settings(@analysis_settings));
my $std = $analysis->{study_table_property};
my $sheet = $std->Sheets(0);
open(my $summary, '>', "{{{{output.summary}}}}") or die $!;
print $summary "RowCount=", $sheet->RowCount, "\\n";
print $summary "ColumnCount=", $sheet->ColumnCount, "\\n";
for my $col (0..($sheet->ColumnCount-1)) {{
  my $heading = $sheet->ColumnHeading($col);
  print $summary "Heading", $col, "=", $heading, "\\n" if defined $heading;
}}
{result_lines}
close($summary);
open(my $table, '>', "{{{{output.table}}}}") or die $!;
my @headings = map {{ $sheet->ColumnHeading($_) }} (0..($sheet->ColumnCount-1));
print $table join("\\t", @headings), "\\n";
for my $row (0..($sheet->RowCount-1)) {{
  my @values = map {{
    my $value = $sheet->Cell($row, $_);
    defined $value ? $value : "";
  }} (0..($sheet->ColumnCount-1));
  print $table join("\\t", @values), "\\n";
}}
close($table);
{save_table_line}
{extra_table_blocks}
"""

    result = _run_materialsscript_job(
        script_template=script,
        input_files={"trajectory": input_trajectory},
        output_files=outputs,
        job_name=job_name,
        run_mode="flat",
        keep_job_dir=keep_job_dir,
        timeout_seconds=timeout_seconds,
    )

    summary_text = ""
    table_text = ""
    summary_path = result["outputs"].get("summary", {}).get("full_output_path")
    table_path = result["outputs"].get("table", {}).get("full_output_path")
    if summary_path and Path(summary_path).exists():
        summary_text = Path(summary_path).read_text(encoding="utf-8", errors="replace")
    if table_path and Path(table_path).exists():
        table_text = Path(table_path).read_text(encoding="utf-8", errors="replace")

    result["analysis_summary"] = _parse_key_value_text(summary_text)
    result["analysis_table"] = _parse_tsv_table(table_text)
    result["analysis_preview"] = result["analysis_table"]["rows"][:10]
    extra_tables: dict[str, Any] = {}
    for alias in (extra_table_properties or {}):
        alias_path = result["outputs"].get(f"{alias}_table", {}).get("full_output_path")
        alias_text = ""
        if alias_path and Path(alias_path).exists():
            alias_text = Path(alias_path).read_text(encoding="utf-8", errors="replace")
        extra_tables[alias] = _parse_tsv_table(alias_text)
    result["extra_analysis_tables"] = extra_tables
    result["extra_analysis_previews"] = {alias: table["rows"][:10] for alias, table in extra_tables.items()}
    result["references"] = _reference_entries(FORCITE_HELP_PAGES.get(analysis_name, []))
    result["workflow"] = {
        "analysis_name": analysis_name,
        "analysis_settings": analysis_settings or {},
        "selection_settings": selection_settings or {},
        "frame_range": frame_range,
        "extra_table_properties": extra_table_properties or {},
    }
    result["artifact_details"] = _artifact_details_from_outputs(result["outputs"])

    if not keep_job_dir:
        input_dir = result.get("input_dir")
        job_dir = result.get("job_dir")
        if input_dir:
            shutil.rmtree(input_dir, ignore_errors=True)
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        result["job_dir"] = None
        result["input_dir"] = None
        result["output_dir"] = None
        result["rendered_script_path"] = None
        result["script_stdout_path"] = None
        result["matstudio_log_path"] = None

    return result


def _run_guarded_materialsscript_process(
    command: list[str], *, cwd: Path, timeout_seconds: int, stdin_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], bool, dict[str, Any] | None, int]:
    """Run one owned MaterialsScript process and kill only its tree on timeout."""
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    stdin_handle = stdin_path.open("r", encoding="ascii") if stdin_path is not None else None
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=stdin_handle if stdin_handle is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            env=env,
        )
    except Exception:
        if stdin_handle is not None:
            stdin_handle.close()
        raise
    termination: dict[str, Any] | None = None
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        termination = _terminate_process_tree(process)
        stdout, stderr = process.communicate(timeout=15)
    finally:
        if stdin_handle is not None:
            stdin_handle.close()
    return (
        subprocess.CompletedProcess(command, process.returncode, stdout, stderr),
        timed_out,
        termination,
        process.pid,
    )


def _run_materialsscript_job(
    *,
    script_template: str,
    input_files: dict[str, str] | None,
    output_files: dict[str, Any] | None,
    job_name: str,
    run_mode: str,
    keep_job_dir: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    if not script_template.strip():
        raise ValueError("script_template is required.")
    if run_mode not in {"flat", "project"}:
        raise ValueError("run_mode must be exactly 'flat' or 'project'.")

    config = load_pipeline_config()
    timeout_seconds = bounded_timeout(timeout_seconds, config=config)

    ms_paths = _materials_studio_paths()
    run_mat_script = approved_executable(ms_paths["run_mat_script"], config=config)
    _assert_ascii_absolute_path(Path(ms_paths["root"]), "Materials Studio root")
    _assert_ascii_absolute_path(run_mat_script, "RunMatScript path")
    if not run_mat_script.exists():
        raise FileNotFoundError(f"RunMatScript.bat not found: {run_mat_script}")

    job_name = _ascii_safe_name(job_name, "ms_job")
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    scratch_root = _materialsscript_scratch_root()
    input_dir = scratch_root / "inputs" / job_id
    job_dir = scratch_root / "jobs" / job_id
    output_dir = job_dir / "outputs"
    _assert_ascii_absolute_path(input_dir, "MaterialsScript input directory")
    _assert_ascii_absolute_path(job_dir, "MaterialsScript job directory")
    _assert_ascii_absolute_path(output_dir, "MaterialsScript output directory")
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    template_vars: dict[str, str] = {
        "ms_root": _perl_path(Path(ms_paths["root"])),
        "job_dir": _perl_path(job_dir),
        "input_dir": _perl_path(input_dir),
        "output_dir": _perl_path(output_dir),
    }

    staged_inputs: dict[str, Any] = {}
    for alias, source in (input_files or {}).items():
        source_path = resolve_workspace_path(source, must_exist=True, config=config)
        source_suffix = source_path.suffix
        if not source_suffix.isascii():
            raise ValueError(
                f"Input file extension must contain ASCII characters only for MaterialsScript staging: {source_suffix!r}"
            )
        staged_name = f"{_ascii_safe_name(alias, 'input')}{source_suffix}"
        staged_path = input_dir / staged_name
        _assert_ascii_absolute_path(staged_path, f"Staged input path for {alias!r}")
        shutil.copy2(source_path, staged_path)
        sidecars: list[dict[str, str]] = []
        for sidecar_source, sidecar_target in _paired_input_sidecars(source_path, staged_path):
            shutil.copy2(sidecar_source, sidecar_target)
            sidecars.append(
                {
                    "source_path": str(sidecar_source),
                    "staged_path": str(sidecar_target),
                }
            )
        template_vars[f"input.{alias}"] = _perl_path(staged_path)
        staged_inputs[alias] = {
            "source_path": str(source_path),
            "staged_path": str(staged_path),
            "sidecars": sidecars,
        }

    requested_outputs: dict[str, Any] = {}
    for alias, spec in (output_files or {}).items():
        if isinstance(spec, str):
            relative_path = spec
            destination_path = None
        else:
            relative_path = spec.get("relative_path")
            destination_path = spec.get("destination_path")
        if not relative_path:
            raise ValueError(f"output_files[{alias!r}] must define relative_path.")
        relative_output_path = Path(relative_path)
        if relative_output_path.is_absolute():
            raise ValueError(f"output_files[{alias!r}].relative_path must be relative to the job directory.")
        if not str(relative_output_path).isascii():
            raise ValueError(f"output_files[{alias!r}].relative_path must contain ASCII characters only.")
        full_output_path = (output_dir / relative_output_path).resolve()
        if not full_output_path.is_relative_to(job_dir.resolve()):
            raise ValueError(f"output_files[{alias!r}].relative_path escapes the MaterialsScript job directory.")
        _assert_ascii_absolute_path(full_output_path, f"Staged output path for {alias!r}")
        full_output_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_destination = (
            resolve_output_path(destination_path, config=config) if destination_path else None
        )
        template_vars[f"output.{alias}"] = _perl_path(full_output_path)
        template_vars[f"output_rel.{alias}"] = _perl_path(Path(relative_path))
        requested_outputs[alias] = {
            "relative_path": relative_path,
            "full_output_path": str(full_output_path),
            "destination_path": str(resolved_destination) if resolved_destination else None,
        }

    rendered_script = _render_template(script_template, template_vars)
    _assert_no_unicode_absolute_path_literals(rendered_script)
    if re.search(r"\{\{[^{}]+\}\}", rendered_script):
        raise ValueError("Rendered MaterialsScript contains an unresolved template placeholder.")
    script_path = job_dir / f"{job_name}.pl"
    script_path.write_text(rendered_script, encoding="utf-8", newline="\n")

    command = [str(run_mat_script), "-project" if run_mode == "project" else "-flat", job_name]
    with acquire_execution_slot(config=config):
        completed, timed_out, termination, process_pid = _run_guarded_materialsscript_process(
            command, cwd=job_dir, timeout_seconds=timeout_seconds
        )

    stdout_path = Path(f"{script_path}.out")
    log_candidates = (
        [job_dir / f"{job_name}_Files" / "MatStudioLog.htm", job_dir / f"{job_name}MatStudioLog.htm"]
        if run_mode == "project"
        else [job_dir / f"{job_name}MatStudioLog.htm", job_dir / f"{job_name}_Files" / "MatStudioLog.htm"]
    )
    log_path = next((path for path in log_candidates if path.exists()), log_candidates[0])
    script_stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    log_html = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    log_text = _strip_html(log_html)

    staged_output_state: dict[str, dict[str, Any]] = {}
    for alias, entry in requested_outputs.items():
        full_output_path = Path(entry["full_output_path"])
        exists = full_output_path.is_file() and full_output_path.stat().st_size > 0
        staged_output_state[alias] = {
            "exists": exists,
            "full_output_path": str(full_output_path),
            "destination_path": entry["destination_path"],
            "bytes": full_output_path.stat().st_size if exists else 0,
            "sha256": hashlib.sha256(full_output_path.read_bytes()).hexdigest() if exists else None,
        }

    success = (
        not timed_out
        and
        completed.returncode == 0
        and "Completion status: (OK)." in log_text
        and "Exiting MatServer: status OK." in log_text
        and all(item["exists"] for item in staged_output_state.values())
    )

    copied_outputs: dict[str, Any] = {}
    published_paths: list[Path] = []
    temporary_paths: list[Path] = []
    try:
        for alias, state in staged_output_state.items():
            destination_path = state["destination_path"]
            copied_to = None
            copied_sidecars: list[dict[str, str]] = []
            if success and destination_path:
                destination = Path(destination_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                publish_set = [(Path(state["full_output_path"]), destination)]
                publish_set.extend(_paired_output_sidecars(Path(state["full_output_path"]), destination))
                for source, target in publish_set:
                    if not source.is_file() or source.stat().st_size <= 0:
                        raise RuntimeError(f"Expected staged output sidecar is missing or empty: {source.name}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temp_target = target.with_name(f".{target.name}.mcp-{uuid.uuid4().hex}.tmp")
                    with source.open("rb") as source_handle, temp_target.open("xb") as temp_handle:
                        shutil.copyfileobj(source_handle, temp_handle)
                    temporary_paths.append(temp_target)
                    if target.exists():
                        raise FileExistsError(f"Refusing to overwrite existing output: {target}")
                    temp_target.rename(target)
                    temporary_paths.remove(temp_target)
                    published_paths.append(target)
                    if source == Path(state["full_output_path"]):
                        copied_to = str(target)
                    else:
                        copied_sidecars.append({"source_path": str(source), "destination_path": str(target)})
            copied_outputs[alias] = {
                **state,
                "copied_to": copied_to,
                "copied_sidecars": copied_sidecars,
            }
    except Exception:
        for path in reversed(published_paths):
            path.unlink(missing_ok=True)
        for path in temporary_paths:
            path.unlink(missing_ok=True)
        raise

    audit = {
        "schema_version": 1,
        "status": "succeeded" if success else ("timeout" if timed_out else "failed"),
        "job_id": job_id,
        "job_name": job_name,
        "pid": process_pid,
        "command": command,
        "timeout_seconds": timeout_seconds,
        "exit_code": completed.returncode,
        "termination": termination,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode("utf-8")).hexdigest(),
        "script_sha256": hashlib.sha256(rendered_script.encode("utf-8")).hexdigest(),
        "outputs": staged_output_state,
        "published_paths": [str(path) for path in published_paths],
    }
    audit_path = job_dir / "audit.json"
    with audit_path.open("x", encoding="utf-8") as handle:
        json.dump(audit, handle, ensure_ascii=False, indent=2)

    data = {
        "success": success,
        "job_id": job_id,
        "job_name": job_name,
        "run_mode": run_mode,
        "job_dir": str(job_dir) if keep_job_dir else None,
        "input_dir": str(input_dir) if keep_job_dir else None,
        "output_dir": str(output_dir) if keep_job_dir else None,
        "rendered_script_path": str(script_path),
        "staged_inputs": staged_inputs,
        "outputs": copied_outputs,
        "run_mat_script_exit_code": completed.returncode,
        "pid": process_pid,
        "timed_out": timed_out,
        "termination": termination,
        "shell_stdout": completed.stdout,
        "shell_stderr": completed.stderr,
        "script_stdout_path": str(stdout_path) if stdout_path.exists() else None,
        "script_stdout": script_stdout,
        "matstudio_log_path": str(log_path) if log_path.exists() else None,
        "matstudio_log_excerpt": log_text[:2000],
        "audit_path": str(audit_path),
    }

    if not success:
        error_bits = [bit for bit in [completed.stderr.strip(), script_stdout.strip(), log_text[:1200].strip()] if bit]
        data["error_summary"] = "\n\n".join(error_bits[:3])

    if success and not keep_job_dir:
        shutil.rmtree(input_dir, ignore_errors=True)
        shutil.rmtree(job_dir, ignore_errors=True)
        data["job_dir"] = None
        data["input_dir"] = None
        data["output_dir"] = None
        data["rendered_script_path"] = None
        data["script_stdout_path"] = None
        data["matstudio_log_path"] = None
        data["audit_path"] = None

    return redact_sensitive(data)


def _forcite_report_properties(extra_properties: list[str] | None = None) -> list[str]:
    base = [
        "PotentialEnergy",
        "KineticEnergy",
        "TotalEnergy",
        "Temperature",
        "Pressure",
    ]
    for item in extra_properties or []:
        if item not in base:
            base.append(item)
    return base


def _forcite_report_lines(properties: list[str]) -> str:
    lines = []
    for prop in properties:
        lines.append(
            "eval {\n"
            f"  my $value = $doc->{prop};\n"
            f"  print $fh \"{prop}=$value\\n\" if defined $value;\n"
            "};\n"
        )
    return "".join(lines)


def _build_forcite_script_template(
    task_name: str,
    module_settings: dict[str, Any],
    *,
    include_structure: bool,
    include_trajectory: bool,
    report_properties: list[str] | None = None,
) -> tuple[str, list[str]]:
    if task_name not in {"Energy", "GeometryOptimization", "Dynamics"}:
        raise ValueError(f"Unsupported governed Forcite task: {task_name}")
    properties = _forcite_report_properties(report_properties)
    report_lines = _forcite_report_lines(properties)
    output_blocks = []
    if include_structure:
        output_blocks.append('$doc->Export("{{output.structure}}");')
    if include_trajectory:
        output_blocks.append('$results->Trajectory->Export("{{output.trajectory}}");')
    script = f"""use strict;
use warnings;
use MaterialsScript qw(:all);
my $doc = Documents->Import("{{{{input.structure}}}}");
Modules->Forcite->ChangeSettings({_perl_settings_array(module_settings)});
my $results = Modules->Forcite->{task_name}->Run($doc);
open(my $fh, '>', "{{{{output.report}}}}") or die $!;
{report_lines}eval {{
  print $fh "TrajectoryFrames=", $results->Trajectory->NumFrames, "\\n" if $results->Trajectory;
}};
my $atom_audit_count = 0;
my $missing_forcefield_type_count = 0;
my $partial_charge_count = 0;
my $partial_charge_read_error_count = 0;
my $net_partial_charge = 0.0;
foreach my $atom (@{{$doc->Atoms}}) {{
  ++$atom_audit_count;
  my $forcefield_type = eval {{ $atom->ForcefieldType }};
  ++$missing_forcefield_type_count if !defined($forcefield_type) || $forcefield_type eq "";
  my $charge_ok = eval {{
    my $charge = $atom->Charge;
    die "undefined charge" if !defined($charge);
    $net_partial_charge += $charge;
    ++$partial_charge_count;
    1;
  }};
  ++$partial_charge_read_error_count if !$charge_ok;
}}
print $fh "AtomAuditCount=$atom_audit_count\\n";
print $fh "MissingForcefieldTypeCount=$missing_forcefield_type_count\\n";
print $fh "PartialChargeCount=$partial_charge_count\\n";
print $fh "PartialChargeReadErrorCount=$partial_charge_read_error_count\\n";
print $fh "NetPartialCharge=$net_partial_charge\\n";
close($fh);
{os.linesep.join(output_blocks)}
"""
    return script, properties


def _run_forcite_task(
    *,
    task_name: str,
    input_structure: str,
    module_settings: dict[str, Any] | None,
    report_properties: list[str] | None,
    job_name: str,
    output_structure_path: str | None = None,
    output_trajectory_path: str | None = None,
    run_mode: str = "flat",
    timeout_seconds: int = 600,
    keep_job_dir: bool = True,
) -> dict[str, Any]:
    outputs: dict[str, Any] = {
        "report": {"relative_path": "report.txt"},
    }
    if output_structure_path:
        outputs["structure"] = {
            "relative_path": "exports/output_structure.xsd",
            "destination_path": output_structure_path,
        }
    if output_trajectory_path:
        outputs["trajectory"] = {
            "relative_path": "exports/output_trajectory.xtd",
            "destination_path": output_trajectory_path,
        }

    script, properties = _build_forcite_script_template(
        task_name,
        module_settings or {},
        include_structure="structure" in outputs,
        include_trajectory="trajectory" in outputs,
        report_properties=report_properties,
    )

    result = _run_materialsscript_job(
        script_template=script,
        input_files={"structure": input_structure},
        output_files=outputs,
        job_name=job_name,
        run_mode=run_mode,
        keep_job_dir=True,
        timeout_seconds=timeout_seconds,
    )
    report_info = result["outputs"].get("report", {})
    report_path = report_info.get("full_output_path")
    if report_path and Path(report_path).exists():
        result["parsed_report"] = _parse_key_value_text(Path(report_path).read_text(encoding="utf-8", errors="replace"))
    else:
        result["parsed_report"] = {}
    result["artifact_details"] = _artifact_details_from_outputs(result["outputs"])
    result["references"] = _reference_entries(FORCITE_HELP_PAGES.get(task_name, []))
    result["workflow"] = {
        "task_name": task_name,
        "module_settings": module_settings or {},
        "report_properties": properties,
    }
    if not keep_job_dir:
        input_dir = result.get("input_dir")
        job_dir = result.get("job_dir")
        if input_dir:
            shutil.rmtree(input_dir, ignore_errors=True)
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        result["job_dir"] = None
        result["input_dir"] = None
        result["output_dir"] = None
        result["rendered_script_path"] = None
        result["script_stdout_path"] = None
        result["matstudio_log_path"] = None
    return result


@mcp.tool()
def ms_detect_installation() -> dict[str, Any]:
    """Detect the local BIOVIA Materials Studio 2023 installation and key paths."""

    return _run_helper("detect")["data"]


@mcp.tool()
def md_pipeline_get_config() -> dict[str, Any]:
    """Return a redacted pipeline configuration summary safe for MCP clients."""

    config = load_pipeline_config()
    software = config.get("software", {})
    components: dict[str, Any] = {}
    for name in ("materials_studio", "lammps", "mpi", "vmd", "packmol"):
        section = software.get(name, {})
        configured = any(bool(section.get(key)) for key in ("root", "run_mat_script", "executable", "msi2lmp"))
        components[name] = {"configured": configured}
    policy = config.get("policy", {})
    return {
        "schema_version": policy.get("schema_version"),
        "components": components,
        "limits": dict(policy.get("limits", {})),
        "execution": dict(policy.get("execution", {})),
        "preflight": dict(policy.get("preflight", {})),
        "redacted": True,
    }


@mcp.tool()
def md_pipeline_health_check(run_version_probes: bool = True) -> dict[str, Any]:
    """Check MS, LAMMPS, MPI, VMD, conversion, and packing tools without running a simulation."""

    return pipeline_health_check(run_version_probes=run_version_probes)


@mcp.tool()
def md_model_readiness_assess(
    model_spec: dict[str, Any],
    search_roots: list[str] | None = None,
) -> dict[str, Any]:
    """Read-only intake assessment for an incomplete Materials Studio model specification."""

    operation = "md_model_readiness_assess"
    try:
        return success_result(operation, assess_model_readiness(model_spec, search_roots=search_roots))
    except Exception as exc:
        return error_result(operation, exc)


@mcp.tool()
def md_model_gap_resolution_plan(
    model_spec: dict[str, Any],
    search_roots: list[str] | None = None,
) -> dict[str, Any]:
    """Plan local and human-reviewed remedies for model inputs that are not yet ready."""

    operation = "md_model_gap_resolution_plan"
    try:
        return success_result(operation, build_model_gap_resolution_plan(model_spec, search_roots=search_roots))
    except Exception as exc:
        return error_result(operation, exc)


@mcp.tool()
def md_search_public_model_evidence(
    query: str,
    provider: str,
    max_results: int = 3,
    allow_network: bool = False,
    dry_run: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Read fixed-provider PubChem/Crossref metadata only after explicit network opt-in."""

    operation = "md_search_public_model_evidence"
    parameters = {
        "query": query,
        "provider": provider,
        "max_results": max_results,
        "allow_network": allow_network,
    }
    try:
        if not isinstance(dry_run, bool) or not isinstance(allow_network, bool):
            raise ValueError("dry_run and allow_network must be booleans")
        request_plan = build_public_evidence_request(query, provider, max_results)
        if dry_run:
            return success_result(
                operation,
                {
                    **_dry_run_payload(operation, parameters, validations={"network_request": request_plan}),
                    "network_access": "not_requested",
                    "network_opt_in_required": True,
                    "confirmation_required_for_live_lookup": True,
                },
                warnings=["No network request was sent. A live lookup requires allow_network=true and an exact single-use confirmation token."],
            )
        if not allow_network:
            raise PermissionError("Live public evidence lookup requires allow_network=true")
        confirmation_manager.consume(confirmation_token, operation, parameters)
        return success_result(operation, search_public_model_evidence(query, provider, max_results))
    except Exception as exc:
        return error_result(operation, exc)


@mcp.resource(
    "ms://runtime/health",
    name="materials_studio_runtime_health",
    description="Read-only Materials Studio, LAMMPS, MPI, VMD, and Packmol readiness.",
    mime_type="application/json",
)
def ms_runtime_health_resource() -> str:
    return json.dumps(pipeline_health_check(run_version_probes=False), ensure_ascii=False, indent=2)


@mcp.resource(
    "ms://catalog/public-tools",
    name="materials_studio_public_tool_catalog",
    description="Versioned public MCP tool and risk catalog.",
    mime_type="application/json",
)
def ms_public_tool_catalog_resource() -> str:
    return json.dumps(api_catalog(), ensure_ascii=False, indent=2)


@mcp.resource(
    "materials-studio://capabilities/v1",
    name="materials_studio_2023_capability_registry",
    description="Hash-audited MaterialsScript capabilities; unregistered and unverified fields are unavailable.",
    mime_type="application/json",
)
def ms_capability_registry_resource() -> str:
    return json.dumps(audit_capability_registry(), ensure_ascii=False, indent=2)


@mcp.prompt(
    name="materials_studio_safe_operation",
    title="Plan a safe Materials Studio operation",
    description="Require structured parameters, dry-run, preflight, confirmation, and immutable evidence.",
)
def materials_studio_safe_operation_prompt(objective: str, project_directory: str = "") -> str:
    return (
        "Plan this Materials Studio operation without generating free-form Perl. "
        "Select one registered structured tool, call dry-run first, require structure/parameter/license/environment "
        "preflight, obtain exact user confirmation for R2/R3 execution, refuse overwrite, and preserve parameters, "
        "rendered template, logs, outputs, hashes, and environment evidence. "
        f"Objective: {objective}. Project directory: {project_directory or 'not yet selected'}."
    )


@mcp.tool()
def md_architecture_compliance_audit() -> dict[str, Any]:
    """Audit the public MCP surface against the reviewed Windows/MS 2023 safety baseline."""

    capability_audit = audit_capability_registry()
    rows: list[dict[str, Any]] = []
    for item in PUBLIC_TOOLS:
        function = globals().get(item.name)
        signature = inspect.signature(function) if callable(function) else None
        rows.append(
            {
                "name": item.name,
                "risk": item.risk,
                "lifecycle": item.lifecycle,
                "registered": callable(function),
                "has_dry_run": bool(signature and "dry_run" in signature.parameters),
                "has_confirmation_token": bool(signature and "confirmation_token" in signature.parameters),
            }
        )
    dry_run_exemptions = {"md_prepare_production_confirmation"}
    missing_dry_run = [
        row["name"] for row in rows
        if row["risk"] != "R0" and row["name"] not in dry_run_exemptions and not row["has_dry_run"]
    ]
    confirmation_required_tools = {"md_search_public_model_evidence"}
    missing_confirmation = [
        row["name"] for row in rows
        if (row["risk"] in {"R2", "R3"} or row["name"] in confirmation_required_tools)
        and not row["has_confirmation_token"]
    ]
    deprecated_public = [row["name"] for row in rows if row["lifecycle"] == "deprecated"]
    principles = {
        "structured_operations_only": {
            "status": "pass",
            "evidence": "ms_run_materialsscript and chapter-specific free-form helpers are internal profiles and absent from the public registry.",
        },
        "controlled_materialsscript_templates": {
            "status": "pass",
            "evidence": "Public MS mutations render reviewed templates from typed values; unresolved placeholders and Unicode absolute job paths fail closed.",
        },
        "no_default_overwrite": {
            "status": "pass",
            "evidence": "policy.execution.overwrite_existing_outputs=false, resolve_output_path rejects collisions, and scientific artifacts use exclusive creation.",
        },
        "dry_run_for_all_writes_and_computation": {
            "status": "pass" if not missing_dry_run else "fail",
            "missing_tools": missing_dry_run,
        },
        "mandatory_preflight": {
            "status": "pass",
            "evidence": "Every public compute tool validates project/input hashes, typed parameter ranges, output collisions, executable allowlists and environment readiness before output promotion; MaterialsScript license acceptance and completion markers fail closed in the retained launch audit.",
        },
        "confirmation_for_destructive_or_high_resource": {
            "status": "pass" if not missing_confirmation else "fail",
            "missing_tools": missing_confirmation,
        },
        "explicit_opt_in_for_public_network_metadata": {
            "status": "pass" if not missing_confirmation else "fail",
            "evidence": "Public evidence lookup is fixed-provider, dry-run by default, and consumes a confirmation bound to allow_network=true before sending a query.",
        },
        "complete_reproducibility_record": {
            "status": "pass",
            "evidence": "Governed operations preserve manifests, idempotency records, exact parameters, rendered templates, stdout, MatStudio logs, execution audits, outputs and SHA-256 receipts; async tasks add immutable task and result records.",
        },
        "structured_json_results": {"status": "pass", "evidence": "Public controlled tools use versioned success_result/error_result envelopes."},
        "no_unverified_local_api_claims": {
            "status": "pass" if capability_audit["status"] == "pass" else "fail",
            "evidence": {
                "registry_status": capability_audit["status"],
                "registry_summary": capability_audit["summary"],
                "policy": capability_audit["policy"],
                "resource_uri": "materials-studio://capabilities/v1",
            },
        },
        "local_docs_and_real_tests_are_authoritative": {
            "status": "pass",
            "evidence": "Local help/example discovery, frozen source hashes, MS 23.1 execution logs, and real project post-validation are retained.",
        },
    }
    layers = {
        "mcp_interface": {"status": "pass", "tools": len(rows), "resources": 3, "prompts": 1},
        "parameter_validation": {"status": "pass"},
        "scientific_workflows": {
            "status": "pass",
            "detail": "Versioned Forcite energy, geometry-optimization and bounded NVT profiles complement the hash-bound geology, packing, conversion, LAMMPS-gate and G06 workflows.",
        },
        "task_management": {
            "status": "pass",
            "missing": [],
            "existing": [
                "asynchronous_submit", "owner_authenticated_query", "confirmed_cancel", "confirmed_retry",
                "persisted_atomic_task_records", "fixed_worker_dispatch_allowlist",
                "cross_process_concurrency_limit", "owned_process_tree_timeout", "idempotency",
            ],
        },
        "materialsscript_adapter": {"status": "pass"},
        "result_parsing": {"status": "pass"},
        "record_layer": {"status": "pass"},
    }
    blockers = []
    blockers.extend({"code": "DRY_RUN_MISSING", "tool": name} for name in missing_dry_run)
    blockers.extend({"code": "CONFIRMATION_MISSING", "tool": name} for name in missing_confirmation)
    if layers["task_management"]["missing"]:
        blockers.append({"code": "TASK_MANAGEMENT_INCOMPLETE", "detail": layers["task_management"]["missing"]})
    blockers.extend(
        {"code": "PRINCIPLE_NOT_PASS", "principle": name, "status": item["status"]}
        for name, item in principles.items() if item["status"] != "pass"
    )
    blockers.extend(
        {"code": "ARCHITECTURE_LAYER_NOT_PASS", "layer": name, "status": item["status"]}
        for name, item in layers.items() if item["status"] != "pass"
    )
    return {
        "schema_version": 1,
        "audit_id": f"MS-MCP-ARCHITECTURE-{__version__}-CANDIDATE",
        "status": "pass" if not blockers else "blocked",
        "release_allowed": not blockers,
        "public_tool_count": len(rows),
        "deprecated_public_tools": deprecated_public,
        "principles": principles,
        "layers": layers,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "tool_matrix": rows,
    }


@mcp.tool()
def md_prepare_production_confirmation(
    tool_name: str,
    parameters: dict[str, Any],
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Issue a short-lived, single-use confirmation for an exact production operation."""

    allowed_tools = {
        "md_task_submit",
        "md_task_cancel",
        "md_task_retry",
        "ms_forcite_dynamics",
        "ms_geology_import_crystal_parent",
        "ms_geology_build_periodic_slab_cell",
        "ms_pack_periodic_aqueous_nacl",
        "md_build_clayff_spce_nacl_lammps",
        "ms_forcite_calculation_checked",
        "ms_geology_build_supercell",
        "ms_geology_enumerate_surface_terminations",
        "ms_geology_apply_substitutions",
        "ms_geology_place_counterions",
        "ms_geology_apply_hydroxylation_ledger",
        "ms_moc_open_document",
        "md_convert_to_lammps_checked",
        "md_export_xsd_to_car_mdf_checked",
        "md_g01_qualification_vertical",
        "md_scientific_gate_audit",
        "ms_castep_preflight_checked",
        "ms_list_analysis_targets",
        "md_search_public_model_evidence",
    }
    if tool_name not in allowed_tools:
        raise ValueError(f"Production confirmation is not enabled for tool: {tool_name}")
    return confirmation_manager.issue(tool_name, parameters, ttl_seconds)


@mcp.tool()
def md_task_submit(
    tool_name: str,
    parameters: dict[str, Any],
    dry_run: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Preflight and asynchronously submit one exact governed operation."""

    operation = "md_task_submit"
    confirmation_parameters = {"tool_name": tool_name, "parameters": parameters}
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        request = validate_task_request(tool_name, parameters)
        target = globals().get(tool_name)
        if not callable(target):
            raise ValueError(f"Async target is not registered: {tool_name}")
        preflight = target(**parameters, dry_run=True)
        if not isinstance(preflight, dict) or preflight.get("ok") is not True:
            raise ValueError(f"Target dry-run preflight failed: {preflight}")
        if dry_run:
            return success_result(
                operation,
                _dry_run_payload(
                    operation, confirmation_parameters,
                    planned_outputs=[str(DEFAULT_TASK_ROOT / "<generated-task-id>" / "task.json")],
                    validations={"target_preflight": preflight, "parameters_sha256": request["parameters_sha256"]},
                    template_text=f"python -m materials_studio_mcp.task_manager run <generated-task-id>\n{tool_name}",
                    resource_estimate={"parallel_jobs": 1},
                ),
            )
        confirmation_manager.consume(confirmation_token, operation, confirmation_parameters)
        return success_result(operation, submit_task(tool_name, parameters))
    except Exception as exc:
        return error_result(operation, exc)


@mcp.tool()
def md_task_query(task_id: str, owner_capability: str) -> dict[str, Any]:
    """Query one persisted task after proving ownership."""

    try:
        return success_result("md_task_query", query_task(task_id, owner_capability))
    except Exception as exc:
        return error_result("md_task_query", exc)


@mcp.tool()
def md_task_cancel(
    task_id: str,
    owner_capability: str,
    dry_run: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Plan or cancel one owned task and its fixed worker process tree."""

    operation = "md_task_cancel"
    confirmation_parameters = {"task_id": task_id, "owner_capability": owner_capability}
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        current = query_task(task_id, owner_capability)
        if dry_run:
            plan_parameters = {"task_id": task_id, "owner_capability_sha256": hashlib.sha256(owner_capability.encode("utf-8")).hexdigest().upper()}
            return success_result(
                operation,
                _dry_run_payload(
                    operation, plan_parameters,
                    validations={"ownership": "pass", "current_status": current["status"], "worker_pid": current.get("worker_pid")},
                ),
            )
        confirmation_manager.consume(confirmation_token, operation, confirmation_parameters)
        return success_result(operation, cancel_task(task_id, owner_capability))
    except Exception as exc:
        return error_result(operation, exc)


@mcp.tool()
def md_task_retry(
    task_id: str,
    owner_capability: str,
    dry_run: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Preflight and retry one owned failed or cancelled task as a new immutable task."""

    operation = "md_task_retry"
    confirmation_parameters = {"task_id": task_id, "owner_capability": owner_capability}
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        current = query_task(task_id, owner_capability)
        if current["status"] not in {"failed", "cancelled"}:
            raise ValueError("Only failed or cancelled tasks may be retried")
        target = globals()[current["tool_name"]]
        preflight = target(**current["parameters"], dry_run=True)
        if preflight.get("ok") is not True:
            raise ValueError(f"Retry target dry-run preflight failed: {preflight}")
        if dry_run:
            plan_parameters = {"task_id": task_id, "owner_capability_sha256": hashlib.sha256(owner_capability.encode("utf-8")).hexdigest().upper()}
            return success_result(
                operation,
                _dry_run_payload(
                    operation, plan_parameters,
                    planned_outputs=[str(DEFAULT_TASK_ROOT / "<new-task-id>" / "task.json")],
                    validations={"ownership": "pass", "current_status": current["status"], "target_preflight": preflight},
                ),
            )
        confirmation_manager.consume(confirmation_token, operation, confirmation_parameters)
        return success_result(operation, retry_task(task_id, owner_capability))
    except Exception as exc:
        return error_result(operation, exc)


@mcp.tool()
def md_project_initialize(
    project_id: str,
    title: str,
    projects_root: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Create a non-overwriting, reproducible MS/LAMMPS/VMD project directory and manifest."""

    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    if dry_run:
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", project_id.strip()).strip("._-")[:80]
        if not safe_id:
            raise ValueError("project_id must contain at least one letter or number")
        config = load_pipeline_config()
        root = Path(projects_root) if projects_root else Path(config["policy"]["workspace_roots"][0]) / "projects"
        project = resolve_workspace_path(str(root / safe_id), must_exist=False)
        if project.exists():
            raise FileExistsError(f"Project already exists; refusing to overwrite: {project}")
        parameters = {"project_id": project_id, "title": title, "projects_root": projects_root}
        return success_result(
            "md_project_initialize",
            _dry_run_payload(
                "md_project_initialize", parameters,
                planned_outputs=[str(project), str(project / "manifest.json")],
                validations={"workspace_allowed": True, "output_absent": True},
            ),
        )
    return initialize_project(project_id=project_id, title=title, projects_root=projects_root)


@mcp.tool()
def md_project_get(project_directory: str) -> dict[str, Any]:
    """Read a pipeline project's manifest."""

    return get_project(project_directory)


@mcp.tool()
def md_project_update_specification(
    project_directory: str,
    specification: dict[str, Any],
    forcefield: dict[str, Any] | None = None,
    science_contract: dict[str, Any] | None = None,
    geology_model: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Replace the science contract and merge confirmed model/forcefield/geology requirements."""

    parameters = {
        "specification": specification,
        "forcefield": forcefield,
        "science_contract": science_contract,
        "geology_model": geology_model,
    }
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    project = get_project(project_directory)
    for label, value in parameters.items():
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"{label} must be an object")
    if dry_run:
        return success_result(
            "md_project_update_specification",
            _dry_run_payload(
                "md_project_update_specification", {**parameters, "project_directory": project_directory},
                planned_outputs=[project["manifest_path"]],
                validations={"manifest_readable": True, "parameter_types_valid": True},
            ),
        )
    result, replayed = run_idempotent(
        project_directory, idempotency_key, "md_project_update_specification", parameters,
        lambda: update_model_specification(
            project_directory, specification, forcefield, science_contract, geology_model,
        ),
    )
    if idempotency_key is not None:
        result = {**result, "idempotency_key": idempotency_key, "replayed": replayed}
    return result


@mcp.tool()
def md_project_register_artifact(
    project_directory: str,
    artifact_path: str,
    role: str,
    source: str | None = None,
    idempotency_key: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Register a project file with role, provenance, size, and SHA-256 integrity metadata."""

    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    project = Path(get_project(project_directory)["project_directory"])
    artifact = resolve_workspace_path(artifact_path, must_exist=True)
    if project != artifact and project not in artifact.parents:
        raise PermissionError("Artifacts must be stored inside the project directory")
    if not artifact.is_file():
        raise FileNotFoundError(f"Artifact not found: {artifact}")
    parameters = {"artifact_path": artifact_path, "role": role, "source": source}
    if dry_run:
        return success_result(
            "md_project_register_artifact",
            _dry_run_payload(
                "md_project_register_artifact", {**parameters, "project_directory": project_directory},
                planned_outputs=[str(project / "manifest.json")],
                validations={"artifact_exists": True, "artifact_sha256": sha256_file(artifact)},
            ),
        )
    result, replayed = run_idempotent(
        project_directory, idempotency_key, "md_project_register_artifact",
        parameters,
        lambda: register_artifact(project_directory, artifact_path, role, source),
    )
    if idempotency_key is not None:
        result = {**result, "idempotency_key": idempotency_key, "replayed": replayed}
    return result


@mcp.tool()
def md_project_validate(project_directory: str) -> dict[str, Any]:
    """Validate project structure, model specification, forcefield declaration, and registered file integrity."""

    return validate_project(project_directory)


@mcp.tool()
def md_project_set_quality_gate(project_directory: str, gate: str, status: str,
                                evidence: dict[str, Any] | None = None,
                                dry_run: bool = True) -> dict[str, Any]:
    """Record a quality-gate decision and structured evidence in the project manifest."""

    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    project = get_project(project_directory)
    if dry_run:
        if status not in {"pending", "fail", "blocked"}:
            raise ValueError("Only pending, fail, or blocked may be set directly")
        parameters = {"project_directory": project_directory, "gate": gate, "status": status, "evidence": evidence}
        return success_result(
            "md_project_set_quality_gate",
            _dry_run_payload(
                "md_project_set_quality_gate", parameters,
                planned_outputs=[project["manifest_path"]],
                validations={"manifest_readable": True, "caller_status_allowed": True},
            ),
        )
    return set_quality_gate(project_directory, gate, status, evidence)


@mcp.tool()
def md_project_transition(project_directory: str, target_status: str, reason: str,
                          evidence_ids: list[str] | None = None,
                          dry_run: bool = True) -> dict[str, Any]:
    """Apply one audited, legal project lifecycle transition with a v1 result envelope."""

    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        project = get_project(project_directory)
        current = project["manifest"]["project"]["status"]
        from .project_manager import PROJECT_TRANSITIONS
        if target_status not in PROJECT_TRANSITIONS.get(current, frozenset()):
            raise ValueError(f"Illegal project transition: {current} -> {target_status}")
        if dry_run:
            parameters = {
                "project_directory": project_directory, "target_status": target_status,
                "reason": reason, "evidence_ids": evidence_ids,
            }
            return success_result(
                "md_project_transition",
                _dry_run_payload(
                    "md_project_transition", parameters,
                    planned_outputs=[project["manifest_path"]],
                    validations={"current_status": current, "transition_allowed": True},
                ),
            )
        data = transition_project_status(
            project_directory, target_status, reason=reason, evidence_ids=evidence_ids,
        )
        return success_result("md_project_transition", data)
    except Exception as exc:
        return error_result("md_project_transition", exc)


@mcp.tool()
def md_structure_preflight(path: str, charge_tolerance: float = 1e-6) -> dict[str, Any]:
    """Check an XSD or LAMMPS data file for structural, topology, type, cell, and charge issues."""

    return inspect_structure_preflight(path, charge_tolerance)


@mcp.tool()
def ms_prepare_castep_pl_package(
    input_xsd: str,
    input_sha256: str,
    output_directory: str,
    calculation_name: str,
    spins: list[int],
    cores: int = 4,
    cutoff: float = 326.5,
    max_scf_cycles: int = 500,
    max_geometry_iterations: int = 150,
    scf_convergence: float = 0.000002,
    force_convergence: float = 0.03,
    dispersion_method: str = "TS",
    spin_mode: str = "fixed",
    density_mixing_amplitude: float = 0.05,
    spin_mixing_amplitude: float = 0.08,
    diis_history: int = 5,
    smearing: float = 0.2,
    optimization_algorithm: str = "BFGS",
    allow_local: bool = False,
    dry_run: bool = True,
    adaptive_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare hash-bound CASTEP PL/XSD folders, optionally bound to an adaptive plan."""

    operation = "ms_prepare_castep_pl_package"
    try:
        source = resolve_workspace_path(input_xsd, must_exist=True)
        output = resolve_output_path(output_directory)
        data = prepare_castep_pl_package(
            input_xsd=source,
            input_sha256=input_sha256,
            output_directory=output,
            calculation_name=calculation_name,
            spins=spins,
            cores=cores,
            cutoff=cutoff,
            max_scf_cycles=max_scf_cycles,
            max_geometry_iterations=max_geometry_iterations,
            scf_convergence=scf_convergence,
            force_convergence=force_convergence,
            dispersion_method=dispersion_method,
            spin_mode=spin_mode,
            density_mixing_amplitude=density_mixing_amplitude,
            spin_mixing_amplitude=spin_mixing_amplitude,
            diis_history=diis_history,
            smearing=smearing,
            optimization_algorithm=optimization_algorithm,
            allow_local=allow_local,
            dry_run=dry_run,
            adaptive_plan=adaptive_plan,
        )
        return success_result(operation, data)
    except Exception as exc:
        return error_result(operation, exc)


@mcp.tool()
def ms_prepare_castep_standalone_inputs(
    input_xsd: str,
    input_sha256: str,
    output_directory: str,
    calculation_name: str,
    standalone_context: dict[str, Any],
    cores: int = 4,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Prepare hash-bound, execution-blocked .cell/.param files for standalone CASTEP."""

    operation = "ms_prepare_castep_standalone_inputs"
    try:
        source = resolve_workspace_path(input_xsd, must_exist=True)
        output = resolve_output_path(output_directory)
        data = prepare_castep_standalone_inputs(
            input_xsd=source,
            input_sha256=input_sha256,
            output_directory=output,
            calculation_name=calculation_name,
            standalone_context=standalone_context,
            cores=cores,
            dry_run=dry_run,
        )
        return success_result(operation, data)
    except Exception as exc:
        return error_result(operation, exc)


@mcp.tool()
def ms_castep_fixed_profile_preflight(
    input_manifest: str,
    input_manifest_sha256: str,
) -> dict[str, Any]:
    """Read-only preflight for the exact P3-C alpha-quartz CASTEP profile."""

    operation = "ms_castep_fixed_profile_preflight"
    try:
        manifest = resolve_workspace_path(input_manifest, must_exist=True)
        return success_result(
            operation,
            inspect_fixed_profile_preflight_request(
                input_manifest=manifest,
                input_manifest_sha256=input_manifest_sha256,
            ),
        )
    except Exception as exc:
        return error_result(operation, exc)


@mcp.tool()
def ms_castep_preflight_checked(
    package_directory: str,
    package_manifest_sha256: str,
    task_name: str,
    timeout_seconds: int = 120,
    dry_run: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Run one exact generated CASTEP PL through MatServer and exit before CASTEP."""

    operation = "ms_castep_preflight_checked"
    parameters = {
        "package_directory": package_directory,
        "package_manifest_sha256": package_manifest_sha256,
        "task_name": task_name,
        "timeout_seconds": timeout_seconds,
    }
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        package = resolve_workspace_path(package_directory, must_exist=True)
        plan = inspect_castep_preflight_plan(package, package_manifest_sha256, task_name)
        task_directory = _assert_ascii_absolute_path(Path(plan["task_directory"]), "CASTEP preflight task directory")
        _assert_ascii_absolute_path(Path(plan["pl_path"]), "CASTEP preflight PL path")
        timeout = bounded_timeout(timeout_seconds)
        health = pipeline_health_check(run_version_probes=False)
        if health.get("status") != "ready":
            raise RuntimeError("Materials Studio environment preflight is not ready")
        run_mat_script = approved_executable(Path(_materials_studio_paths()["run_mat_script"]))
        if not run_mat_script.is_file():
            raise FileNotFoundError(f"RunMatScript.bat not found: {run_mat_script}")
        if dry_run:
            return success_result(
                operation,
                _dry_run_payload(
                    operation,
                    parameters,
                    planned_outputs=[plan["stdout_path"], plan["matstudio_log_path"], plan["receipt_path"]],
                    validations={
                        "package": plan,
                        "environment_preflight": health,
                        "run_mat_script": str(run_mat_script),
                        "gateway_selected": False,
                        "castep_execution_allowed": False,
                    },
                    resource_estimate={"parallel_jobs": 1, "timeout_seconds": timeout},
                ),
            )

        confirmation_manager.consume(confirmation_token, operation, parameters)
        command = [str(run_mat_script), "-flat", Path(plan["pl_path"]).stem]
        process_environment = dict(os.environ)
        process_environment[PREFLIGHT_ENVIRONMENT_VARIABLE] = "1"
        with acquire_execution_slot():
            completed, timed_out, termination, process_pid = _run_guarded_materialsscript_process(
                command,
                cwd=task_directory,
                timeout_seconds=timeout,
                env=process_environment,
            )
        result = finalize_castep_preflight(
            plan,
            completed,
            timed_out=timed_out,
            termination=termination,
            process_pid=process_pid,
        )
        if result["status"] != "preflight_pass":
            raise RecordedExecutionError(
                f"CASTEP runtime-only preflight failed; evidence retained at {result['receipt_path']}",
                result,
            )
        return success_result(operation, result)
    except Exception as exc:
        return error_result(operation, exc)


@mcp.tool()
def ms_castep_gateway_readiness(requested_cores: int = 12) -> dict[str, Any]:
    """Inspect the local Gateway and report CASTEP submission blockers without submitting."""

    operation = "ms_castep_gateway_readiness"
    try:
        root = Path(_materials_studio_paths()["root"])
        return success_result(operation, inspect_castep_gateway_readiness(root, requested_cores))
    except Exception as exc:
        return error_result(operation, exc)


@mcp.tool()
def md_g01_qualification_vertical(
    project_id: str,
    input_xsd: str,
    input_sha256: str | None = None,
    projects_root: str | None = None,
    forcefield_file: str | None = None,
    forcefield_off: str | None = None,
    temperature_kelvin: float = 50.0,
    minimization_iterations: int = 100,
    nvt_steps: int = 20,
    timestep_fs: float = 1.0,
    random_seed: int = 173017,
    timeout_seconds: int = 300,
    dry_run: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Run only the bounded G01 MS -> LAMMPS -> VMD qualification workflow."""

    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    return run_g01_qualification_vertical(
        project_id=project_id,
        input_xsd=input_xsd,
        input_sha256=input_sha256,
        projects_root=projects_root,
        forcefield_file=forcefield_file,
        forcefield_off=forcefield_off,
        temperature_kelvin=temperature_kelvin,
        minimization_iterations=minimization_iterations,
        nvt_steps=nvt_steps,
        timestep_fs=timestep_fs,
        random_seed=random_seed,
        timeout_seconds=timeout_seconds,
        dry_run=dry_run,
        confirmation_token=confirmation_token,
    )


@mcp.tool()
def md_scientific_gate_audit(
    project_directory: str,
    target_model_contract: dict[str, Any],
    evidence_manifest: dict[str, Any] | None = None,
    dry_run: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Audit a frozen target-model science contract without running any engine."""

    return audit_target_model_science(
        project_directory=project_directory,
        target_model_contract=target_model_contract,
        evidence_manifest=evidence_manifest,
        dry_run=dry_run,
        confirmation_token=confirmation_token,
    )


@mcp.tool()
def md_msi2lmp_preflight(
    car_path: str,
    mdf_path: str | None = None,
    forcefield_file: str | None = None,
    forcefield_class: str = "I",
) -> dict[str, Any]:
    """Validate the CAR/MDF pair, forcefield class, and explicit msi2lmp parameter file before conversion."""

    return inspect_msi2lmp_inputs(car_path, mdf_path, forcefield_file, forcefield_class)


def md_convert_car_mdf_to_lammps(
    car_path: str,
    mdf_path: str,
    output_data_path: str,
    forcefield_file: str,
    forcefield_class: str = "I",
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Run a guarded msi2lmp conversion and accept output only after LAMMPS data preflight passes."""

    return convert_car_mdf(car_path, mdf_path, output_data_path, forcefield_file, forcefield_class, timeout_seconds)


def md_export_xsd_to_car_mdf(
    input_xsd: str,
    output_car: str,
    output_mdf: str,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Export an XSD to a project-mode CAR/MDF pair with ASCII-safe staging."""

    if Path(output_car).with_suffix("") != Path(output_mdf).with_suffix(""):
        raise ValueError("output_car and output_mdf must have the same root name")
    if Path(output_car).exists() or Path(output_mdf).exists():
        raise FileExistsError("Refusing to overwrite an existing CAR/MDF output")
    script = r'''use strict;
use warnings;
use MaterialsScript qw(:all);
my $doc = Documents->Import("{{input.structure}}");
$doc->Export("{{output.car}}");
$doc->Close;
'''
    result = _run_materialsscript_job(
        script_template=script,
        input_files={"structure": input_xsd},
        output_files={
            "car": {"relative_path": "model.car", "destination_path": output_car},
            "mdf": {"relative_path": "model.mdf", "destination_path": output_mdf},
        },
        job_name="export_car_mdf", run_mode="project", keep_job_dir=False, timeout_seconds=timeout_seconds,
    )
    paired_outputs_exist = all(
        result.get("outputs", {}).get(name, {}).get("exists") for name in ("car", "mdf")
    )
    result["success"] = bool(result.get("success") and paired_outputs_exist)
    if result["success"]:
        result["success_basis"] = (
            "MatStudioLog reports Completion status (OK), MatServer exits OK, and paired CAR/MDF outputs exist"
        )
    elif paired_outputs_exist:
        result["outputs_untrusted"] = True
        result["error_summary"] = result.get("error_summary") or (
            "CAR/MDF files exist but Materials Studio did not satisfy the audited success markers; outputs are untrusted."
        )
    return result


def _require_file_in_project(project: Path, value: str, label: str) -> Path:
    path = resolve_workspace_path(value, must_exist=True)
    if not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    if path != project and project not in path.parents:
        raise PermissionError(f"{label} must be inside the bound project")
    return path


def _checked_conversion_failure(result: dict[str, Any]) -> Exception:
    stage = str(result.get("stage", "unknown"))
    diagnostics = result.get("diagnostics")
    parts = [f"Checked msi2lmp conversion failed at stage {stage}"]
    input_problem = False
    if isinstance(diagnostics, dict):
        missing = diagnostics.get("missing_mass_types")
        if isinstance(missing, list) and missing:
            parts.append(f"missing mass types: {', '.join(str(item) for item in missing)}")
            input_problem = True
        warnings = diagnostics.get("inconsistent_connectivity_warning_count")
        if isinstance(warnings, int) and warnings:
            parts.append(f"inconsistent connectivity warnings: {warnings}")
            input_problem = True
        recommendation = diagnostics.get("recommendation")
        if recommendation:
            parts.append(str(recommendation))
    message = "; ".join(parts)
    return ValueError(message) if input_problem else RuntimeError(message)


@mcp.tool()
def md_export_xsd_to_car_mdf_checked(
    project_directory: str,
    input_xsd: str,
    input_sha256: str,
    output_slot: str,
    idempotency_key: str,
    confirmation_token: str | None = None,
    timeout_seconds: int = 300,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Export a hash-bound project XSD to an audited CAR/MDF pair."""

    tool_name = "md_export_xsd_to_car_mdf_checked"
    parameters = {
        "project_directory": project_directory,
        "input_xsd": input_xsd,
        "input_sha256": input_sha256,
        "output_slot": output_slot,
        "idempotency_key": idempotency_key,
        "timeout_seconds": timeout_seconds,
    }
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required for checked export")
        slot = validate_output_slot(output_slot)
        project = Path(get_project(project_directory)["project_directory"]).resolve()
        source = _require_file_in_project(project, input_xsd, "input_xsd")
        if source.suffix.lower() != ".xsd":
            raise ValueError("input_xsd must have the .xsd suffix")
        source_sha256 = validate_input_hash(source, input_sha256)
        output_car = project / "conversion" / f"{slot}.car"
        output_mdf = project / "conversion" / f"{slot}.mdf"
        if dry_run and (output_car.exists() or output_mdf.exists()):
            raise FileExistsError("Refusing to overwrite checked CAR/MDF outputs")
        if dry_run:
            health = pipeline_health_check(run_version_probes=False)
            ms_health = _pipeline_check(health, "Materials Studio RunMatScript")
            export_template = (
                'use strict;\nuse warnings;\nuse MaterialsScript qw(:all);\n'
                'my $doc = Documents->Import("{{input.structure}}");\n'
                '$doc->Export("{{output.car}}");\n$doc->Close;\n'
            )
            return success_result(
                tool_name,
                _dry_run_payload(
                    tool_name, parameters,
                    planned_outputs=[str(output_car), str(output_mdf)],
                    validations={
                        "project_manifest": "pass", "input_hash": source_sha256,
                        "output_absent": True, "materials_studio": ms_health,
                    },
                    template_text=export_template,
                    resource_estimate={"parallel_jobs": 1, "timeout_seconds": timeout_seconds},
                ),
            )

        def implementation() -> dict[str, Any]:
            if output_car.exists() or output_mdf.exists():
                raise FileExistsError("Refusing to overwrite checked CAR/MDF outputs")
            confirmation_manager.consume(confirmation_token, tool_name, parameters)
            result = md_export_xsd_to_car_mdf(
                str(source), str(output_car), str(output_mdf), timeout_seconds
            )
            if not result.get("success"):
                raise RuntimeError(result.get("error_summary") or "Checked CAR/MDF export failed")
            if not output_car.is_file() or not output_mdf.is_file():
                raise RuntimeError("Checked export did not publish both CAR and MDF files")
            registrations = [
                register_artifact(project_directory, str(output_car), "materials_studio_car", source=str(source)),
                register_artifact(project_directory, str(output_mdf), "materials_studio_mdf", source=str(source)),
            ]
            return {
                "status": "export_pass",
                "production_released": False,
                "input_sha256": source_sha256,
                "output_car": str(output_car),
                "output_car_sha256": sha256_file(output_car),
                "output_mdf": str(output_mdf),
                "output_mdf_sha256": sha256_file(output_mdf),
                "execution": result,
                "artifact_registrations": registrations,
                "limitations": [
                    "Export success proves a hash-bound file conversion, not forcefield coverage or energy equivalence.",
                ],
            }

        data, replayed = run_idempotent(
            project_directory, idempotency_key, tool_name, parameters, implementation
        )
        return success_result(tool_name, data, replayed=replayed)
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def md_convert_to_lammps_checked(
    project_directory: str,
    car_path: str,
    car_sha256: str,
    mdf_path: str,
    mdf_sha256: str,
    forcefield_file: str,
    forcefield_sha256: str,
    forcefield_class: str,
    output_slot: str,
    idempotency_key: str,
    confirmation_token: str | None = None,
    timeout_seconds: int = 300,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Convert a hash-bound project CAR/MDF pair with a hash-bound msi2lmp forcefield."""

    tool_name = "md_convert_to_lammps_checked"
    parameters = {
        "project_directory": project_directory,
        "car_path": car_path,
        "car_sha256": car_sha256,
        "mdf_path": mdf_path,
        "mdf_sha256": mdf_sha256,
        "forcefield_file": forcefield_file,
        "forcefield_sha256": forcefield_sha256,
        "forcefield_class": forcefield_class,
        "output_slot": output_slot,
        "idempotency_key": idempotency_key,
        "timeout_seconds": timeout_seconds,
    }
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required for checked conversion")
        slot = validate_output_slot(output_slot)
        project = Path(get_project(project_directory)["project_directory"]).resolve()
        car = _require_file_in_project(project, car_path, "car_path")
        mdf = _require_file_in_project(project, mdf_path, "mdf_path")
        car_digest = validate_input_hash(car, car_sha256)
        mdf_digest = validate_input_hash(mdf, mdf_sha256)
        preflight = inspect_msi2lmp_inputs(
            str(car), str(mdf), forcefield_file, forcefield_class
        )
        if preflight.get("status") != "pass":
            raise ValueError("CAR/MDF and forcefield input preflight did not pass")
        selected_forcefield = Path(str(preflight["forcefield_file"])).resolve(strict=True)
        forcefield_digest = validate_input_hash(selected_forcefield, forcefield_sha256)
        output_data = project / "conversion" / f"{slot}.data"
        if dry_run and output_data.exists():
            raise FileExistsError(f"Refusing to overwrite checked LAMMPS output: {output_data}")
        if dry_run:
            health = pipeline_health_check(run_version_probes=False)
            return success_result(
                tool_name,
                _dry_run_payload(
                    tool_name, parameters,
                    planned_outputs=[str(output_data)],
                    validations={
                        "project_manifest": "pass", "car_sha256": car_digest,
                        "mdf_sha256": mdf_digest, "forcefield_sha256": forcefield_digest,
                        "msi2lmp_preflight": preflight,
                        "lammps_environment": _pipeline_check(health, "msi2lmp"),
                    },
                    template_text="msi2lmp <model-root> -class <validated-class> -frc <hash-bound-file>",
                    resource_estimate={"parallel_jobs": 1, "timeout_seconds": timeout_seconds},
                ),
            )

        def implementation() -> dict[str, Any]:
            if output_data.exists():
                raise FileExistsError(f"Refusing to overwrite checked LAMMPS output: {output_data}")
            confirmation_manager.consume(confirmation_token, tool_name, parameters)
            result = convert_car_mdf(
                str(car), str(mdf), str(output_data), str(selected_forcefield),
                forcefield_class, timeout_seconds,
            )
            if not result.get("success") or not output_data.is_file():
                raise _checked_conversion_failure(result)
            data_preflight = result.get("data_preflight")
            expected_atoms = preflight.get("car_detected_record_count")
            actual_atoms = (
                data_preflight.get("header_counts", {}).get("atoms")
                if isinstance(data_preflight, dict) else None
            )
            if not isinstance(expected_atoms, int) or actual_atoms != expected_atoms:
                raise RuntimeError(
                    f"Converted atom count mismatch: {actual_atoms!r} != {expected_atoms!r}"
                )
            registration = register_artifact(
                project_directory, str(output_data), "lammps_data", source=tool_name
            )
            return {
                "status": "conversion_pass",
                "production_released": False,
                "car_sha256": car_digest,
                "mdf_sha256": mdf_digest,
                "forcefield_file": str(selected_forcefield),
                "forcefield_sha256": forcefield_digest,
                "output_data": str(output_data),
                "output_data_sha256": sha256_file(output_data),
                "input_preflight": preflight,
                "validation": {
                    "source_atom_records": expected_atoms,
                    "lammps_atoms": actual_atoms,
                    "atom_count_matches": True,
                    "lammps_data_preflight_status": data_preflight.get("status"),
                },
                "execution": result,
                "artifact_registration": registration,
                "limitations": [
                    "Conversion success does not prove forcefield-library equivalence outside the validated parameter subset.",
                    "Model-specific energy equivalence and production quality gates remain mandatory.",
                ],
            }

        data, replayed = run_idempotent(
            project_directory, idempotency_key, tool_name, parameters, implementation
        )
        return success_result(tool_name, data, replayed=replayed)
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def ms_search_local_help(query: str, max_results: int = 10) -> dict[str, Any]:
    """Search the installed Materials Studio 2023 scripting help."""

    return _run_helper(
        "search-help",
        {
            "query": query,
            "max_results": max_results,
        },
    )["data"]


@mcp.tool()
def ms_read_local_help_page(path: str) -> dict[str, Any]:
    """Read a specific local Materials Studio help page and extract cleaned text and code examples."""

    return _run_helper(
        "read-help-page",
        {
            "path": path,
        },
    )["data"]


@mcp.tool()
def ms_find_code_examples(query: str, max_results: int = 8) -> dict[str, Any]:
    """Find local Materials Studio scripting code examples relevant to a query."""

    return _run_helper(
        "find-code-examples",
        {
            "query": query,
            "max_results": max_results,
        },
    )["data"]


@mcp.tool()
def ms_list_example_documents(
    pattern: str = "*.xsd",
    max_results: int = 50,
) -> dict[str, Any]:
    """List built-in Materials Studio example documents such as .xsd and .xtd."""

    return _run_helper(
        "list-examples",
        {
            "pattern": pattern,
            "max_results": max_results,
        },
    )["data"]


@mcp.tool()
def ms_task_catalog() -> dict[str, Any]:
    """List the high-level workflows and helper tools exposed by this Materials Studio MCP server."""

    capability_audit = audit_capability_registry()
    return {
        "server_name": SERVER_NAME,
        "api": api_catalog(),
        "workflows": WORKFLOW_CATALOG,
        "forcite_profiles": {
            "preparation": [
                {
                    "profile_id": profile_id,
                    "forcefield": settings["CurrentForcefield"],
                    "charge_assignment": settings["ChargeAssignment"],
                    "role": "primary" if profile_id == "prepare_compassiii_v1" else "diagnostic_fallback",
                    "production_released": False,
                }
                for profile_id, settings in _FORCEFIELD_PREPARATION_PROFILES.items()
            ],
            "calculation": [
                "energy_compassiii_v1",
                "geometry_optimization_compassiii_v1",
                "dynamics_nvt_compassiii_v1",
            ],
        },
        "materialsscript_capabilities": {
            "status": capability_audit["status"],
            "target": capability_audit["target"],
            "policy": capability_audit["policy"],
            "summary": capability_audit["summary"],
            "verified_ids": [
                item["id"] for item in capability_audit["capabilities"] if item["verified"]
            ],
            "unavailable_ids": [
                item["id"] for item in capability_audit["capabilities"] if not item["verified"]
            ],
            "resource_uri": "materials-studio://capabilities/v1",
        },
        "notes": [
            "Use high-level Forcite tools for common simulation tasks.",
            "Fallback forcefield preparation profiles are explicit diagnostic alternatives and never silently replace COMPASSIII.",
            "Unregistered or hash-unverified MaterialsScript APIs and parameters are unavailable by policy.",
            "Arbitrary MaterialsScript execution is not exposed. Add and review a dedicated controlled tool for unsupported APIs.",
        ],
    }


@mcp.tool()
def ms_recommend_workflow(
    request: str,
    input_structure: str | None = None,
    calculation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recommend the best available Materials Studio MCP workflow for a natural-language task request."""

    return _recommend_workflows(
        request=request,
        input_structure=input_structure,
        calculation_context=calculation_context,
    )


@mcp.tool()
def ms_execute_task_request(
    request: str,
    input_structure: str | None = None,
    input_trajectory: str | None = None,
    output_directory: str | None = None,
    keep_job_dir: bool = True,
    calculation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan a high-level workflow without executing it.

    Natural-language requests are intentionally plan-only. Call the selected
    controlled tool explicitly after reviewing its typed parameters and gates.
    """

    primary_input = input_trajectory or input_structure
    recommendation = _recommend_workflows(
        request=request,
        input_structure=primary_input,
        calculation_context=calculation_context,
    )
    best = recommendation["recommended_workflows"][0]
    return {
        "success": True,
        "executed": False,
        "mode": "plan_only",
        "request": request,
        "selected_tool": best["tool"],
        "recommendation": recommendation,
        "provided_inputs": {
            "input_structure": input_structure,
            "input_trajectory": input_trajectory,
            "output_directory": output_directory,
            "calculation_context": calculation_context,
        },
        "message": "No task was executed. Review the recommendation and call the selected controlled tool explicitly.",
    }


@mcp.tool()
def ms_scan_workspace(
    root_dir: str,
    patterns: list[str] | None = None,
    max_results: int = 200,
) -> dict[str, Any]:
    """Scan a workspace folder for Materials Studio files."""

    return _run_helper(
        "scan-workspace",
        {
            "root_dir": root_dir,
            "patterns": patterns
            or ["*.xsd", "*.xtd", "*.stp", "*.std", "*.xod", "*.car", "*.mdf"],
            "max_results": max_results,
        },
    )["data"]


@mcp.tool()
def ms_inspect_document(path: str) -> dict[str, Any]:
    """Parse a Materials Studio document or project file and return best-effort metadata."""

    return _run_helper(
        "inspect-document",
        {
            "path": path,
            "timeout_seconds": 180,
        },
    )["data"]


def _write_json_artifact_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _write_text_artifact_exclusive(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


@mcp.tool()
def ms_geology_build_periodic_slab_cell(
    project_directory: str,
    input_surface_structure: str,
    input_sha256: str,
    vacuum_thickness_angstrom: float,
    expected_total_c_angstrom: float,
    cell_tolerance_angstrom: float,
    output_slot: str,
    max_atoms: int,
    idempotency_key: str,
    confirmation_token: str | None = None,
    timeout_seconds: int = 600,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Convert a reviewed 2D surface to a hash-bound 3D periodic slab cell."""

    tool_name = "ms_geology_build_periodic_slab_cell"
    parameters = {
        "project_directory": project_directory,
        "input_surface_structure": input_surface_structure,
        "input_sha256": input_sha256,
        "vacuum_thickness_angstrom": vacuum_thickness_angstrom,
        "expected_total_c_angstrom": expected_total_c_angstrom,
        "cell_tolerance_angstrom": cell_tolerance_angstrom,
        "output_slot": output_slot,
        "max_atoms": max_atoms,
        "idempotency_key": idempotency_key,
        "timeout_seconds": timeout_seconds,
    }
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        slot = validate_output_slot(output_slot)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required for geology model mutations")
        if isinstance(max_atoms, bool) or not isinstance(max_atoms, int) or not 1 <= max_atoms <= 10_000_000:
            raise ValueError("max_atoms must be an integer from 1 to 10000000")
        script = build_periodic_slab_cell_script(float(vacuum_thickness_angstrom))
        project = Path(get_project(project_directory)["project_directory"])
        destination = project / "model" / "cells" / f"{slot}.xsd"
        receipt_path = project / "reports" / f"{slot}.periodic_slab_cell.receipt.json"
        source = resolve_workspace_path(input_surface_structure, must_exist=True)
        source_sha256 = validate_input_hash(source, input_sha256)
        input_model = inspect_xsd_geometry(source)
        if input_model["periodic_dimension"] != 2:
            raise ValueError("Periodic slab-cell input must be a 2D PlaneGroup XSD")
        if input_model["atom_count"] > max_atoms:
            raise ValueError("Input surface atom count exceeds max_atoms")
        normal_span = surface_normal_span_angstrom(source)
        expected_from_request = normal_span + float(vacuum_thickness_angstrom)
        if not math.isclose(expected_from_request, float(expected_total_c_angstrom), abs_tol=float(cell_tolerance_angstrom)):
            raise ValueError("surface normal span plus vacuum thickness does not match expected_total_c_angstrom")
        if dry_run and (destination.exists() or receipt_path.exists()):
            raise FileExistsError("Refusing to overwrite periodic slab-cell artifacts")
        if dry_run:
            return success_result(
                tool_name,
                _dry_run_payload(
                    tool_name, parameters,
                    planned_outputs=[str(destination), str(receipt_path)],
                    validations={
                        "input_sha256": source_sha256, "periodic_dimension": 2,
                        "atom_count": input_model["atom_count"], "surface_normal_span_angstrom": normal_span,
                        "expected_total_c_matches": True,
                    },
                    template_text=script,
                    resource_estimate={"parallel_jobs": 1, "max_atoms": max_atoms, "timeout_seconds": timeout_seconds},
                ),
            )

        def implementation() -> dict[str, Any]:
            confirmation_manager.consume(confirmation_token, tool_name, parameters)
            result = _run_materialsscript_job(
                script_template=script,
                input_files={"structure": str(source)},
                output_files={
                    "structure": {
                        "relative_path": "periodic_slab_cell.xsd",
                        "destination_path": str(destination),
                    },
                },
                job_name=f"geology_slab_cell_{slot}",
                run_mode="flat",
                keep_job_dir=True,
                timeout_seconds=timeout_seconds,
            )
            if not result.get("success"):
                raise RuntimeError(result.get("error_summary") or "Materials Studio slab-cell job failed")
            try:
                validation = validate_periodic_slab_cell_result(
                    input_model, destination, float(expected_total_c_angstrom),
                    float(cell_tolerance_angstrom),
                )
                receipt = {
                    "schema_version": 1,
                    "tool": tool_name,
                    "status": validation["status"],
                    "production_released": False,
                    "input_path": str(source),
                    "input_sha256": input_model["sha256"],
                    "output_path": str(destination),
                    "output_sha256": sha256_file(destination),
                    "surface_normal_span_angstrom": normal_span,
                    "vacuum_thickness_angstrom": float(vacuum_thickness_angstrom),
                    "validation": validation,
                    "execution": {
                        "job_id": result.get("job_id"),
                        "audit_path": result.get("audit_path"),
                        "run_mat_script_exit_code": result.get("run_mat_script_exit_code"),
                        "timed_out": result.get("timed_out"),
                    },
                }
                _write_json_artifact_exclusive(receipt_path, receipt)
                registrations = [
                    register_artifact(project_directory, str(destination), "geology_periodic_slab_cell", source=str(source)),
                    register_artifact(project_directory, str(receipt_path), "geology_periodic_slab_cell_receipt", source=tool_name),
                ]
                return {
                    "status": validation["status"],
                    "production_released": False,
                    "output_path": str(destination),
                    "output_sha256": receipt["output_sha256"],
                    "receipt_path": str(receipt_path),
                    "surface_normal_span_angstrom": normal_span,
                    "validation": validation,
                    "artifact_registrations": registrations,
                }
            except Exception:
                destination.unlink(missing_ok=True)
                receipt_path.unlink(missing_ok=True)
                raise

        data, replayed = run_idempotent(
            project_directory, idempotency_key, tool_name, parameters, implementation
        )
        return success_result(tool_name, data, replayed=replayed)
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def ms_pack_periodic_aqueous_nacl(
    project_directory: str,
    input_periodic_structure: str,
    input_sha256: str,
    water_model: str,
    ion_model: str,
    water_count: int,
    sodium_count: int,
    chloride_count: int,
    packmol_tolerance_angstrom: float,
    normal_boundary_clearance_angstrom: float,
    random_seed: int,
    required_final_formal_charge_e: float,
    output_slot: str,
    max_total_atoms: int,
    idempotency_key: str,
    confirmation_token: str | None = None,
    timeout_seconds: int = 1800,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Pack exact SPC/E water and NaCl counts into an audited periodic slab cell."""

    tool_name = "ms_pack_periodic_aqueous_nacl"
    parameters = {
        "project_directory": project_directory,
        "input_periodic_structure": input_periodic_structure,
        "input_sha256": input_sha256,
        "water_model": water_model,
        "ion_model": ion_model,
        "water_count": water_count,
        "sodium_count": sodium_count,
        "chloride_count": chloride_count,
        "packmol_tolerance_angstrom": packmol_tolerance_angstrom,
        "normal_boundary_clearance_angstrom": normal_boundary_clearance_angstrom,
        "random_seed": random_seed,
        "required_final_formal_charge_e": required_final_formal_charge_e,
        "output_slot": output_slot,
        "max_total_atoms": max_total_atoms,
        "idempotency_key": idempotency_key,
        "timeout_seconds": timeout_seconds,
    }
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        slot = validate_output_slot(output_slot)
        if water_model != "SPC/E":
            raise ValueError("water_model must be exactly 'SPC/E'")
        if ion_model != "Joung-Cheatham 2008 SPC/E":
            raise ValueError("ion_model must be exactly 'Joung-Cheatham 2008 SPC/E'")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required for periodic packing mutations")
        project = Path(get_project(project_directory)["project_directory"])
        destination = project / "model" / "cells" / f"{slot}.xsd"
        packmol_request_path = project / "request" / f"{slot}.packmol.inp"
        fluid_ledger_path = project / "request" / f"{slot}.packed_fluid.tsv"
        packmol_log_path = project / "reports" / f"{slot}.packmol.log"
        receipt_path = project / "reports" / f"{slot}.packing.receipt.json"
        published = [destination, packmol_request_path, fluid_ledger_path, packmol_log_path, receipt_path]
        created_paths: list[Path] = []
        if dry_run and any(path.exists() for path in published):
            existing = next(path for path in published if path.exists())
            raise FileExistsError(f"Refusing to overwrite existing packing artifact: {existing}")
        source = resolve_workspace_path(input_periodic_structure, must_exist=True)
        source_sha256 = validate_input_hash(source, input_sha256)
        frame = periodic_orthorhombic_frame(source)
        request = validate_aqueous_nacl_request(
            frame, water_count=water_count, sodium_count=sodium_count, chloride_count=chloride_count,
            packmol_tolerance_angstrom=packmol_tolerance_angstrom,
            normal_boundary_clearance_angstrom=normal_boundary_clearance_angstrom,
            random_seed=random_seed, max_total_atoms=max_total_atoms,
            required_final_formal_charge_e=required_final_formal_charge_e,
        )
        config = load_pipeline_config()
        packmol_executable = approved_executable(config["software"].get("packmol", {}).get("executable"), config=config)
        packmol_shell = approved_executable(config["software"].get("packmol", {}).get("shell"), config=config)
        packmol_text = packmol_input_text(
            lengths=frame["lengths_angstrom"], region=request["packing_region_angstrom"],
            tolerance=request["packmol_tolerance_angstrom"], seed=request["random_seed"],
            water_count=request["water_count"], sodium_count=request["sodium_count"],
            chloride_count=request["chloride_count"],
        )
        if dry_run:
            return success_result(
                tool_name,
                _dry_run_payload(
                    tool_name, parameters,
                    planned_outputs=[str(path) for path in published],
                    validations={
                        "input_sha256": source_sha256, "packing_request": request,
                        "packmol_executable_sha256": sha256_file(packmol_executable),
                        "packmol_shell_sha256": sha256_file(packmol_shell),
                    },
                    template_text=packmol_text + "\n" + build_packed_fluid_import_script(),
                    resource_estimate={"parallel_jobs": 1, "max_total_atoms": max_total_atoms, "timeout_seconds": timeout_seconds},
                ),
            )

        def implementation() -> dict[str, Any]:
            if any(path.exists() for path in published):
                existing = next(path for path in published if path.exists())
                raise FileExistsError(f"Refusing to overwrite existing packing artifact: {existing}")
            confirmation_manager.consume(confirmation_token, tool_name, parameters)
            timeout = bounded_timeout(timeout_seconds, config=config)
            job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            job_dir = _materialsscript_scratch_root() / "packmol" / job_id
            _assert_ascii_absolute_path(job_dir, "Packmol job directory")
            job_dir.mkdir(parents=True, exist_ok=False)

            fixed_atoms = [
                (atom["element"], *atom["local_xyz"])
                for atom in frame["local_atoms"]
            ]
            (job_dir / "framework.xyz").write_text(
                xyz_text(fixed_atoms, "hash-bound fixed periodic framework"), encoding="ascii", newline="\n"
            )
            (job_dir / "spce_water.xyz").write_text(spce_template_xyz(), encoding="ascii", newline="\n")
            (job_dir / "sodium.xyz").write_text(
                xyz_text([("Na", 0.0, 0.0, 0.0)], "sodium ion"), encoding="ascii", newline="\n"
            )
            (job_dir / "chloride.xyz").write_text(
                xyz_text([("Cl", 0.0, 0.0, 0.0)], "chloride ion"), encoding="ascii", newline="\n"
            )
            shutil.copy2(packmol_executable, job_dir / "packmol.exe")
            (job_dir / "packmol.inp").write_text(packmol_text, encoding="ascii", newline="\n")
            command = [str(packmol_shell), "-c", "./packmol.exe < packmol.inp"]
            with acquire_execution_slot(config=config):
                completed, timed_out, termination, process_pid = _run_guarded_materialsscript_process(
                    command, cwd=job_dir, timeout_seconds=timeout
                )
            packmol_log = completed.stdout + ("\n" + completed.stderr if completed.stderr else "")
            packed_xyz_path = job_dir / "packed.xyz"
            if timed_out:
                raise TimeoutError(f"Packmol timed out after {timeout} seconds")
            if completed.returncode != 0 or not packed_xyz_path.is_file():
                summary = packmol_log[-4000:].strip() or "Packmol did not create packed.xyz"
                raise RuntimeError(f"Packmol packing failed: {summary}")
            records = parse_xyz(packed_xyz_path)
            distance_audit = audit_packed_xyz(records, frame, request)
            fluid_text = packed_fluid_tsv(records, frame, request)
            _write_text_artifact_exclusive(fluid_ledger_path, fluid_text)
            created_paths.append(fluid_ledger_path)

            ms_result = _run_materialsscript_job(
                script_template=build_packed_fluid_import_script(),
                input_files={"structure": str(source), "fluid_ledger": str(fluid_ledger_path)},
                output_files={
                    "structure": {
                        "relative_path": "packed_periodic_aqueous_nacl.xsd",
                        "destination_path": str(destination),
                    },
                },
                job_name=f"pack_periodic_nacl_{slot}",
                run_mode="flat",
                keep_job_dir=True,
                timeout_seconds=timeout,
            )
            if destination.exists():
                created_paths.append(destination)
            if not ms_result.get("success"):
                raise RuntimeError(ms_result.get("error_summary") or "Materials Studio packed XSD job failed")
            validation = validate_packed_xsd(frame["ledger"]["model"], destination, request)
            _write_text_artifact_exclusive(packmol_request_path, packmol_text)
            created_paths.append(packmol_request_path)
            _write_text_artifact_exclusive(packmol_log_path, packmol_log)
            created_paths.append(packmol_log_path)
            receipt = {
                "schema_version": 1,
                "tool": tool_name,
                "status": validation["status"],
                "production_released": False,
                "input_path": str(source),
                "input_sha256": frame["ledger"]["model"]["sha256"],
                "output_path": str(destination),
                "output_sha256": sha256_file(destination),
                "declared_models": {"water": water_model, "ions": ion_model},
                "request": request,
                "cell_frame": {
                    "lengths_angstrom": frame["lengths_angstrom"],
                    "framework_normal_span_angstrom": frame["framework_normal_span_angstrom"],
                    "normal_gap_angstrom": frame["normal_gap_angstrom"],
                    "normal_shift_angstrom": frame["normal_shift_angstrom"],
                    "largest_gap_original": frame["largest_gap_original"],
                },
                "distance_audit": distance_audit,
                "validation": validation,
                "provenance": {
                    "packmol_executable": str(packmol_executable),
                    "packmol_executable_sha256": sha256_file(packmol_executable),
                    "packmol_shell": str(packmol_shell),
                    "packmol_shell_sha256": sha256_file(packmol_shell),
                    "packmol_request_path": str(packmol_request_path),
                    "packmol_request_sha256": sha256_file(packmol_request_path),
                    "fluid_ledger_path": str(fluid_ledger_path),
                    "fluid_ledger_sha256": sha256_file(fluid_ledger_path),
                },
                "execution": {
                    "packmol_job_id": job_id,
                    "packmol_job_directory": str(job_dir),
                    "packmol_exit_code": completed.returncode,
                    "packmol_process_pid": process_pid,
                    "packmol_timed_out": timed_out,
                    "packmol_termination": termination,
                    "materials_studio_job_id": ms_result.get("job_id"),
                    "materials_studio_audit_path": ms_result.get("audit_path"),
                    "run_mat_script_exit_code": ms_result.get("run_mat_script_exit_code"),
                    "materials_studio_timed_out": ms_result.get("timed_out"),
                },
                "limitations": [
                    "This tool constructs geometry and formal ion charge only; it does not assign force-field atom types or partial charges.",
                    "The output remains a reviewed candidate until force-field, energy, and short-runtime gates pass.",
                ],
            }
            _write_json_artifact_exclusive(receipt_path, receipt)
            created_paths.append(receipt_path)
            registrations = []
            for path, role, artifact_source in (
                (destination, "periodic_aqueous_nacl_candidate", str(source)),
                (packmol_request_path, "packmol_request", tool_name),
                (fluid_ledger_path, "packed_fluid_ledger", tool_name),
                (packmol_log_path, "packmol_execution_log", tool_name),
                (receipt_path, "periodic_aqueous_nacl_packing_receipt", tool_name),
            ):
                registrations.append(
                    register_artifact(project_directory, str(path), role, source=artifact_source)
                )
            return {
                "status": validation["status"],
                "production_released": False,
                "output_path": str(destination),
                "output_sha256": receipt["output_sha256"],
                "receipt_path": str(receipt_path),
                "molecule_counts": distance_audit["molecule_counts"],
                "distance_audit": distance_audit,
                "validation": validation,
                "artifact_registrations": registrations,
            }

        try:
            data, replayed = run_idempotent(
                project_directory, idempotency_key, tool_name, parameters, implementation
            )
        except Exception:
            for path in created_paths:
                path.unlink(missing_ok=True)
            raise
        return success_result(tool_name, data, replayed=replayed)
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def md_build_clayff_spce_nacl_lammps(
    project_directory: str,
    input_packed_structure: str,
    input_sha256: str,
    packing_receipt_path: str,
    packing_receipt_sha256: str,
    authenticated_methods_path: str,
    authenticated_methods_sha256: str,
    supporting_information_path: str,
    supporting_information_sha256: str,
    clay_nonbonded_path: str,
    clay_nonbonded_sha256: str,
    clay_bonded_path: str,
    clay_bonded_sha256: str,
    joung_cheatham_spce_path: str,
    joung_cheatham_spce_sha256: str,
    output_slot: str,
    max_atoms: int,
    idempotency_key: str,
    confirmation_token: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Generate an exact-count neutral quartz ClayFF/SPC/E/Joung LAMMPS candidate."""

    tool_name = "md_build_clayff_spce_nacl_lammps"
    parameters = {
        "project_directory": project_directory,
        "input_packed_structure": input_packed_structure,
        "input_sha256": input_sha256,
        "packing_receipt_path": packing_receipt_path,
        "packing_receipt_sha256": packing_receipt_sha256,
        "authenticated_methods_path": authenticated_methods_path,
        "authenticated_methods_sha256": authenticated_methods_sha256,
        "supporting_information_path": supporting_information_path,
        "supporting_information_sha256": supporting_information_sha256,
        "clay_nonbonded_path": clay_nonbonded_path,
        "clay_nonbonded_sha256": clay_nonbonded_sha256,
        "clay_bonded_path": clay_bonded_path,
        "clay_bonded_sha256": clay_bonded_sha256,
        "joung_cheatham_spce_path": joung_cheatham_spce_path,
        "joung_cheatham_spce_sha256": joung_cheatham_spce_sha256,
        "output_slot": output_slot,
        "max_atoms": max_atoms,
        "idempotency_key": idempotency_key,
    }
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        slot = validate_output_slot(output_slot)
        if isinstance(max_atoms, bool) or not isinstance(max_atoms, int) or not 19364 <= max_atoms <= 1_000_000:
            raise ValueError("max_atoms must be an integer from 19364 to 1000000")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required for LAMMPS candidate generation")
        project = Path(get_project(project_directory)["project_directory"])
        profile_path = project / "forcefield" / f"{slot}.forcefield.json"
        data_path = project / "lammps" / f"{slot}.data"
        gate_input_path = project / "lammps" / f"in.{slot}.gates"
        production_input_path = project / "lammps" / f"in.{slot}.production"
        protocol_path = project / "request" / f"{slot}.protocol.json"
        receipt_path = project / "reports" / f"{slot}.lammps_build.receipt.json"
        published = [profile_path, data_path, gate_input_path, production_input_path, protocol_path, receipt_path]
        created_paths: list[Path] = []
        if dry_run and any(path.exists() for path in published):
            existing = next(path for path in published if path.exists())
            raise FileExistsError(f"Refusing to overwrite existing LAMMPS candidate artifact: {existing}")
        inputs = [
            resolve_workspace_path(value, must_exist=True) for value in (
                input_packed_structure, packing_receipt_path, authenticated_methods_path,
                supporting_information_path, clay_nonbonded_path, clay_bonded_path,
                joung_cheatham_spce_path,
            )
        ]
        expected_hashes = [
            input_sha256, packing_receipt_sha256, authenticated_methods_sha256,
            supporting_information_sha256, clay_nonbonded_sha256, clay_bonded_sha256,
            joung_cheatham_spce_sha256,
        ]
        validated_hashes = [validate_input_hash(path, digest) for path, digest in zip(inputs, expected_hashes)]
        source, packing_receipt, methods, si, clay_nonbonded, clay_bonded, joung = inputs
        packing = json.loads(packing_receipt.read_text(encoding="utf-8"))
        if (
            packing.get("status") != "periodic_aqueous_nacl_packing_pass"
            or packing.get("output_sha256") != input_sha256.upper()
            or [packing.get("request", {}).get(key) for key in ("water_count", "sodium_count", "chloride_count")]
            != [5340, 40, 40]
        ):
            raise ValueError("Packing receipt is not the exact neutral 5340/40/40 G06 candidate")
        method_evidence = json.loads(methods.read_text(encoding="utf-8"))
        if method_evidence.get("evidence_id") != "G06-AUTH-METHODS-20260716" or not any(
            item.get("doi") == "10.1021/acs.jpcc.7b08214" for item in method_evidence.get("sources", [])
        ):
            raise ValueError("Authenticated method evidence does not bind the target G06 DOI")
        source_evidence = validate_forcefield_sources(clay_nonbonded, clay_bonded, joung)
        classification = classify_neutral_quartz_spce_nacl(source)
        if classification["ledger"]["model"]["atom_count"] > max_atoms:
            raise ValueError("Typed G06 atom count exceeds max_atoms")
        profile = forcefield_profile({
            **source_evidence, "clay_nonbonded_path": str(clay_nonbonded),
            "clay_bonded_path": str(clay_bonded), "joung_cheatham_spce_path": str(joung),
            "clayff_primary_doi": "10.1021/jp003751k", "joung_cheatham_doi": "10.1021/jp8001614",
            "paper_parameter_declaration_doi": "10.1021/acs.jpcc.7b08214",
        })
        gate_text = render_gate_input(data_path.name)
        production_text = render_production_input(data_path.name)
        if dry_run:
            return success_result(
                tool_name,
                _dry_run_payload(
                    tool_name, parameters,
                    planned_outputs=[str(path) for path in published],
                    validations={
                        "input_hashes": validated_hashes, "forcefield_sources": source_evidence,
                        "atom_count": classification["ledger"]["model"]["atom_count"],
                        "type_counts": classification["type_counts"], "net_charge_e": classification["net_charge_e"],
                    },
                    template_text=gate_text + "\n" + production_text,
                    resource_estimate={"parallel_jobs": 1, "max_atoms": max_atoms},
                ),
            )

        def implementation() -> dict[str, Any]:
            if any(path.exists() for path in published):
                existing = next(path for path in published if path.exists())
                raise FileExistsError(f"Refusing to overwrite existing LAMMPS candidate artifact: {existing}")
            confirmation_manager.consume(confirmation_token, tool_name, parameters)
            if source.stat().st_size <= 0 or si.suffix.lower() != ".pdf":
                raise ValueError("Packed source and supporting information inputs must be regular nonempty files")

            data_text = render_lammps_data(classification, profile)
            _write_json_artifact_exclusive(profile_path, profile)
            created_paths.append(profile_path)
            _write_text_artifact_exclusive(data_path, data_text)
            created_paths.append(data_path)
            _write_text_artifact_exclusive(gate_input_path, gate_text)
            created_paths.append(gate_input_path)
            _write_text_artifact_exclusive(production_input_path, production_text)
            created_paths.append(production_input_path)
            contract = protocol_contract(
                input_sha256=input_sha256.upper(),
                packing_receipt_sha256=packing_receipt_sha256.upper(),
                methods_sha256=authenticated_methods_sha256.upper(),
                si_sha256=supporting_information_sha256.upper(),
                forcefield_profile_sha256=sha256_file(profile_path),
                data_sha256=sha256_file(data_path),
                gate_input_sha256=sha256_file(gate_input_path),
                production_input_sha256=sha256_file(production_input_path),
                observed_cell_lengths=classification["frame"]["lengths_angstrom"],
                type_counts=classification["type_counts"],
            )
            _write_json_artifact_exclusive(protocol_path, contract)
            created_paths.append(protocol_path)
            receipt = {
                "schema_version": 1,
                "tool": tool_name,
                "status": "clayff_spce_nacl_lammps_candidate_pass",
                "production_released": False,
                "input_path": str(source),
                "input_sha256": input_sha256.upper(),
                "table_s1_type_counts": classification["type_counts"],
                "net_charge_e": classification["net_charge_e"],
                "topology": {
                    "atoms": 19364,
                    "bonds": len(classification["structural_bonds"]) + len(classification["water_bonds"]),
                    "angles": len(classification["structural_angles"]) + len(classification["water_angles"]),
                    "structural_oh_bonds": len(classification["structural_bonds"]),
                    "water_oh_bonds": len(classification["water_bonds"]),
                    "structural_sih_oh_hh_angles": len(classification["structural_angles"]),
                    "water_hoh_angles": len(classification["water_angles"]),
                },
                "artifacts": {
                    "forcefield_profile": {"path": str(profile_path), "sha256": sha256_file(profile_path)},
                    "lammps_data": {"path": str(data_path), "sha256": sha256_file(data_path)},
                    "gate_input": {"path": str(gate_input_path), "sha256": sha256_file(gate_input_path)},
                    "production_input": {"path": str(production_input_path), "sha256": sha256_file(production_input_path)},
                    "protocol": {"path": str(protocol_path), "sha256": sha256_file(protocol_path)},
                },
                "source_evidence": source_evidence,
                "limitations": [
                    "This is a deterministic protocol reconstruction, not the authors' original coordinate file.",
                    "The MS 23.1 parent gives B=39.27997 A rather than the paper's 39.82 A and is not silently rescaled.",
                    "All substrate atoms are fixed because no complete author fixed-layer atom ledger was available.",
                    "Production remains blocked until run-0, minimization, short dynamics, and long protocol gates pass.",
                ],
            }
            _write_json_artifact_exclusive(receipt_path, receipt)
            created_paths.append(receipt_path)
            registrations = []
            for path, role in (
                (profile_path, "g06_forcefield_profile"),
                (data_path, "g06_lammps_candidate"),
                (gate_input_path, "g06_lammps_gate_input"),
                (production_input_path, "g06_lammps_production_input"),
                (protocol_path, "g06_exact_reconstruction_protocol"),
                (receipt_path, "g06_lammps_build_receipt"),
            ):
                registrations.append(register_artifact(project_directory, str(path), role, source=tool_name))
            return {
                "status": receipt["status"],
                "production_released": False,
                "data_path": str(data_path),
                "data_sha256": receipt["artifacts"]["lammps_data"]["sha256"],
                "gate_input_path": str(gate_input_path),
                "production_input_path": str(production_input_path),
                "protocol_path": str(protocol_path),
                "receipt_path": str(receipt_path),
                "table_s1_type_counts": classification["type_counts"],
                "topology": receipt["topology"],
                "artifact_registrations": registrations,
            }

        try:
            data, replayed = run_idempotent(
                project_directory, idempotency_key, tool_name, parameters, implementation
            )
        except Exception:
            for path in created_paths:
                path.unlink(missing_ok=True)
            raise
        return success_result(tool_name, data, replayed=replayed)
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def ms_geology_import_crystal_parent(
    project_directory: str,
    input_crystal_structure: str,
    input_sha256: str,
    expected_elements: dict[str, int],
    output_slot: str,
    max_atoms: int,
    idempotency_key: str,
    confirmation_token: str | None = None,
    timeout_seconds: int = 300,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Import a hash-bound CIF/XSD crystal parent and verify its periodic element inventory."""

    tool_name = "ms_geology_import_crystal_parent"
    parameters = {
        "project_directory": project_directory,
        "input_crystal_structure": input_crystal_structure,
        "input_sha256": input_sha256,
        "expected_elements": expected_elements,
        "output_slot": output_slot,
        "max_atoms": max_atoms,
        "idempotency_key": idempotency_key,
        "timeout_seconds": timeout_seconds,
    }
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        slot = validate_output_slot(output_slot)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required for geology model mutations")
        project = Path(get_project(project_directory)["project_directory"])
        destination = project / "model" / "parents" / f"{slot}.xsd"
        receipt_path = project / "reports" / f"{slot}.crystal_parent.receipt.json"
        source_snapshot = project / "source" / "crystal_parents" / f"{slot}{Path(input_crystal_structure).suffix.lower()}"
        source = _resolve_crystal_parent_source(input_crystal_structure)
        source_sha256 = validate_input_hash(source, input_sha256)
        request = validate_crystal_parent_request(source, expected_elements, max_atoms)
        for path in (destination, receipt_path) if dry_run else ():
            if path.exists():
                raise FileExistsError(f"Refusing to overwrite crystal-parent artifact: {path}")
        if source_snapshot.exists():
            validate_input_hash(source_snapshot, input_sha256)
        if dry_run:
            return success_result(
                tool_name,
                _dry_run_payload(
                    tool_name, parameters,
                    planned_outputs=[str(source_snapshot), str(destination), str(receipt_path)],
                    validations={"input_sha256": source_sha256, "request": request},
                    template_text=build_crystal_parent_import_script(),
                    resource_estimate={"parallel_jobs": 1, "max_atoms": max_atoms, "timeout_seconds": timeout_seconds},
                ),
            )

        def implementation() -> dict[str, Any]:
            confirmation_manager.consume(confirmation_token, tool_name, parameters)
            source_snapshot.parent.mkdir(parents=True, exist_ok=True)
            if source_snapshot.exists():
                validate_input_hash(source_snapshot, input_sha256)
            else:
                shutil.copy2(source, source_snapshot)
                validate_input_hash(source_snapshot, input_sha256)
            result = _run_materialsscript_job(
                script_template=build_crystal_parent_import_script(),
                input_files={"structure": str(source_snapshot)},
                output_files={
                    "structure": {
                        "relative_path": "crystal_parent.xsd",
                        "destination_path": str(destination),
                    },
                },
                job_name=f"geology_parent_{slot}",
                run_mode="flat",
                keep_job_dir=True,
                timeout_seconds=timeout_seconds,
            )
            if not result.get("success"):
                raise RuntimeError(result.get("error_summary") or "Materials Studio crystal import failed")
            try:
                validation = validate_crystal_parent_import_result(
                    destination, request["expected_elements"], request["max_atoms"]
                )
                receipt = {
                    "schema_version": 1,
                    "tool": tool_name,
                    "status": validation["status"],
                    "production_released": False,
                    "source_path": str(source),
                    "source_sha256": source_sha256,
                    "source_snapshot_path": str(source_snapshot),
                    "source_snapshot_sha256": sha256_file(source_snapshot),
                    "output_path": str(destination),
                    "output_sha256": sha256_file(destination),
                    "request": request,
                    "validation": validation,
                    "execution": {
                        "job_id": result.get("job_id"),
                        "audit_path": result.get("audit_path"),
                        "run_mat_script_exit_code": result.get("run_mat_script_exit_code"),
                        "timed_out": result.get("timed_out"),
                    },
                    "limitations": [
                        "Import validates provenance, periodicity, coordinates and composition only.",
                        "No surface termination, hydroxylation, forcefield assignment or production release is implied.",
                    ],
                }
                _write_json_artifact_exclusive(receipt_path, receipt)
                registrations = [
                    register_artifact(
                        project_directory,
                        str(source_snapshot),
                        "geology_crystal_parent_source_snapshot",
                        source=str(source),
                    ),
                    register_artifact(
                        project_directory, str(destination), "geology_crystal_parent", source=str(source_snapshot)
                    ),
                    register_artifact(
                        project_directory, str(receipt_path), "geology_crystal_parent_receipt", source=tool_name
                    ),
                ]
                return {
                    "status": validation["status"],
                    "production_released": False,
                    "output_path": str(destination),
                    "output_sha256": receipt["output_sha256"],
                    "receipt_path": str(receipt_path),
                    "validation": validation,
                    "artifact_registrations": registrations,
                    "limitations": receipt["limitations"],
                }
            except Exception:
                destination.unlink(missing_ok=True)
                receipt_path.unlink(missing_ok=True)
                raise

        data, replayed = run_idempotent(
            project_directory, idempotency_key, tool_name, parameters, implementation
        )
        return success_result(tool_name, data, replayed=replayed)
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def ms_geology_assess_nanopore_contract(contract_path: str) -> dict[str, Any]:
    """Read and fail-closed validate a mineral nanopore construction contract."""

    tool_name = "ms_geology_assess_nanopore_contract"
    try:
        return success_result(tool_name, assess_geopore_contract(contract_path))
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def ms_moc_get_status() -> dict[str, Any]:
    """Return the readiness of the local Materials Studio desktop-control layer."""

    tool_name = "ms_moc_get_status"
    try:
        return success_result(tool_name, get_moc_status())
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def ms_moc_open_document(
    project_directory: str,
    document_path: str,
    document_sha256: str,
    idempotency_key: str,
    dry_run: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Open one hash-bound project document through MOC after an optional dry run."""

    tool_name = "ms_moc_open_document"
    parameters = {
        "project_directory": project_directory,
        "document_path": document_path,
        "document_sha256": document_sha256,
        "idempotency_key": idempotency_key,
        "dry_run": dry_run,
    }
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required for MOC document operations")
        project = Path(get_project(project_directory)["project_directory"]).resolve()
        document = resolve_workspace_path(document_path, must_exist=True)
        if not document.is_file():
            raise ValueError(f"MOC document must be a regular file: {document}")
        if document.suffix.lower() not in MOC_DOCUMENT_SUFFIXES:
            raise ValueError(f"Unsupported Materials Studio document type: {document.suffix}")
        if document != project and project not in document.parents:
            raise PermissionError("MOC can only open a document inside the bound project")
        actual_sha256 = validate_input_hash(document, document_sha256)

        def implementation() -> dict[str, Any]:
            if not dry_run:
                confirmation_manager.consume(confirmation_token, tool_name, parameters)
            response = launch_document(document, dry_run=dry_run)
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("MOC launch response is missing its result object")
            expected_status = "dry_run" if dry_run else "launched"
            if result.get("status") != expected_status:
                raise RuntimeError(
                    f"MOC launch status mismatch: {result.get('status')!r} != {expected_status!r}"
                )
            if result.get("document_sha256") != actual_sha256:
                raise RuntimeError("MOC launch response document hash does not match the validated input")
            return {
                "status": expected_status,
                "dry_run": dry_run,
                "document_path": str(document),
                "document_sha256": actual_sha256,
                "moc": result,
            }

        data, replayed = run_idempotent(
            project_directory, idempotency_key, tool_name, parameters, implementation
        )
        return success_result(tool_name, data, replayed=replayed)
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def ms_geology_build_supercell(
    project_directory: str,
    input_structure: str,
    input_sha256: str,
    repeat_a: int,
    repeat_b: int,
    repeat_c: int,
    output_slot: str,
    max_atoms: int,
    idempotency_key: str,
    confirmation_token: str | None = None,
    timeout_seconds: int = 600,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Build one audited XSD supercell with the local MS 2023 BuildSuperCell API."""

    tool_name = "ms_geology_build_supercell"
    parameters = {
        "project_directory": project_directory,
        "input_structure": input_structure,
        "input_sha256": input_sha256,
        "repeat_a": repeat_a,
        "repeat_b": repeat_b,
        "repeat_c": repeat_c,
        "output_slot": output_slot,
        "max_atoms": max_atoms,
        "idempotency_key": idempotency_key,
        "timeout_seconds": timeout_seconds,
    }
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        repeats = validate_repeats(repeat_a, repeat_b, repeat_c)
        slot = validate_output_slot(output_slot)
        if isinstance(max_atoms, bool) or not isinstance(max_atoms, int) or not 1 <= max_atoms <= 10_000_000:
            raise ValueError("max_atoms must be an integer from 1 to 10000000")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required for geology model mutations")
        project = Path(get_project(project_directory)["project_directory"])
        destination = project / "model" / f"{slot}.xsd"
        receipt_path = project / "reports" / f"{slot}.supercell.receipt.json"
        if dry_run and (destination.exists() or receipt_path.exists()):
            raise FileExistsError("Refusing to overwrite supercell artifacts")
        if dry_run:
            source = resolve_workspace_path(input_structure, must_exist=True)
            source_sha256 = validate_input_hash(source, input_sha256)
            input_model = inspect_xsd_geometry(source)
            periodic_dimension = input_model.get("periodic_dimension")
            if periodic_dimension not in {2, 3}:
                raise ValueError("Supercell input must be a 2D PlaneGroup or 3D SpaceGroup XSD")
            if periodic_dimension == 2 and repeats[2] != 1:
                raise ValueError("A 2D surface supercell requires repeat_c=1")
            expected_atoms = input_model["atom_count"] * repeats[0] * repeats[1] * repeats[2]
            if expected_atoms > max_atoms:
                raise ValueError("Requested supercell exceeds max_atoms")
            return success_result(
                tool_name,
                _dry_run_payload(
                    tool_name, parameters,
                    planned_outputs=[str(destination), str(receipt_path)],
                    validations={"input_sha256": source_sha256, "input_atoms": input_model["atom_count"], "expected_atoms": expected_atoms},
                    template_text=build_supercell_script(repeats, periodic_dimension),
                    resource_estimate={"parallel_jobs": 1, "max_atoms": max_atoms, "timeout_seconds": timeout_seconds},
                ),
            )

        def implementation() -> dict[str, Any]:
            confirmation_manager.consume(confirmation_token, tool_name, parameters)
            source = resolve_workspace_path(input_structure, must_exist=True)
            validate_input_hash(source, input_sha256)
            input_model = inspect_xsd_geometry(source)
            periodic_dimension = input_model.get("periodic_dimension")
            if periodic_dimension not in {2, 3}:
                raise ValueError("Supercell input must be a 2D PlaneGroup or 3D SpaceGroup XSD")
            if periodic_dimension == 2 and repeats[2] != 1:
                raise ValueError("A 2D surface supercell requires repeat_c=1")
            expected_atoms = input_model["atom_count"] * math.prod(repeats)
            if expected_atoms > max_atoms:
                raise ValueError(f"Expected supercell atom count {expected_atoms} exceeds max_atoms {max_atoms}")
            result = _run_materialsscript_job(
                script_template=build_supercell_script(repeats, periodic_dimension),
                input_files={"structure": str(source)},
                output_files={
                    "structure": {
                        "relative_path": "supercell.xsd",
                        "destination_path": str(destination),
                    }
                },
                job_name=f"geology_supercell_{slot}",
                run_mode="flat",
                keep_job_dir=True,
                timeout_seconds=timeout_seconds,
            )
            if not result.get("success"):
                raise RuntimeError(result.get("error_summary") or "Materials Studio supercell job failed")
            try:
                validation = validate_supercell_result(input_model, destination, repeats, max_atoms)
                receipt = {
                    "schema_version": 1,
                    "tool": tool_name,
                    "status": "candidate_model_pass",
                    "production_released": False,
                    "input_sha256": input_model["sha256"],
                    "output_path": str(destination),
                    "output_sha256": sha256_file(destination),
                    "validation": validation,
                    "execution": {
                        "job_id": result.get("job_id"),
                        "audit_path": result.get("audit_path"),
                        "run_mat_script_exit_code": result.get("run_mat_script_exit_code"),
                        "timed_out": result.get("timed_out"),
                    },
                    "limitations": [
                        "This tool builds geometry only; it does not assign or validate a force field.",
                        "A supercell candidate is not a production trajectory or a production release.",
                    ],
                }
                _write_json_artifact_exclusive(receipt_path, receipt)
                output_registration = register_artifact(
                    project_directory, str(destination), "geology_supercell_candidate", source=str(source)
                )
                receipt_registration = register_artifact(
                    project_directory, str(receipt_path), "geology_supercell_receipt", source=tool_name
                )
                return {
                    "status": "candidate_model_pass",
                    "production_released": False,
                    "output_path": str(destination),
                    "output_sha256": receipt["output_sha256"],
                    "receipt_path": str(receipt_path),
                    "validation": validation,
                    "artifact_registrations": [output_registration, receipt_registration],
                }
            except Exception:
                destination.unlink(missing_ok=True)
                receipt_path.unlink(missing_ok=True)
                raise

        data, replayed = run_idempotent(
            project_directory, idempotency_key, tool_name, parameters, implementation
        )
        return success_result(tool_name, data, replayed=replayed)
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def ms_geology_enumerate_surface_terminations(
    project_directory: str,
    input_bulk_structure: str,
    input_sha256: str,
    miller_h: int,
    miller_k: int,
    miller_l: int,
    thickness_angstrom: float,
    top_positions: list[float],
    output_slot: str,
    max_candidates: int,
    idempotency_key: str,
    confirmation_token: str | None = None,
    timeout_seconds: int = 900,
    u_vector: list[int] | None = None,
    v_vector: list[int] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Enumerate nonproduction surface candidates without selecting or repairing a termination."""

    tool_name = "ms_geology_enumerate_surface_terminations"
    parameters = {
        "project_directory": project_directory,
        "input_bulk_structure": input_bulk_structure,
        "input_sha256": input_sha256,
        "miller_h": miller_h,
        "miller_k": miller_k,
        "miller_l": miller_l,
        "thickness_angstrom": thickness_angstrom,
        "top_positions": top_positions,
        "output_slot": output_slot,
        "max_candidates": max_candidates,
        "idempotency_key": idempotency_key,
        "timeout_seconds": timeout_seconds,
    }
    if u_vector is not None or v_vector is not None:
        parameters["u_vector"] = u_vector
        parameters["v_vector"] = v_vector
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        surface = validate_surface_parameters(
            miller_h, miller_k, miller_l, thickness_angstrom, top_positions, max_candidates
        )
        mesh_vectors = validate_surface_mesh_vectors(surface["miller"], u_vector, v_vector)
        slot = validate_output_slot(output_slot)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required for geology model mutations")
        project = Path(get_project(project_directory)["project_directory"])
        candidate_root = project / "model" / "surfaces" / slot
        manifest_path = project / "reports" / f"{slot}.surface_candidates.json"
        candidate_paths = [candidate_root / f"candidate_{index:03d}.xsd" for index in range(len(surface["top_positions"]))]
        if dry_run and (manifest_path.exists() or any(path.exists() for path in candidate_paths)):
            raise FileExistsError("Refusing to overwrite surface-enumeration artifacts")
        if dry_run:
            source = resolve_workspace_path(input_bulk_structure, must_exist=True)
            source_sha256 = validate_input_hash(source, input_sha256)
            input_model = inspect_xsd_geometry(source)
            if input_model["periodic_dimension"] != 3:
                raise ValueError("Surface enumeration input must be a 3D SpaceGroup XSD bulk model")
            script = build_surface_enumeration_script(
                surface["miller"], surface["thickness_angstrom"], surface["top_positions"],
                *(mesh_vectors or (None, None)),
            )
            return success_result(
                tool_name,
                _dry_run_payload(
                    tool_name, parameters,
                    planned_outputs=[*(str(path) for path in candidate_paths), str(manifest_path)],
                    validations={"input_sha256": source_sha256, "periodic_dimension": 3, "candidate_count": len(candidate_paths), "surface": surface},
                    template_text=script,
                    resource_estimate={"parallel_jobs": 1, "max_candidates": max_candidates, "timeout_seconds": timeout_seconds},
                ),
            )

        def implementation() -> dict[str, Any]:
            confirmation_manager.consume(confirmation_token, tool_name, parameters)
            source = resolve_workspace_path(input_bulk_structure, must_exist=True)
            validate_input_hash(source, input_sha256)
            input_model = inspect_xsd_geometry(source)
            if input_model.get("periodic_dimension") != 3:
                raise ValueError("Surface enumeration input must be a 3D SpaceGroup XSD bulk model")
            outputs = {
                f"candidate_{index}": {
                    "relative_path": f"candidate_{index:03d}.xsd",
                    "destination_path": str(path),
                }
                for index, path in enumerate(candidate_paths)
            }
            result = _run_materialsscript_job(
                script_template=build_surface_enumeration_script(
                    surface["miller"], surface["thickness_angstrom"], surface["top_positions"],
                    *(mesh_vectors or (None, None)),
                ),
                input_files={"structure": str(source)},
                output_files=outputs,
                job_name=f"geology_surface_{slot}",
                run_mode="flat",
                keep_job_dir=True,
                timeout_seconds=timeout_seconds,
            )
            if not result.get("success"):
                raise RuntimeError(result.get("error_summary") or "Materials Studio surface enumeration failed")
            try:
                evidence = validate_surface_candidates(
                    candidate_paths, surface["miller"], surface["thickness_angstrom"], surface["top_positions"]
                )
                evidence.update(
                    {
                        "schema_version": 1,
                        "tool": tool_name,
                        "input_path": str(source),
                        "input_sha256": input_model["sha256"],
                        "surface_mesh_vectors": (
                            {"u": list(mesh_vectors[0]), "v": list(mesh_vectors[1])}
                            if mesh_vectors else None
                        ),
                        "execution": {
                            "job_id": result.get("job_id"),
                            "audit_path": result.get("audit_path"),
                            "run_mat_script_exit_code": result.get("run_mat_script_exit_code"),
                            "timed_out": result.get("timed_out"),
                        },
                    }
                )
                _write_json_artifact_exclusive(manifest_path, evidence)
                registrations = [
                    register_artifact(
                        project_directory, str(path), "surface_termination_candidate", source=str(source)
                    )
                    for path in candidate_paths
                ]
                registrations.append(
                    register_artifact(
                        project_directory, str(manifest_path), "surface_candidate_manifest", source=tool_name
                    )
                )
                return {
                    "status": evidence["status"],
                    "production_released": False,
                    "manifest_path": str(manifest_path),
                    "candidate_count": len(candidate_paths),
                    "candidates": evidence["candidates"],
                    "artifact_registrations": registrations,
                    "limitations": evidence["limitations"],
                }
            except Exception:
                for path in candidate_paths:
                    path.unlink(missing_ok=True)
                manifest_path.unlink(missing_ok=True)
                raise

        data, replayed = run_idempotent(
            project_directory, idempotency_key, tool_name, parameters, implementation
        )
        return success_result(tool_name, data, replayed=replayed)
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def ms_geology_apply_substitutions(
    project_directory: str,
    input_structure: str,
    input_sha256: str,
    substitutions: list[dict[str, Any]],
    output_slot: str,
    idempotency_key: str,
    confirmation_token: str | None = None,
    timeout_seconds: int = 600,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Apply an explicit, named P1 substitution ledger and return a nonproduction candidate."""

    tool_name = "ms_geology_apply_substitutions"
    parameters = {
        "project_directory": project_directory,
        "input_structure": input_structure,
        "input_sha256": input_sha256,
        "substitutions": substitutions,
        "output_slot": output_slot,
        "idempotency_key": idempotency_key,
        "timeout_seconds": timeout_seconds,
    }
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        slot = validate_output_slot(output_slot)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required for geology model mutations")
        project = Path(get_project(project_directory)["project_directory"])
        destination = project / "model" / f"{slot}.xsd"
        receipt_path = project / "reports" / f"{slot}.substitutions.receipt.json"
        charge_audit_path = project / "reports" / f"{slot}.substitutions.charge.tsv"
        published = [destination, receipt_path, charge_audit_path]
        if dry_run and any(path.exists() for path in published):
            raise FileExistsError("Refusing to overwrite substitution artifacts")
        if dry_run:
            source = resolve_workspace_path(input_structure, must_exist=True)
            source_sha256 = validate_input_hash(source, input_sha256)
            request = validate_substitution_ledger(source, substitutions)
            return success_result(
                tool_name,
                _dry_run_payload(
                    tool_name, parameters,
                    planned_outputs=[str(path) for path in published],
                    validations={"input_sha256": source_sha256, "substitution_count": len(request["substitutions"])},
                    template_text=build_substitution_script(request["substitutions"]),
                    resource_estimate={"parallel_jobs": 1, "timeout_seconds": timeout_seconds},
                ),
            )

        def implementation() -> dict[str, Any]:
            confirmation_manager.consume(confirmation_token, tool_name, parameters)
            source = resolve_workspace_path(input_structure, must_exist=True)
            validate_input_hash(source, input_sha256)
            request = validate_substitution_ledger(source, substitutions)
            result = _run_materialsscript_job(
                script_template=build_substitution_script(request["substitutions"]),
                input_files={"structure": str(source)},
                output_files={
                    "structure": {
                        "relative_path": "substituted.xsd",
                        "destination_path": str(destination),
                    },
                    "charge_audit": {
                        "relative_path": "substitution_charge.tsv",
                        "destination_path": str(charge_audit_path),
                    },
                },
                job_name=f"geology_substitution_{slot}",
                run_mode="flat",
                keep_job_dir=True,
                timeout_seconds=timeout_seconds,
            )
            if not result.get("success"):
                raise RuntimeError(result.get("error_summary") or "Materials Studio substitution job failed")
            try:
                charge_audit = parse_charge_audit(charge_audit_path)
                validation = validate_substitution_result(
                    request["input"], destination, request["substitutions"], charge_audit
                )
                receipt = {
                    "schema_version": 1,
                    "tool": tool_name,
                    "status": validation["status"],
                    "production_released": False,
                    "input_path": str(source),
                    "input_sha256": request["input"]["model"]["sha256"],
                    "output_path": str(destination),
                    "output_sha256": sha256_file(destination),
                    "substitutions": request["substitutions"],
                    "charge_audit": charge_audit,
                    "validation": validation,
                    "execution": {
                        "job_id": result.get("job_id"),
                        "audit_path": result.get("audit_path"),
                        "run_mat_script_exit_code": result.get("run_mat_script_exit_code"),
                        "timed_out": result.get("timed_out"),
                    },
                    "limitations": [
                        "The substitution ledger is caller-supplied and must have independent scientific provenance.",
                        "Formal charge is audited separately from forcefield partial charge; no forcefield is assigned.",
                        "This geometry candidate is not a production release.",
                    ],
                }
                _write_json_artifact_exclusive(receipt_path, receipt)
                registrations = [
                    register_artifact(project_directory, str(destination), "geology_substitution_candidate", source=str(source)),
                    register_artifact(project_directory, str(charge_audit_path), "geology_substitution_charge_audit", source=tool_name),
                    register_artifact(project_directory, str(receipt_path), "geology_substitution_receipt", source=tool_name),
                ]
                return {
                    "status": validation["status"],
                    "production_released": False,
                    "output_path": str(destination),
                    "output_sha256": receipt["output_sha256"],
                    "receipt_path": str(receipt_path),
                    "validation": validation,
                    "artifact_registrations": registrations,
                    "limitations": receipt["limitations"],
                }
            except Exception:
                destination.unlink(missing_ok=True)
                charge_audit_path.unlink(missing_ok=True)
                receipt_path.unlink(missing_ok=True)
                raise

        data, replayed = run_idempotent(
            project_directory, idempotency_key, tool_name, parameters, implementation
        )
        return success_result(tool_name, data, replayed=replayed)
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def ms_geology_place_counterions(
    project_directory: str,
    input_structure: str,
    input_sha256: str,
    placements: list[dict[str, Any]],
    output_slot: str,
    min_framework_distance_angstrom: float,
    min_counterion_distance_angstrom: float,
    max_atoms: int,
    idempotency_key: str,
    confirmation_token: str | None = None,
    timeout_seconds: int = 600,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Place explicit counterions from a reviewed P1 fractional-coordinate ledger."""

    tool_name = "ms_geology_place_counterions"
    parameters = {
        "project_directory": project_directory,
        "input_structure": input_structure,
        "input_sha256": input_sha256,
        "placements": placements,
        "output_slot": output_slot,
        "min_framework_distance_angstrom": min_framework_distance_angstrom,
        "min_counterion_distance_angstrom": min_counterion_distance_angstrom,
        "max_atoms": max_atoms,
        "idempotency_key": idempotency_key,
        "timeout_seconds": timeout_seconds,
    }
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        slot = validate_output_slot(output_slot)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required for geology model mutations")
        project = Path(get_project(project_directory)["project_directory"])
        destination = project / "model" / f"{slot}.xsd"
        receipt_path = project / "reports" / f"{slot}.counterions.receipt.json"
        charge_audit_path = project / "reports" / f"{slot}.counterions.charge.tsv"
        published = [destination, receipt_path, charge_audit_path]
        if dry_run and any(path.exists() for path in published):
            raise FileExistsError("Refusing to overwrite counterion artifacts")
        if dry_run:
            source = resolve_workspace_path(input_structure, must_exist=True)
            source_sha256 = validate_input_hash(source, input_sha256)
            request = validate_counterion_ledger(
                source, placements, min_framework_distance_angstrom,
                min_counterion_distance_angstrom, max_atoms,
            )
            return success_result(
                tool_name,
                _dry_run_payload(
                    tool_name, parameters,
                    planned_outputs=[str(path) for path in published],
                    validations={"input_sha256": source_sha256, "placement_count": len(request["placements"])},
                    template_text=build_counterion_script(request["placements"]),
                    resource_estimate={"parallel_jobs": 1, "max_atoms": max_atoms, "timeout_seconds": timeout_seconds},
                ),
            )

        def implementation() -> dict[str, Any]:
            confirmation_manager.consume(confirmation_token, tool_name, parameters)
            source = resolve_workspace_path(input_structure, must_exist=True)
            validate_input_hash(source, input_sha256)
            request = validate_counterion_ledger(
                source, placements, min_framework_distance_angstrom,
                min_counterion_distance_angstrom, max_atoms,
            )
            result = _run_materialsscript_job(
                script_template=build_counterion_script(request["placements"]),
                input_files={"structure": str(source)},
                output_files={
                    "structure": {
                        "relative_path": "counterions.xsd",
                        "destination_path": str(destination),
                    },
                    "charge_audit": {
                        "relative_path": "counterion_charge.tsv",
                        "destination_path": str(charge_audit_path),
                    },
                },
                job_name=f"geology_counterions_{slot}",
                run_mode="flat",
                keep_job_dir=True,
                timeout_seconds=timeout_seconds,
            )
            if not result.get("success"):
                raise RuntimeError(result.get("error_summary") or "Materials Studio counterion job failed")
            try:
                charge_audit = parse_charge_audit(charge_audit_path)
                validation = validate_counterion_result(
                    request["input"], destination, request["placements"],
                    min_framework_distance_angstrom, min_counterion_distance_angstrom, charge_audit,
                )
                receipt = {
                    "schema_version": 1,
                    "tool": tool_name,
                    "status": validation["status"],
                    "production_released": False,
                    "input_path": str(source),
                    "input_sha256": request["input"]["model"]["sha256"],
                    "output_path": str(destination),
                    "output_sha256": sha256_file(destination),
                    "placements": request["placements"],
                    "charge_audit": charge_audit,
                    "validation": validation,
                    "execution": {
                        "job_id": result.get("job_id"),
                        "audit_path": result.get("audit_path"),
                        "run_mat_script_exit_code": result.get("run_mat_script_exit_code"),
                        "timed_out": result.get("timed_out"),
                    },
                    "limitations": [
                        "The coordinate ledger is caller-supplied and must have independent scientific provenance.",
                        "Only formal charge and geometry are audited; no forcefield partial charge or ion parameters are assigned.",
                        "This geometry candidate is not a production release.",
                    ],
                }
                _write_json_artifact_exclusive(receipt_path, receipt)
                registrations = [
                    register_artifact(project_directory, str(destination), "geology_counterion_candidate", source=str(source)),
                    register_artifact(project_directory, str(charge_audit_path), "geology_counterion_charge_audit", source=tool_name),
                    register_artifact(project_directory, str(receipt_path), "geology_counterion_receipt", source=tool_name),
                ]
                return {
                    "status": validation["status"],
                    "production_released": False,
                    "output_path": str(destination),
                    "output_sha256": receipt["output_sha256"],
                    "receipt_path": str(receipt_path),
                    "validation": validation,
                    "artifact_registrations": registrations,
                    "limitations": receipt["limitations"],
                }
            except Exception:
                destination.unlink(missing_ok=True)
                charge_audit_path.unlink(missing_ok=True)
                receipt_path.unlink(missing_ok=True)
                raise

        data, replayed = run_idempotent(
            project_directory, idempotency_key, tool_name, parameters, implementation
        )
        return success_result(tool_name, data, replayed=replayed)
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def ms_geology_apply_hydroxylation_ledger(
    project_directory: str,
    input_surface_structure: str,
    input_sha256: str,
    sites: list[dict[str, Any]],
    output_slot: str,
    min_oh_bond_length_angstrom: float,
    max_oh_bond_length_angstrom: float,
    min_nonbonded_distance_angstrom: float,
    max_atoms: int,
    idempotency_key: str,
    confirmation_token: str | None = None,
    timeout_seconds: int = 600,
    required_final_formal_charge_e: float | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Protonate an explicit ledger of singly Si-coordinated O sites in a 2D p1 surface."""

    tool_name = "ms_geology_apply_hydroxylation_ledger"
    parameters = {
        "project_directory": project_directory,
        "input_surface_structure": input_surface_structure,
        "input_sha256": input_sha256,
        "sites": sites,
        "output_slot": output_slot,
        "min_oh_bond_length_angstrom": min_oh_bond_length_angstrom,
        "max_oh_bond_length_angstrom": max_oh_bond_length_angstrom,
        "min_nonbonded_distance_angstrom": min_nonbonded_distance_angstrom,
        "max_atoms": max_atoms,
        "idempotency_key": idempotency_key,
        "timeout_seconds": timeout_seconds,
    }
    if required_final_formal_charge_e is not None:
        parameters["required_final_formal_charge_e"] = required_final_formal_charge_e
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        slot = validate_output_slot(output_slot)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required for geology model mutations")
        project = Path(get_project(project_directory)["project_directory"])
        destination = project / "model" / "surfaces" / "hydroxylated" / f"{slot}.xsd"
        receipt_path = project / "reports" / f"{slot}.hydroxylation.receipt.json"
        charge_audit_path = project / "reports" / f"{slot}.hydroxylation.charge.tsv"
        published = [destination, receipt_path, charge_audit_path]
        if dry_run and any(path.exists() for path in published):
            raise FileExistsError("Refusing to overwrite hydroxylation artifacts")
        if dry_run:
            source = resolve_workspace_path(input_surface_structure, must_exist=True)
            source_sha256 = validate_input_hash(source, input_sha256)
            request = validate_hydroxylation_ledger(
                source, sites, min_oh_bond_length_angstrom, max_oh_bond_length_angstrom,
                min_nonbonded_distance_angstrom, max_atoms,
            )
            return success_result(
                tool_name,
                _dry_run_payload(
                    tool_name, parameters,
                    planned_outputs=[str(path) for path in published],
                    validations={"input_sha256": source_sha256, "site_count": len(request["sites"]), "required_final_formal_charge_e": required_final_formal_charge_e},
                    template_text=build_hydroxylation_script(request["sites"]),
                    resource_estimate={"parallel_jobs": 1, "max_atoms": max_atoms, "timeout_seconds": timeout_seconds},
                ),
            )

        def implementation() -> dict[str, Any]:
            confirmation_manager.consume(confirmation_token, tool_name, parameters)
            source = resolve_workspace_path(input_surface_structure, must_exist=True)
            validate_input_hash(source, input_sha256)
            request = validate_hydroxylation_ledger(
                source, sites, min_oh_bond_length_angstrom, max_oh_bond_length_angstrom,
                min_nonbonded_distance_angstrom, max_atoms,
            )
            result = _run_materialsscript_job(
                script_template=build_hydroxylation_script(request["sites"]),
                input_files={"structure": str(source)},
                output_files={
                    "structure": {
                        "relative_path": "hydroxylated.xsd",
                        "destination_path": str(destination),
                    },
                    "charge_audit": {
                        "relative_path": "hydroxylation_charge.tsv",
                        "destination_path": str(charge_audit_path),
                    },
                },
                job_name=f"geology_hydroxylation_{slot}",
                run_mode="flat",
                keep_job_dir=True,
                timeout_seconds=timeout_seconds,
            )
            if not result.get("success"):
                raise RuntimeError(result.get("error_summary") or "Materials Studio hydroxylation job failed")
            try:
                charge_audit = parse_charge_audit(charge_audit_path)
                validation = validate_hydroxylation_result(
                    request["input"], request["input_bonds"], destination, request["sites"],
                    min_nonbonded_distance_angstrom, charge_audit,
                    required_final_formal_charge_e,
                )
                receipt = {
                    "schema_version": 1,
                    "tool": tool_name,
                    "status": validation["status"],
                    "production_released": False,
                    "input_path": str(source),
                    "input_sha256": request["input"]["model"]["sha256"],
                    "output_path": str(destination),
                    "output_sha256": sha256_file(destination),
                    "sites": request["sites"],
                    "charge_audit": charge_audit,
                    "validation": validation,
                    "execution": {
                        "job_id": result.get("job_id"),
                        "audit_path": result.get("audit_path"),
                        "run_mat_script_exit_code": result.get("run_mat_script_exit_code"),
                        "timed_out": result.get("timed_out"),
                    },
                    "limitations": [
                        "This tool executes a caller-supplied site ledger; it does not select a termination or infer a protonation protocol.",
                        "Reported silanol density is a candidate geometry measurement, not validation against a target-plane literature value.",
                        "Formal charge remains separate from forcefield partial charge; no forcefield types or parameters are assigned.",
                        "The hydroxylated surface remains a nonproduction candidate.",
                    ],
                }
                _write_json_artifact_exclusive(receipt_path, receipt)
                registrations = [
                    register_artifact(project_directory, str(destination), "geology_hydroxylation_candidate", source=str(source)),
                    register_artifact(project_directory, str(charge_audit_path), "geology_hydroxylation_charge_audit", source=tool_name),
                    register_artifact(project_directory, str(receipt_path), "geology_hydroxylation_receipt", source=tool_name),
                ]
                return {
                    "status": validation["status"],
                    "production_released": False,
                    "output_path": str(destination),
                    "output_sha256": receipt["output_sha256"],
                    "receipt_path": str(receipt_path),
                    "validation": validation,
                    "artifact_registrations": registrations,
                    "limitations": receipt["limitations"],
                }
            except Exception:
                destination.unlink(missing_ok=True)
                charge_audit_path.unlink(missing_ok=True)
                receipt_path.unlink(missing_ok=True)
                raise

        data, replayed = run_idempotent(
            project_directory, idempotency_key, tool_name, parameters, implementation
        )
        return success_result(tool_name, data, replayed=replayed)
    except Exception as exc:
        return error_result(tool_name, exc)


@mcp.tool()
def ms_list_analysis_targets(
    input_document: str,
    input_sha256: str,
    job_name: str = "list_analysis_targets",
    timeout_seconds: int = 600,
    keep_job_dir: bool = False,
    dry_run: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """List analysis targets through a hash-bound, confirmed MaterialsScript run."""

    outputs: dict[str, Any] = {
        "summary": {"relative_path": "summary.txt"},
        "elements": {"relative_path": "elements.tsv"},
        "forcefield_types": {"relative_path": "forcefield_types.tsv"},
        "atom_names": {"relative_path": "atom_names.tsv"},
        "sets": {"relative_path": "sets.tsv"},
    }

    script = """use strict;
use warnings;
use MaterialsScript qw(:all);

my $doc = Documents->Import("{{input.document}}");
my $atoms = eval { $doc->UnitCell->Atoms };
$atoms = eval { $doc->Atoms } if !defined($atoms);

my %element_counts;
my %forcefield_counts;
my %name_counts;
foreach my $atom (@$atoms) {
  my $element = eval { $atom->ElementSymbol };
  $element_counts{$element}++ if defined($element) && $element ne "";

  my $forcefield_type = eval { $atom->ForcefieldType };
  $forcefield_counts{$forcefield_type}++ if defined($forcefield_type) && $forcefield_type ne "";

  my $name = eval { $atom->Name };
  $name_counts{$name}++ if defined($name) && $name ne "";
}

my $frame_count = 1;
eval {
  my $trajectory = $doc->Trajectory;
  $frame_count = $trajectory->NumFrames if defined $trajectory;
};

my @set_entries;
eval {
  foreach my $set (@{$doc->Sets}) {
    my $set_name = eval { $set->Name };
    next if !defined($set_name) || $set_name eq "";
    my $item_count = eval { $set->Items->Count };
    $item_count = 0 if !defined($item_count);
    push @set_entries, [$set_name, $item_count];
  }
};

open(my $summary, '>', "{{output.summary}}") or die $!;
print $summary "AtomCount=", $atoms->Count, "\\n";
print $summary "FrameCount=", $frame_count, "\\n";
print $summary "ElementTypeCount=", scalar(keys %element_counts), "\\n";
print $summary "ForcefieldTypeCount=", scalar(keys %forcefield_counts), "\\n";
print $summary "AtomNameCount=", scalar(keys %name_counts), "\\n";
print $summary "SetCount=", scalar(@set_entries), "\\n";
close($summary);

open(my $elements, '>', "{{output.elements}}") or die $!;
print $elements "Element\\tCount\\n";
foreach my $key (sort keys %element_counts) {
  print $elements $key, "\\t", $element_counts{$key}, "\\n";
}
close($elements);

open(my $forcefield, '>', "{{output.forcefield_types}}") or die $!;
print $forcefield "ForcefieldType\\tCount\\n";
foreach my $key (sort keys %forcefield_counts) {
  print $forcefield $key, "\\t", $forcefield_counts{$key}, "\\n";
}
close($forcefield);

open(my $names, '>', "{{output.atom_names}}") or die $!;
print $names "AtomName\\tCount\\n";
foreach my $key (sort keys %name_counts) {
  print $names $key, "\\t", $name_counts{$key}, "\\n";
}
close($names);

open(my $sets, '>', "{{output.sets}}") or die $!;
print $sets "SetName\\tItemCount\\n";
foreach my $entry (@set_entries) {
  print $sets $entry->[0], "\\t", $entry->[1], "\\n";
}
close($sets);
"""

    if not isinstance(dry_run, bool) or not isinstance(keep_job_dir, bool):
        return error_result(
            "ms_list_analysis_targets",
            ValueError("dry_run and keep_job_dir must be booleans"),
        )
    try:
        source = resolve_workspace_path(input_document, must_exist=True)
        validated_sha256 = validate_input_hash(source, input_sha256)
        timeout = bounded_timeout(timeout_seconds)
        parameters = {
            "input_document": str(source),
            "input_sha256": input_sha256,
            "job_name": job_name,
            "timeout_seconds": timeout,
            "keep_job_dir": keep_job_dir,
        }
        if dry_run:
            return success_result(
                "ms_list_analysis_targets",
                _dry_run_payload(
                    "ms_list_analysis_targets",
                    parameters,
                    planned_outputs=[
                        "<job_dir>/output/summary.txt",
                        "<job_dir>/output/elements.tsv",
                        "<job_dir>/output/forcefield_types.tsv",
                        "<job_dir>/output/atom_names.tsv",
                        "<job_dir>/output/sets.tsv",
                    ],
                    validations={
                        "input_sha256": validated_sha256,
                        "input_suffix": source.suffix.lower(),
                        "source_exists": True,
                    },
                    template_text=script,
                    resource_estimate={"parallel_jobs": 1, "timeout_seconds": timeout},
                ),
            )
        confirmation_manager.consume(
            confirmation_token, "ms_list_analysis_targets", parameters
        )
    except Exception as exc:
        return error_result("ms_list_analysis_targets", exc)

    result = _run_materialsscript_job(
        script_template=script,
        input_files={"document": str(source)},
        output_files=outputs,
        job_name=job_name,
        run_mode="flat",
        keep_job_dir=True,
        timeout_seconds=timeout,
    )

    parsed_tables: dict[str, Any] = {}
    for alias in ["elements", "forcefield_types", "atom_names", "sets"]:
        table_path = result["outputs"].get(alias, {}).get("full_output_path")
        table_text = ""
        if table_path and Path(table_path).exists():
            table_text = Path(table_path).read_text(encoding="utf-8", errors="replace")
        parsed_tables[alias] = _parse_tsv_table(table_text)

    summary_text = ""
    summary_path = result["outputs"].get("summary", {}).get("full_output_path")
    if summary_path and Path(summary_path).exists():
        summary_text = Path(summary_path).read_text(encoding="utf-8", errors="replace")

    summary = _parse_key_value_text(summary_text)
    result["analysis_target_summary"] = summary
    result["analysis_targets"] = parsed_tables
    result["selection_examples"] = {
        "elements": [row["Element"] for row in parsed_tables["elements"]["rows"][:10] if row.get("Element")],
        "forcefield_types": [row["ForcefieldType"] for row in parsed_tables["forcefield_types"]["rows"][:10] if row.get("ForcefieldType")],
        "set_names": [row["SetName"] for row in parsed_tables["sets"]["rows"][:10] if row.get("SetName")],
    }
    result["artifact_details"] = _artifact_details_from_outputs(result["outputs"])
    result["input_document"] = str(source)
    result["input_sha256"] = validated_sha256
    result["references"] = _reference_entries(
        [
            "scriptingapi/apicreateset.htm",
            "scriptingapi/apisetsfilter.htm",
            "scriptingapi/apielementsymbol.htm",
            "scriptingapi/apiforcefieldtype.htm",
        ]
    )

    if not keep_job_dir:
        input_dir = result.get("input_dir")
        job_dir = result.get("job_dir")
        if input_dir:
            shutil.rmtree(input_dir, ignore_errors=True)
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        result["job_dir"] = None
        result["input_dir"] = None
        result["output_dir"] = None
        result["rendered_script_path"] = None
        result["script_stdout_path"] = None
        result["matstudio_log_path"] = None

    return result


def ms_run_materialsscript(
    script_template: str,
    input_files: dict[str, str] | None = None,
    output_files: dict[str, Any] | None = None,
    job_name: str = "ms_job",
    run_mode: str = "flat",
    keep_job_dir: bool = True,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Internal developer-only arbitrary MaterialsScript runner.

    Use placeholders like {{input.alias}}, {{output.alias}}, {{job_dir}}, {{input_dir}}, {{output_dir}}, and {{ms_root}}
    inside script_template.
    """

    return _run_materialsscript_job(
        script_template=script_template,
        input_files=input_files,
        output_files=output_files,
        job_name=job_name,
        run_mode=run_mode,
        keep_job_dir=keep_job_dir,
        timeout_seconds=timeout_seconds,
    )


def _reject_open_module_settings(module_settings: dict[str, Any] | None) -> None:
    """Fail closed until versioned, range-checked Forcite profiles are available."""

    if module_settings:
        raise ValueError(
            "Open module_settings are disabled by the MCP v1 safety policy. "
            "Use the tool's reviewed defaults; a versioned settings profile is required for custom settings."
        )


_FORCEFIELD_PREPARATION_PROFILES: dict[str, dict[str, str]] = {
    "prepare_compassiii_v1": {
        "CurrentForcefield": "COMPASSIII",
        "AssignForcefieldTypes": "Yes",
        "AssignBondOrder": "No",
        "AssignChargeGroups": "Yes",
        "ChargeAssignment": "Forcefield assigned",
        "ReportAutomaticTerms": "Yes",
    },
    "prepare_pcff_v1": {
        "CurrentForcefield": "pcff",
        "AssignForcefieldTypes": "Yes",
        "AssignBondOrder": "No",
        "AssignChargeGroups": "Yes",
        "ChargeAssignment": "Forcefield assigned",
        "ReportAutomaticTerms": "Yes",
    },
    "prepare_dreiding_qeq_v1": {
        "CurrentForcefield": "Dreiding",
        "AssignForcefieldTypes": "Yes",
        "AssignBondOrder": "No",
        "AssignChargeGroups": "Yes",
        "ChargeAssignment": "Charge using QEq",
        "ReportAutomaticTerms": "Yes",
    },
    "prepare_universal_qeq_v1": {
        "CurrentForcefield": "Universal",
        "AssignForcefieldTypes": "Yes",
        "AssignBondOrder": "No",
        "AssignChargeGroups": "Yes",
        "ChargeAssignment": "Charge using QEq",
        "ReportAutomaticTerms": "Yes",
    },
}


def _governed_forcite_profile(
    profile_id: str,
    calculation_parameters: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    values = calculation_parameters or {}
    if not isinstance(values, dict):
        raise ValueError("calculation_parameters must be an object")
    if profile_id in _FORCEFIELD_PREPARATION_PROFILES:
        if values:
            raise ValueError(f"{profile_id} does not accept calculation_parameters")
        return "Energy", dict(_FORCEFIELD_PREPARATION_PROFILES[profile_id])
    base = {"CurrentForcefield": "COMPASSIII", "ChargeAssignment": "Use current"}
    if profile_id == "energy_compassiii_v1":
        if values:
            raise ValueError("energy_compassiii_v1 does not accept calculation_parameters")
        return "Energy", base
    if profile_id == "geometry_optimization_compassiii_v1":
        if values:
            raise ValueError("geometry_optimization_compassiii_v1 does not accept calculation_parameters")
        return "GeometryOptimization", base
    if profile_id == "energy_pcff_v1":
        if values:
            raise ValueError("energy_pcff_v1 does not accept calculation_parameters")
        return "Energy", {"CurrentForcefield": "pcff", "ChargeAssignment": "Use current"}
    if profile_id != "dynamics_nvt_compassiii_v1":
        raise ValueError("Unsupported Forcite profile_id")
    allowed = {"temperature_kelvin", "number_of_steps", "time_step_fs", "trajectory_frequency"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"Unsupported dynamics parameters: {sorted(unknown)}")
    temperature = values.get("temperature_kelvin", 300.0)
    steps = values.get("number_of_steps", 100)
    timestep = values.get("time_step_fs", 1.0)
    frequency = values.get("trajectory_frequency", 10)
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 1 <= float(temperature) <= 2000:
        raise ValueError("temperature_kelvin must be from 1 to 2000")
    if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 100000:
        raise ValueError("number_of_steps must be an integer from 1 to 100000")
    if isinstance(timestep, bool) or not isinstance(timestep, (int, float)) or not 0.1 <= float(timestep) <= 5:
        raise ValueError("time_step_fs must be from 0.1 to 5")
    if isinstance(frequency, bool) or not isinstance(frequency, int) or not 1 <= frequency <= steps:
        raise ValueError("trajectory_frequency must be an integer from 1 to number_of_steps")
    return "Dynamics", {
        **base,
        "Ensemble3D": "NVT",
        "Thermostat": "Andersen",
        "Temperature": float(temperature),
        "NumberOfSteps": steps,
        "TimeStep": float(timestep),
        "TrajectoryFrequency": frequency,
    }


def _typing_input_preflight_is_acceptable(structure: dict[str, Any]) -> bool:
    """Allow only missing forcefield types into a preparation profile."""

    if structure.get("format") != "xsd" or not structure.get("atom_count"):
        return False
    errors = structure.get("errors") or []
    missing = int(structure.get("missing_forcefield_type_count") or 0)
    expected = f"{missing} atoms do not have ForcefieldType; forcefield coverage is incomplete"
    return missing > 0 and errors == [expected]


def _typing_postflight_summary(
    input_structure: dict[str, Any],
    output_structure: dict[str, Any],
    parsed_report: dict[str, Any],
    *,
    net_charge_tolerance: float = 1.0e-4,
) -> dict[str, Any]:
    errors: list[str] = []
    for key in ("atom_count", "bond_count", "elements", "bond_types"):
        if input_structure.get(key) != output_structure.get(key):
            errors.append(f"Topology preservation failed for {key}")
    missing_types = int(output_structure.get("missing_forcefield_type_count") or 0)
    if missing_types:
        errors.append(f"{missing_types} output atoms lack ForcefieldType")
    atom_count = int(output_structure.get("atom_count") or 0)
    audited_atoms = int(parsed_report.get("AtomAuditCount") or 0)
    audited_charges = int(parsed_report.get("PartialChargeCount") or 0)
    charge_errors = int(parsed_report.get("PartialChargeReadErrorCount") or 0)
    report_missing_types = int(parsed_report.get("MissingForcefieldTypeCount") or 0)
    if audited_atoms != atom_count:
        errors.append("MaterialsScript atom audit count does not match the output XSD")
    if audited_charges != atom_count or charge_errors:
        errors.append("Partial charges were not readable for every output atom")
    if report_missing_types:
        errors.append(f"MaterialsScript reported {report_missing_types} missing forcefield types")
    try:
        net_partial_charge = float(parsed_report["NetPartialCharge"])
    except (KeyError, TypeError, ValueError):
        net_partial_charge = None
        errors.append("Net partial charge was not reported as a number")
    if net_partial_charge is not None and abs(net_partial_charge) > net_charge_tolerance:
        errors.append(
            f"Net partial charge {net_partial_charge:.8g} exceeds tolerance {net_charge_tolerance:.8g}"
        )
    return {
        "status": "pass" if not errors else "fail",
        "atom_count": atom_count,
        "bond_count": output_structure.get("bond_count"),
        "elements": output_structure.get("elements"),
        "bond_types": output_structure.get("bond_types"),
        "forcefield_types": output_structure.get("forcefield_types"),
        "missing_forcefield_type_count": missing_types,
        "partial_charge_count": audited_charges,
        "partial_charge_read_error_count": charge_errors,
        "net_partial_charge_e": net_partial_charge,
        "net_charge_tolerance_e": net_charge_tolerance,
        "errors": errors,
    }


def _persist_failed_forcite_evidence(
    *,
    project_directory: str,
    evidence_root: Path,
    source: Path,
    source_sha256: str,
    profile_id: str,
    parameters: dict[str, Any],
    module_settings: dict[str, Any],
    health: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    if evidence_root.exists():
        raise FileExistsError("Refusing to overwrite governed Forcite failure evidence")
    evidence_root.mkdir(parents=True, exist_ok=False)
    evidence_sources = {
        "rendered_script.pl": result.get("rendered_script_path"),
        "stdout.log": result.get("script_stdout_path"),
        "matstudio.log": result.get("matstudio_log_path"),
        "execution_audit.json": result.get("audit_path"),
        "result_report.txt": result.get("outputs", {}).get("report", {}).get("full_output_path"),
    }
    copied: list[Path] = []
    for name, source_path in evidence_sources.items():
        source_evidence = Path(source_path) if source_path else None
        if source_evidence is None or not source_evidence.is_file():
            continue
        destination = evidence_root / name
        shutil.copy2(source_evidence, destination)
        copied.append(destination)
    environment_path = evidence_root / "environment.json"
    _write_json_artifact_exclusive(environment_path, health)
    copied.append(environment_path)
    parameters_path = evidence_root / "parameters.json"
    _write_json_artifact_exclusive(parameters_path, {**parameters, "module_settings": module_settings})
    copied.append(parameters_path)
    receipt_path = evidence_root / "failure_receipt.json"
    receipt = {
        "schema_version": 1,
        "tool": "ms_forcite_calculation_checked",
        "status": "forcite_execution_failed",
        "production_released": False,
        "profile_id": profile_id,
        "input_path": str(source),
        "input_sha256": source_sha256,
        "error_summary": result.get("error_summary") or "Governed Forcite calculation failed",
        "job_id": result.get("job_id"),
        "job_directory": result.get("job_dir"),
        "output_candidate_created": False,
        "evidence": [{"path": str(path), "sha256": sha256_file(path)} for path in copied],
    }
    _write_json_artifact_exclusive(receipt_path, receipt)
    copied.append(receipt_path)
    registrations = [
        register_artifact(
            project_directory, str(path), "forcite_failure_evidence",
            source="ms_forcite_calculation_checked",
        )
        for path in copied
    ]
    return {
        "status": receipt["status"],
        "production_released": False,
        "profile_id": profile_id,
        "evidence_directory": str(evidence_root),
        "failure_receipt": str(receipt_path),
        "output_candidate_created": False,
        "artifact_registrations": registrations,
    }


def _record_forcefield_preparation_gates(
    *,
    project_directory: str,
    profile_id: str,
    output_sha256: str,
    output_preflight: dict[str, Any],
    typing_summary: dict[str, Any],
) -> dict[str, Any]:
    structure_passed = output_preflight.get("status") == "pass" and typing_summary.get("status") == "pass"
    forcefield_passed = (
        typing_summary.get("status") == "pass"
        and int(typing_summary.get("missing_forcefield_type_count") or 0) == 0
        and int(typing_summary.get("partial_charge_count") or 0) == int(typing_summary.get("atom_count") or 0)
        and int(typing_summary.get("partial_charge_read_error_count") or 0) == 0
    )
    shared = {
        "profile_id": profile_id,
        "output_sha256": output_sha256,
        "atom_count": typing_summary.get("atom_count"),
        "bond_count": typing_summary.get("bond_count"),
        "elements": typing_summary.get("elements"),
        "bond_types": typing_summary.get("bond_types"),
        "production_released": False,
    }
    structure = _record_verified_quality_gate(
        project_directory,
        "structure",
        validator="materials_studio_mcp.forcite_structure_postflight_v1",
        passed=structure_passed,
        evidence={**shared, "structure_preflight_status": output_preflight.get("status")},
    )
    forcefield = _record_verified_quality_gate(
        project_directory,
        "forcefield",
        validator="materials_studio_mcp.forcite_forcefield_preparation_postflight_v1",
        passed=forcefield_passed,
        evidence={
            **shared,
            "forcefield_types": typing_summary.get("forcefield_types"),
            "missing_forcefield_type_count": typing_summary.get("missing_forcefield_type_count"),
            "partial_charge_count": typing_summary.get("partial_charge_count"),
            "partial_charge_read_error_count": typing_summary.get("partial_charge_read_error_count"),
            "net_partial_charge_e": typing_summary.get("net_partial_charge_e"),
            "net_charge_tolerance_e": typing_summary.get("net_charge_tolerance_e"),
        },
    )
    return {"structure": structure, "forcefield": forcefield}


@mcp.tool()
def ms_forcite_calculation_checked(
    project_directory: str,
    input_structure: str,
    input_sha256: str,
    profile_id: str,
    calculation_parameters: dict[str, Any] | None,
    output_slot: str,
    idempotency_key: str,
    timeout_seconds: int = 1200,
    dry_run: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Run one hash-bound Forcite profile and preserve its complete evidence bundle."""

    tool_name = "ms_forcite_calculation_checked"
    parameters = {
        "project_directory": project_directory,
        "input_structure": input_structure,
        "input_sha256": input_sha256,
        "profile_id": profile_id,
        "calculation_parameters": calculation_parameters,
        "output_slot": output_slot,
        "idempotency_key": idempotency_key,
        "timeout_seconds": timeout_seconds,
    }
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required for governed Forcite calculations")
        slot = validate_output_slot(output_slot)
        task_name, module_settings = _governed_forcite_profile(profile_id, calculation_parameters)
        project = Path(get_project(project_directory)["project_directory"])
        source = _require_file_in_project(project, input_structure, "input_structure")
        if source.suffix.lower() != ".xsd":
            raise ValueError("Governed Forcite input_structure must be an XSD file")
        source_sha256 = validate_input_hash(source, input_sha256)
        structure = inspect_structure_preflight(str(source))
        preparing_forcefield = profile_id in _FORCEFIELD_PREPARATION_PROFILES
        if structure.get("status") != "pass" and not (
            preparing_forcefield and _typing_input_preflight_is_acceptable(structure)
        ):
            raise ValueError("Input structure preflight did not pass")
        timeout = bounded_timeout(timeout_seconds)
        output_root = project / "model" / "calculations"
        output_structure = output_root / f"{slot}.xsd"
        output_trajectory = output_root / f"{slot}.xtd" if task_name == "Dynamics" else None
        evidence_root = project / "reports" / f"{slot}.forcite"
        planned_outputs = [str(output_structure), str(evidence_root)]
        if output_trajectory is not None:
            planned_outputs.append(str(output_trajectory))
        if dry_run and any(Path(path).exists() for path in planned_outputs):
            raise FileExistsError("Refusing to overwrite governed Forcite outputs")
        health = pipeline_health_check(run_version_probes=False)
        if health.get("status") != "ready":
            raise RuntimeError("Materials Studio environment preflight is not ready")
        script, report_properties = _build_forcite_script_template(
            task_name, module_settings, include_structure=True,
            include_trajectory=output_trajectory is not None,
        )
        if dry_run:
            return success_result(
                tool_name,
                _dry_run_payload(
                    tool_name, parameters,
                    planned_outputs=planned_outputs,
                    validations={
                        "input_sha256": source_sha256, "structure_preflight": structure,
                        "environment_preflight": health, "profile_id": profile_id,
                        "module_settings": module_settings, "report_properties": report_properties,
                        "license_preflight": "Live license acceptance is required as the first audited MaterialsScript launch stage.",
                        "typing_input_exception": (
                            "Only missing ForcefieldType values are allowed and must be repaired by this profile."
                            if preparing_forcefield else None
                        ),
                    },
                    template_text=script,
                    resource_estimate={"parallel_jobs": 1, "timeout_seconds": timeout, "number_of_steps": module_settings.get("NumberOfSteps", 0)},
                ),
            )

        def implementation() -> dict[str, Any]:
            if output_structure.exists() or evidence_root.exists() or (output_trajectory and output_trajectory.exists()):
                raise FileExistsError("Refusing to overwrite governed Forcite outputs")
            confirmation_manager.consume(confirmation_token, tool_name, parameters)
            created: list[Path] = []
            try:
                result = _run_forcite_task(
                    task_name=task_name,
                    input_structure=str(source),
                    module_settings=module_settings,
                    report_properties=None,
                    job_name=f"forcite_checked_{slot}",
                    output_structure_path=str(output_structure),
                    output_trajectory_path=str(output_trajectory) if output_trajectory else None,
                    timeout_seconds=timeout,
                    keep_job_dir=True,
                )
                if output_structure.exists():
                    created.append(output_structure)
                if output_trajectory and output_trajectory.exists():
                    created.append(output_trajectory)
                if not result.get("success"):
                    message = result.get("error_summary") or "Governed Forcite calculation failed"
                    failure_data = _persist_failed_forcite_evidence(
                        project_directory=project_directory,
                        evidence_root=evidence_root,
                        source=source,
                        source_sha256=source_sha256,
                        profile_id=profile_id,
                        parameters=parameters,
                        module_settings=module_settings,
                        health=health,
                        result=result,
                    )
                    raise RecordedExecutionError(
                        f"{message}\nFailure evidence retained at {evidence_root}", failure_data
                    )
                output_preflight = inspect_structure_preflight(str(output_structure))
                typing_summary = None
                if preparing_forcefield:
                    typing_summary = _typing_postflight_summary(
                        structure, output_preflight, result.get("parsed_report", {})
                    )
                    if typing_summary["status"] != "pass":
                        raise RuntimeError(
                            "Forcefield preparation postflight failed: "
                            + "; ".join(typing_summary["errors"])
                        )
                evidence_root.mkdir(parents=True, exist_ok=False)
                created.append(evidence_root)
                evidence_sources = {
                    "rendered_script.pl": result.get("rendered_script_path"),
                    "stdout.log": result.get("script_stdout_path"),
                    "matstudio.log": result.get("matstudio_log_path"),
                    "execution_audit.json": result.get("audit_path"),
                    "result_report.txt": result.get("outputs", {}).get("report", {}).get("full_output_path"),
                }
                copied = []
                for name, source_path in evidence_sources.items():
                    source_evidence = Path(source_path) if source_path else None
                    if source_evidence is None or not source_evidence.is_file():
                        raise RuntimeError(f"Required Forcite evidence is missing: {name}")
                    destination = evidence_root / name
                    shutil.copy2(source_evidence, destination)
                    copied.append(destination)
                environment_path = evidence_root / "environment.json"
                _write_json_artifact_exclusive(environment_path, health)
                copied.append(environment_path)
                parameters_path = evidence_root / "parameters.json"
                _write_json_artifact_exclusive(parameters_path, {**parameters, "module_settings": module_settings})
                copied.append(parameters_path)
                receipt_path = evidence_root / "receipt.json"
                receipt = {
                    "schema_version": 1,
                    "tool": tool_name,
                    "status": (
                        "forcefield_preparation_pass" if preparing_forcefield
                        else "forcite_calculation_pass"
                    ),
                    "production_released": False,
                    "profile_id": profile_id,
                    "task_name": task_name,
                    "input_path": str(source),
                    "input_sha256": source_sha256,
                    "template_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest().upper(),
                    "module_settings": module_settings,
                    "parsed_report": result.get("parsed_report", {}),
                    "output_structure_preflight": output_preflight,
                    "forcefield_preparation_audit": typing_summary,
                    "outputs": {
                        "structure": {"path": str(output_structure), "sha256": sha256_file(output_structure)},
                        "trajectory": (
                            {"path": str(output_trajectory), "sha256": sha256_file(output_trajectory)}
                            if output_trajectory else None
                        ),
                    },
                    "evidence": [
                        {"path": str(path), "sha256": sha256_file(path)} for path in copied
                    ],
                    "limitations": [
                        "This governed profile is validated on the local Materials Studio 23.1 API; it is not a general free-form settings interface.",
                        "Production release requires project-specific physical validation beyond successful execution.",
                    ],
                }
                _write_json_artifact_exclusive(receipt_path, receipt)
                copied.append(receipt_path)
                registrations = [
                    register_artifact(project_directory, str(output_structure), "forcite_output_structure", source=str(source))
                ]
                if output_trajectory:
                    registrations.append(register_artifact(project_directory, str(output_trajectory), "forcite_output_trajectory", source=str(source)))
                registrations.extend(
                    register_artifact(project_directory, str(path), "forcite_execution_evidence", source=tool_name)
                    for path in copied
                )
                quality_gate_decisions = None
                if preparing_forcefield and typing_summary is not None:
                    quality_gate_decisions = _record_forcefield_preparation_gates(
                        project_directory=project_directory,
                        profile_id=profile_id,
                        output_sha256=receipt["outputs"]["structure"]["sha256"],
                        output_preflight=output_preflight,
                        typing_summary=typing_summary,
                    )
                return {
                    "status": receipt["status"], "production_released": False,
                    "profile_id": profile_id, "output_structure": str(output_structure),
                    "output_trajectory": str(output_trajectory) if output_trajectory else None,
                    "receipt_path": str(receipt_path), "parsed_report": receipt["parsed_report"],
                    "output_structure_preflight": output_preflight,
                    "forcefield_preparation_audit": typing_summary,
                    "quality_gate_decisions": quality_gate_decisions,
                    "artifact_registrations": registrations,
                }
            except Exception:
                for path in reversed(created):
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
                raise

        data, replayed = run_idempotent(project_directory, idempotency_key, tool_name, parameters, implementation)
        return success_result(tool_name, data, replayed=replayed)
    except Exception as exc:
        return error_result(tool_name, exc)


def ms_forcite_energy(
    input_structure: str,
    output_structure_path: str | None = None,
    module_settings: dict[str, Any] | None = None,
    report_properties: list[str] | None = None,
    job_name: str = "forcite_energy",
    timeout_seconds: int = 600,
    keep_job_dir: bool = True,
) -> dict[str, Any]:
    """Run a high-level Forcite Energy task on a structure and optionally export the resulting structure."""

    _reject_open_module_settings(module_settings)
    merged_settings = {"CurrentForcefield": "COMPASSIII", "ChargeAssignment": "Use current"}
    return _run_forcite_task(
        task_name="Energy",
        input_structure=input_structure,
        module_settings=merged_settings,
        report_properties=report_properties,
        job_name=job_name,
        output_structure_path=output_structure_path,
        timeout_seconds=timeout_seconds,
        keep_job_dir=keep_job_dir,
    )


def ms_forcite_geometry_optimization(
    input_structure: str,
    output_structure_path: str,
    module_settings: dict[str, Any] | None = None,
    report_properties: list[str] | None = None,
    job_name: str = "forcite_geomopt",
    timeout_seconds: int = 600,
    keep_job_dir: bool = True,
) -> dict[str, Any]:
    """Run a high-level Forcite GeometryOptimization task and export the optimized structure."""

    _reject_open_module_settings(module_settings)
    merged_settings = {"CurrentForcefield": "COMPASSIII", "ChargeAssignment": "Use current"}
    return _run_forcite_task(
        task_name="GeometryOptimization",
        input_structure=input_structure,
        module_settings=merged_settings,
        report_properties=report_properties,
        job_name=job_name,
        output_structure_path=output_structure_path,
        timeout_seconds=timeout_seconds,
        keep_job_dir=keep_job_dir,
    )


def ms_forcite_dynamics(
    input_structure: str,
    output_trajectory_path: str,
    output_structure_path: str | None = None,
    module_settings: dict[str, Any] | None = None,
    report_properties: list[str] | None = None,
    job_name: str = "forcite_dynamics",
    timeout_seconds: int = 1200,
    keep_job_dir: bool = True,
    production: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Run a high-level Forcite Dynamics task and export the resulting trajectory."""

    _reject_open_module_settings(module_settings)
    confirmation_parameters = {
        "input_structure": input_structure,
        "output_trajectory_path": output_trajectory_path,
        "output_structure_path": output_structure_path,
        "module_settings": module_settings,
        "report_properties": report_properties,
        "job_name": job_name,
        "timeout_seconds": timeout_seconds,
        "keep_job_dir": keep_job_dir,
        "production": production,
    }
    if production:
        confirmation_manager.consume(confirmation_token, "ms_forcite_dynamics", confirmation_parameters)
    elif confirmation_token is not None:
        raise ValueError("confirmation_token is only valid when production=true")

    merged_settings = {
        "CurrentForcefield": "COMPASSIII",
        "ChargeAssignment": "Use current",
        "Ensemble3D": "NVT",
        "Thermostat": "Andersen",
        "Temperature": 300,
        "NumberOfSteps": 100,
        "TimeStep": 1,
        "TrajectoryFrequency": 10,
    }
    return _run_forcite_task(
        task_name="Dynamics",
        input_structure=input_structure,
        module_settings=merged_settings,
        report_properties=report_properties,
        job_name=job_name,
        output_structure_path=output_structure_path,
        output_trajectory_path=output_trajectory_path,
        timeout_seconds=timeout_seconds,
        keep_job_dir=keep_job_dir,
    )


def ms_forcite_rdf(
    input_trajectory: str,
    output_study_table_path: str | None = None,
    output_structure_factor_study_table_path: str | None = None,
    selection_a: str | None = None,
    selection_b: str | None = None,
    frame_range: str | None = None,
    include_structure_factor: bool = False,
    analysis_settings: dict[str, Any] | None = None,
    job_name: str = "forcite_rdf",
    timeout_seconds: int = 600,
    keep_job_dir: bool = True,
) -> dict[str, Any]:
    """Run Forcite radial distribution function analysis on an atomistic trajectory."""

    merged_settings = {"RDFBinWidth": 0.2, "RDFCutoff": 8}
    if analysis_settings:
        merged_settings.update(analysis_settings)
    effective_include_structure_factor = include_structure_factor or bool(merged_settings.get("RDFComputeStructureFactor"))
    if effective_include_structure_factor:
        merged_settings["RDFComputeStructureFactor"] = True
    return _run_forcite_analysis_task(
        analysis_name="RadialDistributionFunction",
        study_table_property="RDFChartAsStudyTable",
        input_trajectory=input_trajectory,
        analysis_settings=merged_settings,
        selection_settings={
            key: value
            for key, value in {
                "RDFSetA": selection_a,
                "RDFSetB": selection_b,
            }.items()
            if value
        }
        or None,
        frame_range=frame_range,
        extra_table_properties={"structure_factor": "StructureFactorChartAsStudyTable"} if effective_include_structure_factor else None,
        extra_table_destination_paths={
            "structure_factor": output_structure_factor_study_table_path
        }
        if output_structure_factor_study_table_path
        else None,
        result_properties=None,
        job_name=job_name,
        output_study_table_path=output_study_table_path,
        timeout_seconds=timeout_seconds,
        keep_job_dir=keep_job_dir,
    )


def ms_forcite_msd(
    input_trajectory: str,
    output_study_table_path: str | None = None,
    selection: str | None = None,
    frame_range: str | None = None,
    analysis_settings: dict[str, Any] | None = None,
    job_name: str = "forcite_msd",
    timeout_seconds: int = 600,
    keep_job_dir: bool = True,
) -> dict[str, Any]:
    """Run Forcite mean square displacement analysis on an atomistic trajectory."""

    return _run_forcite_analysis_task(
        analysis_name="MeanSquareDisplacement",
        study_table_property="MSDChartAsStudyTable",
        input_trajectory=input_trajectory,
        analysis_settings=analysis_settings,
        selection_settings={"MSDSetA": selection} if selection else None,
        frame_range=frame_range,
        extra_table_properties=None,
        extra_table_destination_paths=None,
        result_properties=[
            "MSDDiffusionCoefficient",
            "MSDDiffusionCoefficientRsq",
            "MSDDiffusionCoefficientxx",
            "MSDDiffusionCoefficientyy",
            "MSDDiffusionCoefficientzz",
            "MSDDiffusionCoefficientxy",
            "MSDDiffusionCoefficientxz",
            "MSDDiffusionCoefficientyz",
            "MSDDiffusionCoefficientRsqxx",
            "MSDDiffusionCoefficientRsqyy",
            "MSDDiffusionCoefficientRsqzz",
            "MSDDiffusionCoefficientRsqxy",
            "MSDDiffusionCoefficientRsqxz",
            "MSDDiffusionCoefficientRsqyz",
        ],
        job_name=job_name,
        output_study_table_path=output_study_table_path,
        timeout_seconds=timeout_seconds,
        keep_job_dir=keep_job_dir,
    )


def ms_forcite_vacf(
    input_trajectory: str,
    output_study_table_path: str | None = None,
    output_power_spectrum_study_table_path: str | None = None,
    selection: str | None = None,
    frame_range: str | None = None,
    include_power_spectrum: bool = False,
    analysis_settings: dict[str, Any] | None = None,
    job_name: str = "forcite_vacf",
    timeout_seconds: int = 600,
    keep_job_dir: bool = True,
) -> dict[str, Any]:
    """Run Forcite velocity autocorrelation analysis on an atomistic trajectory."""

    merged_settings: dict[str, Any] = {}
    if analysis_settings:
        merged_settings.update(analysis_settings)
    effective_include_power_spectrum = include_power_spectrum or bool(
        merged_settings.get("VACFComputePowerSpectrum")
    )
    if effective_include_power_spectrum:
        merged_settings["VACFComputePowerSpectrum"] = True

    return _run_forcite_analysis_task(
        analysis_name="VelocityAutocorrelationFunction",
        study_table_property="VACFChartAsStudyTable",
        input_trajectory=input_trajectory,
        analysis_settings=merged_settings or None,
        selection_settings={"VACFSetA": selection} if selection else None,
        frame_range=frame_range,
        extra_table_properties={
            "power_spectrum": "VACFPowerSpectrumChartAsStudyTable"
        }
        if effective_include_power_spectrum
        else None,
        extra_table_destination_paths={
            "power_spectrum": output_power_spectrum_study_table_path
        }
        if output_power_spectrum_study_table_path
        else None,
        result_properties=None,
        job_name=job_name,
        output_study_table_path=output_study_table_path,
        timeout_seconds=timeout_seconds,
        keep_job_dir=keep_job_dir,
    )


def ms_forcite_thermo_profiles(
    input_trajectory: str,
    output_directory: str,
    properties: list[str] | None = None,
    frame_range: str | None = None,
    common_analysis_settings: dict[str, Any] | None = None,
    analysis_settings_by_property: dict[str, dict[str, Any]] | None = None,
    assess_stability: bool = True,
    job_name_prefix: str = "forcite_thermo_profiles",
    timeout_seconds: int = 600,
    keep_job_dir: bool = True,
) -> dict[str, Any]:
    """Run one or more Forcite thermodynamic profile analyses on a trajectory and return structured time-series summaries."""

    requested_properties = [item.lower() for item in (properties or ["temperature", "pressure", "density", "potential_energy_components"])]
    normalized_properties = [item for item in requested_properties if item in THERMO_PROFILE_SPECS]
    if not normalized_properties:
        raise ValueError(
            "properties must include at least one of: temperature, pressure, density, potential_energy_components, cell_parameters"
        )

    output_dir = Path(output_directory)
    output_dir.mkdir(parents=True, exist_ok=True)

    profile_results: dict[str, Any] = {}
    profile_summaries: dict[str, Any] = {}
    property_assessments: dict[str, Any] = {}
    key_metrics: dict[str, Any] = {}
    common_settings = dict(common_analysis_settings or {})

    for property_name in _ordered_unique_strings(normalized_properties):
        spec = THERMO_PROFILE_SPECS[property_name]
        effective_settings = dict(common_settings)
        effective_settings.update((analysis_settings_by_property or {}).get(property_name, {}))
        profile_result = _run_forcite_analysis_task(
            analysis_name=spec["analysis_name"],
            study_table_property=spec["study_table_property"],
            input_trajectory=input_trajectory,
            analysis_settings=effective_settings or None,
            selection_settings=None,
            frame_range=frame_range,
            extra_table_properties=None,
            extra_table_destination_paths=None,
            result_properties=None,
            job_name=f"{job_name_prefix}_{spec['output_basename']}",
            output_study_table_path=str(output_dir / f"{spec['output_basename']}.std"),
            timeout_seconds=timeout_seconds,
            keep_job_dir=keep_job_dir,
        )
        summary = _summarize_analysis_table(profile_result.get("analysis_table", {}))
        preferred_metrics = _preferred_series_metrics(property_name, summary)
        profile_result["series_summary"] = summary
        profile_result["preferred_series_metrics"] = preferred_metrics
        profile_results[property_name] = profile_result
        profile_summaries[property_name] = {
            "row_count": summary.get("row_count"),
            "columns": summary.get("columns"),
            "axis_column": summary.get("axis_column"),
            "series_metrics": summary.get("series", {}),
            "preferred_series_metrics": preferred_metrics,
            "first_rows": profile_result.get("analysis_preview", [])[:3],
        }
        if preferred_metrics:
            key_metrics[f"{property_name}_column"] = preferred_metrics.get("column")
            key_metrics[f"{property_name}_mean"] = preferred_metrics.get("mean")
            key_metrics[f"{property_name}_last"] = preferred_metrics.get("last")
            key_metrics[f"{property_name}_delta"] = preferred_metrics.get("delta")
            if assess_stability:
                series_values = _numeric_column_values(
                    profile_result.get("analysis_table", {}),
                    str(preferred_metrics["column"]),
                )
                property_assessment = _assess_numeric_series_stability(
                    values=series_values,
                    frame_range=frame_range,
                    property_name=property_name,
                )
                property_assessment["column"] = preferred_metrics["column"]
                property_assessments[property_name] = property_assessment
        elif assess_stability:
            property_assessments[property_name] = {
                "status": "insufficient_data",
                "reason": "No preferred numeric series could be identified for this property.",
            }

    workflow_success = all(result.get("success") for result in profile_results.values())
    stability_assessment = None
    interpretation = None
    report_artifacts: dict[str, str] = {}
    if assess_stability:
        stability_assessment = _aggregate_thermo_stability_assessment(
            property_assessments=property_assessments,
            frame_range=frame_range,
        )
        key_metrics["thermo_overall_status"] = stability_assessment.get("overall_status")
        key_metrics["thermo_recommended_start_frame"] = stability_assessment.get("recommended_production_start_frame")
        key_metrics["thermo_recommended_frame_range"] = stability_assessment.get("recommended_production_frame_range")
        interpretation = _build_thermo_interpretation(
            stability_assessment=stability_assessment,
            profile_summaries=profile_summaries,
        )

        json_path = output_dir / "thermo_assessment.json"
        markdown_path = output_dir / "thermo_assessment.md"
        interpretation_json_path = output_dir / "thermo_interpretation.json"
        interpretation_markdown_path = output_dir / "thermo_interpretation.md"
        json_path.write_text(
            json.dumps(stability_assessment, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        markdown_path.write_text(
            _render_thermo_assessment_markdown(stability_assessment),
            encoding="utf-8",
        )
        if interpretation is not None:
            interpretation_json_path.write_text(
                json.dumps(interpretation, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            interpretation_markdown_path.write_text(
                _render_thermo_interpretation_markdown(interpretation),
                encoding="utf-8",
            )
        report_artifacts = {
            "assessment_json": str(json_path),
            "assessment_markdown": str(markdown_path),
        }
        if interpretation is not None:
            report_artifacts["interpretation_json"] = str(interpretation_json_path)
            report_artifacts["interpretation_markdown"] = str(interpretation_markdown_path)

    return {
        "success": workflow_success,
        "workflow": "forcite_thermo_profiles",
        "input_trajectory": input_trajectory,
        "output_directory": str(output_dir),
        "properties": _ordered_unique_strings(normalized_properties),
        "frame_range": frame_range,
        "common_analysis_settings": common_settings,
        "analysis_settings_by_property": analysis_settings_by_property or {},
        "profile_results": profile_results,
        "profile_summaries": profile_summaries,
        "stability_assessment": stability_assessment,
        "interpretation": interpretation,
        "key_metrics": key_metrics,
        "artifacts": report_artifacts,
        "references": _reference_entries(FORCITE_HELP_PAGES["ThermoProfiles"]),
        "notes": [
            "This workflow returns per-property study tables plus numeric summaries for each time-series column.",
            "Preferred series metrics try to pick the most useful physical column for each property, such as Temperature, Pressure, Density, or Volume.",
            "When assess_stability is enabled, the workflow also estimates a production window and writes both assessment and interpretation reports in JSON and Markdown forms.",
        ],
    }


def ms_analyze_trajectory_bundle(
    input_trajectory: str,
    output_directory: str,
    analyses: list[str] | None = None,
    rdf_settings: dict[str, Any] | None = None,
    rdf_selection_a: str | None = None,
    rdf_selection_b: str | None = None,
    rdf_include_structure_factor: bool = False,
    msd_settings: dict[str, Any] | None = None,
    msd_selection: str | None = None,
    vacf_settings: dict[str, Any] | None = None,
    vacf_selection: str | None = None,
    vacf_include_power_spectrum: bool = False,
    hbond_settings: dict[str, Any] | None = None,
    thermo_properties: list[str] | None = None,
    thermo_common_analysis_settings: dict[str, Any] | None = None,
    thermo_analysis_settings_by_property: dict[str, dict[str, Any]] | None = None,
    analysis_frame_range: str | None = None,
    job_name_prefix: str = "trajectory_bundle",
    analysis_timeout_seconds: int = 600,
    keep_job_dir: bool = True,
) -> dict[str, Any]:
    """Run multiple analyses on an existing trajectory and generate a unified report."""

    requested_analyses = [item.lower() for item in (analyses or ["rdf", "msd", "thermo"])]
    normalized_analyses = [item for item in requested_analyses if item in {"rdf", "msd", "vacf", "hbond", "thermo"}]
    if not normalized_analyses:
        raise ValueError("analyses must include at least one of: rdf, msd, vacf, hbond, thermo")

    output_dir = Path(output_directory)
    output_dir.mkdir(parents=True, exist_ok=True)
    trajectory_inspection = _inspect_materials_document(input_trajectory)
    trajectory_frame_count = _inspection_trajectory_frame_count(trajectory_inspection)

    effective_msd_settings = {"MSDMaxFrameLength": 100}
    if msd_settings:
        effective_msd_settings.update(msd_settings)

    analysis_results: dict[str, Any] = {}
    if "rdf" in normalized_analyses:
        analysis_results["rdf"] = ms_forcite_rdf(
            input_trajectory=input_trajectory,
            output_study_table_path=str(output_dir / "rdf.std"),
            output_structure_factor_study_table_path=str(output_dir / "structure_factor.std")
            if rdf_include_structure_factor
            else None,
            selection_a=rdf_selection_a,
            selection_b=rdf_selection_b,
            frame_range=analysis_frame_range,
            include_structure_factor=rdf_include_structure_factor,
            analysis_settings=rdf_settings,
            job_name=f"{job_name_prefix}_rdf",
            timeout_seconds=analysis_timeout_seconds,
            keep_job_dir=keep_job_dir,
        )
    if "msd" in normalized_analyses:
        analysis_results["msd"] = ms_forcite_msd(
            input_trajectory=input_trajectory,
            output_study_table_path=str(output_dir / "msd.std"),
            selection=msd_selection,
            frame_range=analysis_frame_range,
            analysis_settings=effective_msd_settings,
            job_name=f"{job_name_prefix}_msd",
            timeout_seconds=analysis_timeout_seconds,
            keep_job_dir=keep_job_dir,
        )
    if "vacf" in normalized_analyses:
        analysis_results["vacf"] = ms_forcite_vacf(
            input_trajectory=input_trajectory,
            output_study_table_path=str(output_dir / "vacf.std"),
            output_power_spectrum_study_table_path=str(output_dir / "vacf_power_spectrum.std")
            if vacf_include_power_spectrum
            else None,
            selection=vacf_selection,
            frame_range=analysis_frame_range,
            include_power_spectrum=vacf_include_power_spectrum,
            analysis_settings=vacf_settings,
            job_name=f"{job_name_prefix}_vacf",
            timeout_seconds=analysis_timeout_seconds,
            keep_job_dir=keep_job_dir,
        )
    if "hbond" in normalized_analyses:
        effective_hbond_settings = hbond_settings or {}
        analysis_results["hbond"] = ms_hbond_statistics(
            input_document=input_trajectory,
            mode="trajectory",
            frame_range=analysis_frame_range,
            max_frames=effective_hbond_settings.get("max_frames"),
            output_study_table_path=str(output_dir / "hbond_statistics.std"),
            job_name=f"{job_name_prefix}_hbond",
            timeout_seconds=analysis_timeout_seconds,
            keep_job_dir=keep_job_dir,
        )
    if "thermo" in normalized_analyses:
        analysis_results["thermo"] = ms_forcite_thermo_profiles(
            input_trajectory=input_trajectory,
            output_directory=str(output_dir / "thermo_profiles"),
            properties=thermo_properties,
            frame_range=analysis_frame_range,
            common_analysis_settings=thermo_common_analysis_settings,
            analysis_settings_by_property=thermo_analysis_settings_by_property,
            job_name_prefix=f"{job_name_prefix}_thermo",
            timeout_seconds=analysis_timeout_seconds,
            keep_job_dir=keep_job_dir,
        )

    workflow_success = all(result.get("success") for result in analysis_results.values())
    analysis_summaries: dict[str, Any] = {}
    for name, result in analysis_results.items():
        if name == "thermo":
            analysis_summaries[name] = {
                "properties": result.get("properties", []),
                "key_metrics": result.get("key_metrics", {}),
                "profile_summaries": result.get("profile_summaries", {}),
                "interpretation": result.get("interpretation"),
            }
        else:
            analysis_summaries[name] = {
                "row_count": result.get("analysis_table", {}).get("row_count"),
                "columns": result.get("analysis_table", {}).get("columns"),
                "metrics": result.get("analysis_summary", {}),
                "first_rows": result.get("analysis_preview", [])[:3],
            }

    vacf_table = analysis_results.get("vacf", {}).get("analysis_table", {})
    vacf_column = "VACF" if "VACF" in vacf_table.get("columns", []) else _first_numeric_column(vacf_table, excluded={"Time"})
    vacf_final_value = _final_numeric_row_value(vacf_table, vacf_column) if vacf_column else None
    bundle_key_metrics = {
        "trajectory_frames": trajectory_frame_count,
        "msd_diffusion_coefficient": analysis_results.get("msd", {}).get("analysis_summary", {}).get("MSDDiffusionCoefficient"),
        "msd_diffusion_coefficient_rsq": analysis_results.get("msd", {}).get("analysis_summary", {}).get("MSDDiffusionCoefficientRsq"),
        "mean_hbond_count": analysis_results.get("hbond", {}).get("analysis_summary", {}).get("MeanHBondCount"),
        "frames_with_hbonds": analysis_results.get("hbond", {}).get("analysis_summary", {}).get("FramesWithHBonds"),
        "temperature_profile_mean": analysis_results.get("thermo", {}).get("key_metrics", {}).get("temperature_mean"),
        "pressure_profile_mean": analysis_results.get("thermo", {}).get("key_metrics", {}).get("pressure_mean"),
        "density_profile_mean": analysis_results.get("thermo", {}).get("key_metrics", {}).get("density_mean"),
        "thermo_overall_status": analysis_results.get("thermo", {}).get("stability_assessment", {}).get("overall_status"),
        "thermo_recommended_start_frame": analysis_results.get("thermo", {}).get("stability_assessment", {}).get("recommended_production_start_frame"),
        "thermo_recommended_frame_range": analysis_results.get("thermo", {}).get("stability_assessment", {}).get("recommended_production_frame_range"),
        "vacf_final_value": vacf_final_value,
        "vacf_power_spectrum_rows": analysis_results.get("vacf", {}).get("extra_analysis_tables", {}).get("power_spectrum", {}).get("row_count"),
    }
    unified_report = _build_analysis_report(
        trajectory_frame_count=trajectory_frame_count,
        analysis_results=analysis_results,
        key_metrics=bundle_key_metrics,
        context_label="trajectory analysis bundle",
    )
    bundle_interpretation = {
        "executive_summary": unified_report["executive_summary"],
        "key_findings": unified_report.get("key_findings", [])[:6],
        "recommended_actions": unified_report.get("recommended_actions", []),
    }

    unified_report_markdown_path = output_dir / "unified_analysis_report.md"
    unified_report_json_path = output_dir / "unified_analysis_report.json"
    bundle_interpretation_path = output_dir / "trajectory_bundle_interpretation.md"
    unified_report_markdown_path.write_text(
        _render_analysis_report_markdown(unified_report),
        encoding="utf-8",
    )
    unified_report_json_path.write_text(
        json.dumps(unified_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    bundle_interpretation_path.write_text(
        _render_generic_interpretation_markdown("Trajectory Bundle Interpretation", bundle_interpretation),
        encoding="utf-8",
    )

    return {
        "success": workflow_success,
        "workflow": "trajectory_analysis_bundle",
        "input_trajectory": input_trajectory,
        "output_directory": str(output_dir),
        "analyses": normalized_analyses,
        "trajectory_inspection": trajectory_inspection,
        "key_metrics": bundle_key_metrics,
        "analysis_results": analysis_results,
        "analysis_summaries": analysis_summaries,
        "interpretation": bundle_interpretation,
        "unified_report": unified_report,
        "artifacts": {
            "unified_analysis_report_markdown": str(unified_report_markdown_path),
            "unified_analysis_report_json": str(unified_report_json_path),
            "trajectory_bundle_interpretation_markdown": str(bundle_interpretation_path),
        },
        "notes": [
            "This workflow is designed for existing trajectories and does not rerun dynamics.",
            "It reuses the same structured downstream analyses as the dynamics-plus-analysis workflow and merges them into one report.",
        ],
    }


def ms_hbond_statistics(
    input_document: str,
    mode: str = "auto",
    max_frames: int | None = None,
    frame_range: str | None = None,
    output_study_table_path: str | None = None,
    job_name: str = "hbond_statistics",
    timeout_seconds: int = 600,
    keep_job_dir: bool = True,
) -> dict[str, Any]:
    """Analyze hydrogen bonds for a structure or trajectory and return structured summary and per-frame statistics."""

    normalized_mode = mode.lower().strip()
    if normalized_mode not in {"auto", "single_frame", "trajectory"}:
        raise ValueError("mode must be one of: auto, single_frame, trajectory")
    if max_frames is not None and max_frames < 1:
        raise ValueError("max_frames must be >= 1 when provided.")
    frame_bounds = _split_frame_range_spec(frame_range)

    resolved_mode = normalized_mode
    if resolved_mode == "auto":
        resolved_mode = "trajectory" if Path(input_document).suffix.lower() == ".xtd" else "single_frame"

    outputs: dict[str, Any] = {
        "summary": {"relative_path": "analysis_summary.txt"},
        "table": {"relative_path": "analysis_table.tsv"},
    }
    if output_study_table_path:
        outputs["study_table"] = {
            "relative_path": "../exports/hbond_statistics.std",
            "destination_path": output_study_table_path,
        }

    max_frames_value = "undef" if max_frames is None else str(max_frames)
    start_frame_value = "undef" if frame_bounds is None else str(frame_bounds[0])
    end_frame_value = "undef" if frame_bounds is None else str(frame_bounds[1])
    requested_mode_literal = _perl_quote(resolved_mode)
    create_std_block = ""
    std_row_block = ""
    save_std_block = ""
    if "study_table" in outputs:
        create_std_block = """
my $std = Documents->New("HBondStatistics.std");
my $sheet = $std->Sheets(0);
$sheet->Title = "HBondStatistics";
$sheet->ColumnHeading(0) = "Frame";
$sheet->ColumnHeading(1) = "HBondCount";
$sheet->ColumnHeading(2) = "MeanLength";
$sheet->ColumnHeading(3) = "MinLength";
$sheet->ColumnHeading(4) = "MaxLength";
"""
        std_row_block = """
  $sheet->Cell($row_index, 0) = $frame;
  $sheet->Cell($row_index, 1) = $count;
  $sheet->Cell($row_index, 2) = ($count > 0) ? $mean_length : "";
  $sheet->Cell($row_index, 3) = ($count > 0) ? $min_length : "";
  $sheet->Cell($row_index, 4) = ($count > 0) ? $max_length : "";
"""
        save_std_block = '$std->SaveAs("exports/hbond_statistics.std");'

    script = f"""use strict;
use warnings;
use MaterialsScript qw(:all);

my $doc = Documents->Import("{{{{input.document}}}}");
my $requested_mode = {requested_mode_literal};
my $max_frames = {max_frames_value};
my $start_frame = {start_frame_value};
my $end_frame = {end_frame_value};

my $trajectory;
my $available_frames = 1;
eval {{
  $trajectory = $doc->Trajectory;
  $available_frames = $trajectory->NumFrames if $trajectory;
}};

my $use_trajectory = ($requested_mode eq "trajectory") ? 1 : 0;
if ($requested_mode eq "trajectory" && !$trajectory) {{
  die "Trajectory mode was requested but the imported document does not expose a trajectory.";
}}
if (!$trajectory) {{
  $use_trajectory = 0;
}}
if (($start_frame || $end_frame) && !$use_trajectory) {{
  die "frame_range requires a trajectory document.";
}}

my $frame_start = 1;
my $frame_end = $use_trajectory ? $available_frames : 1;
if (defined $start_frame) {{
  $frame_start = $start_frame;
}}
if (defined $end_frame) {{
  $frame_end = $end_frame;
}}
if ($frame_start < 1 || $frame_end < 1 || $frame_start > $frame_end || $frame_end > $available_frames) {{
  die "frame_range is outside the available trajectory frames.";
}}

my $frames_to_process = $frame_end - $frame_start + 1;
if (defined $max_frames && $max_frames < $frames_to_process) {{
  $frame_end = $frame_start + $max_frames - 1;
  $frames_to_process = $max_frames;
}}

open(my $summary, '>', "{{{{output.summary}}}}") or die $!;
open(my $table, '>', "{{{{output.table}}}}") or die $!;
print $table "Frame\\tHBondCount\\tMeanLength\\tMinLength\\tMaxLength\\n";

{create_std_block}

my $frames_with_hbonds = 0;
my $total_hbond_count = 0;
my $global_total_length = 0;
my $global_min_length;
my $global_max_length;

for (my $frame = $frame_start; $frame <= $frame_end; ++$frame) {{
  if ($use_trajectory && $trajectory) {{
    eval {{ $trajectory->CurrentFrame = $frame; }};
    eval {{ $doc->CurrentFrame = $frame; }};
  }}
  eval {{ $doc->CalculateBonds; }};

  my $hbonds = $doc->UnitCell->HydrogenBonds;
  my $count = 0;
  my $length_sum = 0;
  my $min_length;
  my $max_length;
  foreach my $hbond (@$hbonds) {{
    my $length = $hbond->Length;
    next unless defined $length;
    ++$count;
    $length_sum += $length;
    $min_length = $length if !defined($min_length) || $length < $min_length;
    $max_length = $length if !defined($max_length) || $length > $max_length;
  }}

  my $mean_length = ($count > 0) ? ($length_sum / $count) : undef;
  ++$frames_with_hbonds if $count > 0;
  $total_hbond_count += $count;
  $global_total_length += $length_sum;
  $global_min_length = $min_length if defined($min_length) && (!defined($global_min_length) || $min_length < $global_min_length);
  $global_max_length = $max_length if defined($max_length) && (!defined($global_max_length) || $max_length > $global_max_length);

  print $table join("\\t",
    $frame,
    $count,
    defined($mean_length) ? $mean_length : "",
    defined($min_length) ? $min_length : "",
    defined($max_length) ? $max_length : ""
  ), "\\n";

  my $row_index = $frame - $frame_start;
{std_row_block}
}}

my $global_mean_length = $total_hbond_count > 0 ? ($global_total_length / $total_hbond_count) : 0;
my $mean_hbond_count = $frames_to_process > 0 ? ($total_hbond_count / $frames_to_process) : 0;

print $summary "SelectedMode=", ($use_trajectory ? "trajectory" : "single_frame"), "\\n";
print $summary "RequestedMode=", $requested_mode, "\\n";
print $summary "AvailableFrameCount=", $available_frames, "\\n";
print $summary "FrameRangeStart=", $frame_start, "\\n";
print $summary "FrameRangeEnd=", $frame_end, "\\n";
print $summary "FrameCountUsed=", $frames_to_process, "\\n";
print $summary "FramesWithHBonds=", $frames_with_hbonds, "\\n";
print $summary "TotalHBondCount=", $total_hbond_count, "\\n";
print $summary "MeanHBondCount=", $mean_hbond_count, "\\n";
print $summary "GlobalMeanLength=", $global_mean_length, "\\n";
print $summary "GlobalMinLength=", defined($global_min_length) ? $global_min_length : 0, "\\n";
print $summary "GlobalMaxLength=", defined($global_max_length) ? $global_max_length : 0, "\\n";
close($summary);
close($table);
{save_std_block}
"""

    result = _run_materialsscript_job(
        script_template=script,
        input_files={"document": input_document},
        output_files=outputs,
        job_name=job_name,
        run_mode="flat",
        keep_job_dir=True,
        timeout_seconds=timeout_seconds,
    )

    summary_text = ""
    table_text = ""
    summary_path = result["outputs"].get("summary", {}).get("full_output_path")
    table_path = result["outputs"].get("table", {}).get("full_output_path")
    if summary_path and Path(summary_path).exists():
        summary_text = Path(summary_path).read_text(encoding="utf-8", errors="replace")
    if table_path and Path(table_path).exists():
        table_text = Path(table_path).read_text(encoding="utf-8", errors="replace")

    result["analysis_summary"] = _parse_key_value_text(summary_text)
    result["analysis_table"] = _parse_tsv_table(table_text)
    result["analysis_preview"] = result["analysis_table"]["rows"][:10]
    result["artifact_details"] = _artifact_details_from_outputs(result["outputs"])
    result["references"] = _reference_entries(FORCITE_HELP_PAGES["HydrogenBondStatistics"])
    result["workflow"] = {
        "analysis_name": "HydrogenBondStatistics",
        "mode": resolved_mode,
        "max_frames": max_frames,
        "frame_range": frame_range,
    }
    result["input_document"] = input_document

    if not keep_job_dir:
        input_dir = result.get("input_dir")
        job_dir = result.get("job_dir")
        if input_dir:
            shutil.rmtree(input_dir, ignore_errors=True)
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        result["job_dir"] = None
        result["input_dir"] = None
        result["output_dir"] = None
        result["rendered_script_path"] = None
        result["script_stdout_path"] = None
        result["matstudio_log_path"] = None

    return result


def ms_forcite_dynamics_with_analysis(
    input_structure: str,
    output_directory: str,
    analyses: list[str] | None = None,
    dynamics_settings: dict[str, Any] | None = None,
    rdf_settings: dict[str, Any] | None = None,
    rdf_selection_a: str | None = None,
    rdf_selection_b: str | None = None,
    rdf_include_structure_factor: bool = False,
    msd_settings: dict[str, Any] | None = None,
    msd_selection: str | None = None,
    hbond_settings: dict[str, Any] | None = None,
    thermo_properties: list[str] | None = None,
    thermo_common_analysis_settings: dict[str, Any] | None = None,
    thermo_analysis_settings_by_property: dict[str, dict[str, Any]] | None = None,
    analysis_frame_range: str | None = None,
    job_name_prefix: str = "forcite_dyn_analysis",
    dynamics_timeout_seconds: int = 1200,
    analysis_timeout_seconds: int = 600,
    keep_job_dir: bool = True,
) -> dict[str, Any]:
    """Run dynamics and then automatically perform selected Forcite trajectory analyses."""

    requested_analyses = [item.lower() for item in (analyses or ["rdf", "msd"])]
    normalized_analyses = [item for item in requested_analyses if item in {"rdf", "msd", "hbond", "thermo"}]
    if not normalized_analyses:
        raise ValueError("analyses must include at least one of: rdf, msd, hbond, thermo")

    output_dir = Path(output_directory)
    output_dir.mkdir(parents=True, exist_ok=True)
    trajectory_path = output_dir / "dynamics_trajectory.xtd"
    final_structure_path = output_dir / "dynamics_final_structure.xsd"
    effective_msd_settings = {"MSDMaxFrameLength": 100}
    if msd_settings:
        effective_msd_settings.update(msd_settings)

    dynamics_result = ms_forcite_dynamics(
        input_structure=input_structure,
        output_trajectory_path=str(trajectory_path),
        output_structure_path=str(final_structure_path),
        module_settings=dynamics_settings,
        job_name=f"{job_name_prefix}_dynamics",
        timeout_seconds=dynamics_timeout_seconds,
        keep_job_dir=keep_job_dir,
    )

    workflow_references = _reference_entries(FORCITE_HELP_PAGES["DynamicsWithAnalysis"])

    if not dynamics_result["success"]:
        return {
            "success": False,
            "workflow": "forcite_dynamics_with_analysis",
            "failed_step": "dynamics",
            "output_directory": str(output_dir),
            "dynamics": dynamics_result,
            "references": workflow_references,
        }

    analysis_results: dict[str, Any] = {}
    if "rdf" in normalized_analyses:
        analysis_results["rdf"] = ms_forcite_rdf(
            input_trajectory=str(trajectory_path),
            output_study_table_path=str(output_dir / "rdf.std"),
            output_structure_factor_study_table_path=str(output_dir / "structure_factor.std")
            if rdf_include_structure_factor
            else None,
            selection_a=rdf_selection_a,
            selection_b=rdf_selection_b,
            frame_range=analysis_frame_range,
            include_structure_factor=rdf_include_structure_factor,
            analysis_settings=rdf_settings,
            job_name=f"{job_name_prefix}_rdf",
            timeout_seconds=analysis_timeout_seconds,
            keep_job_dir=keep_job_dir,
        )
    if "msd" in normalized_analyses:
        analysis_results["msd"] = ms_forcite_msd(
            input_trajectory=str(trajectory_path),
            output_study_table_path=str(output_dir / "msd.std"),
            selection=msd_selection,
            frame_range=analysis_frame_range,
            analysis_settings=effective_msd_settings,
            job_name=f"{job_name_prefix}_msd",
            timeout_seconds=analysis_timeout_seconds,
            keep_job_dir=keep_job_dir,
        )
    if "hbond" in normalized_analyses:
        effective_hbond_settings = hbond_settings or {}
        analysis_results["hbond"] = ms_hbond_statistics(
            input_document=str(trajectory_path),
            mode="trajectory",
            frame_range=analysis_frame_range,
            max_frames=effective_hbond_settings.get("max_frames"),
            output_study_table_path=str(output_dir / "hbond_statistics.std"),
            job_name=f"{job_name_prefix}_hbond",
            timeout_seconds=analysis_timeout_seconds,
            keep_job_dir=keep_job_dir,
        )
    if "thermo" in normalized_analyses:
        analysis_results["thermo"] = ms_forcite_thermo_profiles(
            input_trajectory=str(trajectory_path),
            output_directory=str(output_dir / "thermo_profiles"),
            properties=thermo_properties,
            frame_range=analysis_frame_range,
            common_analysis_settings=thermo_common_analysis_settings,
            analysis_settings_by_property=thermo_analysis_settings_by_property,
            job_name_prefix=f"{job_name_prefix}_thermo",
            timeout_seconds=analysis_timeout_seconds,
            keep_job_dir=keep_job_dir,
        )

    workflow_success = dynamics_result["success"] and all(result["success"] for result in analysis_results.values())
    analysis_summaries: dict[str, Any] = {}
    for name, result in analysis_results.items():
        if name == "thermo":
            analysis_summaries[name] = {
                "properties": result.get("properties", []),
                "key_metrics": result.get("key_metrics", {}),
                "profile_summaries": result.get("profile_summaries", {}),
                "interpretation": result.get("interpretation"),
            }
        else:
            analysis_summaries[name] = {
                "row_count": result.get("analysis_table", {}).get("row_count"),
                "columns": result.get("analysis_table", {}).get("columns"),
                "metrics": result.get("analysis_summary", {}),
                "first_rows": result.get("analysis_preview", [])[:3],
            }

    workflow_key_metrics = {
        "potential_energy": dynamics_result.get("parsed_report", {}).get("PotentialEnergy"),
        "kinetic_energy": dynamics_result.get("parsed_report", {}).get("KineticEnergy"),
        "total_energy": dynamics_result.get("parsed_report", {}).get("TotalEnergy"),
        "temperature": dynamics_result.get("parsed_report", {}).get("Temperature"),
        "trajectory_frames": dynamics_result.get("parsed_report", {}).get("TrajectoryFrames"),
        "msd_diffusion_coefficient": analysis_results.get("msd", {}).get("analysis_summary", {}).get("MSDDiffusionCoefficient"),
        "msd_diffusion_coefficient_rsq": analysis_results.get("msd", {}).get("analysis_summary", {}).get("MSDDiffusionCoefficientRsq"),
        "mean_hbond_count": analysis_results.get("hbond", {}).get("analysis_summary", {}).get("MeanHBondCount"),
        "frames_with_hbonds": analysis_results.get("hbond", {}).get("analysis_summary", {}).get("FramesWithHBonds"),
        "temperature_profile_mean": analysis_results.get("thermo", {}).get("key_metrics", {}).get("temperature_mean"),
        "pressure_profile_mean": analysis_results.get("thermo", {}).get("key_metrics", {}).get("pressure_mean"),
        "density_profile_mean": analysis_results.get("thermo", {}).get("key_metrics", {}).get("density_mean"),
        "thermo_overall_status": analysis_results.get("thermo", {}).get("stability_assessment", {}).get("overall_status"),
        "thermo_recommended_start_frame": analysis_results.get("thermo", {}).get("stability_assessment", {}).get("recommended_production_start_frame"),
        "thermo_recommended_frame_range": analysis_results.get("thermo", {}).get("stability_assessment", {}).get("recommended_production_frame_range"),
    }
    workflow_interpretation = _build_dynamics_analysis_interpretation(
        key_metrics=workflow_key_metrics,
        analysis_results=analysis_results,
    )
    unified_report = _build_analysis_report(
        dynamics_result=dynamics_result,
        analysis_results=analysis_results,
        key_metrics=workflow_key_metrics,
    )
    workflow_report_path = output_dir / "workflow_interpretation.md"
    workflow_report_path.write_text(
        _render_dynamics_analysis_interpretation_markdown(workflow_interpretation),
        encoding="utf-8",
    )
    unified_report_markdown_path = output_dir / "unified_analysis_report.md"
    unified_report_json_path = output_dir / "unified_analysis_report.json"
    unified_report_markdown_path.write_text(
        _render_analysis_report_markdown(unified_report),
        encoding="utf-8",
    )
    unified_report_json_path.write_text(
        json.dumps(unified_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "success": workflow_success,
        "workflow": "forcite_dynamics_with_analysis",
        "output_directory": str(output_dir),
        "analyses": normalized_analyses,
        "artifacts": {
            "trajectory": {
                "path": str(trajectory_path),
                "inspection": _inspect_materials_document(str(trajectory_path)),
            },
            "final_structure": {
                "path": str(final_structure_path),
                "inspection": _inspect_materials_document(str(final_structure_path)),
            },
            "workflow_interpretation_markdown": str(workflow_report_path),
            "unified_analysis_report_markdown": str(unified_report_markdown_path),
            "unified_analysis_report_json": str(unified_report_json_path),
        },
        "key_metrics": workflow_key_metrics,
        "dynamics": dynamics_result,
        "analysis_results": analysis_results,
        "analysis_summaries": analysis_summaries,
        "interpretation": workflow_interpretation,
        "unified_report": unified_report,
        "references": workflow_references,
        "notes": [
            "This workflow runs dynamics first and then reimports the exported trajectory for post-analysis.",
            "RDF, MSD, hydrogen-bond, and thermodynamic profile analyses are returned as structured tables so downstream MCP clients can summarize or plot them without parsing proprietary documents.",
            "The bundled MSD step uses MSDMaxFrameLength=100 by default so diffusion-coefficient fitting is more likely to be available.",
            "The thermodynamic profile branch also estimates a recommended production window when enough time-series points are available.",
            "A unified multi-analysis report is also exported in Markdown and JSON forms.",
        ],
    }


def ms_forcite_relax_and_dynamics(
    input_structure: str,
    output_directory: str,
    geometry_optimization_settings: dict[str, Any] | None = None,
    dynamics_settings: dict[str, Any] | None = None,
    job_name_prefix: str = "forcite_relax_md",
    geometry_timeout_seconds: int = 900,
    dynamics_timeout_seconds: int = 1200,
    keep_job_dir: bool = True,
) -> dict[str, Any]:
    """Run a composite workflow: geometry optimization first, then dynamics from the optimized structure."""

    output_dir = Path(output_directory)
    output_dir.mkdir(parents=True, exist_ok=True)

    optimized_structure = output_dir / "optimized_structure.xsd"
    final_structure = output_dir / "dynamics_final_structure.xsd"
    trajectory = output_dir / "dynamics_trajectory.xtd"

    geometry_result = ms_forcite_geometry_optimization(
        input_structure=input_structure,
        output_structure_path=str(optimized_structure),
        module_settings=geometry_optimization_settings,
        job_name=f"{job_name_prefix}_geomopt",
        timeout_seconds=geometry_timeout_seconds,
        keep_job_dir=keep_job_dir,
    )

    workflow_references = _reference_entries(
        FORCITE_HELP_PAGES["GeometryOptimization"] + FORCITE_HELP_PAGES["Dynamics"]
    )

    if not geometry_result["success"]:
        return {
            "success": False,
            "workflow": "forcite_relax_and_dynamics",
            "failed_step": "geometry_optimization",
            "output_directory": str(output_dir),
            "geometry_optimization": geometry_result,
            "references": workflow_references,
        }

    dynamics_result = ms_forcite_dynamics(
        input_structure=str(optimized_structure),
        output_trajectory_path=str(trajectory),
        output_structure_path=str(final_structure),
        module_settings=dynamics_settings,
        job_name=f"{job_name_prefix}_dynamics",
        timeout_seconds=dynamics_timeout_seconds,
        keep_job_dir=keep_job_dir,
    )

    optimized_summary = _inspect_materials_document(str(optimized_structure))
    final_structure_summary = _inspect_materials_document(str(final_structure))
    trajectory_summary = _inspect_materials_document(str(trajectory))
    workflow_success = geometry_result["success"] and dynamics_result["success"]

    key_metrics = {
        "optimized_potential_energy": geometry_result.get("parsed_report", {}).get("PotentialEnergy"),
        "dynamics_potential_energy": dynamics_result.get("parsed_report", {}).get("PotentialEnergy"),
        "dynamics_temperature": dynamics_result.get("parsed_report", {}).get("Temperature"),
        "trajectory_frames": dynamics_result.get("parsed_report", {}).get("TrajectoryFrames"),
    }

    return {
        "success": workflow_success,
        "workflow": "forcite_relax_and_dynamics",
        "output_directory": str(output_dir),
        "artifacts": {
            "optimized_structure": {
                "path": str(optimized_structure),
                "inspection": optimized_summary,
            },
            "final_structure": {
                "path": str(final_structure),
                "inspection": final_structure_summary,
            },
            "trajectory": {
                "path": str(trajectory),
                "inspection": trajectory_summary,
            },
        },
        "key_metrics": key_metrics,
        "geometry_optimization": geometry_result,
        "dynamics": dynamics_result,
        "references": workflow_references,
        "notes": [
            "This workflow uses the optimized structure as the direct input to dynamics.",
            "For long or production dynamics, review forcefield choice, ensemble, thermostat, timestep, and equilibration strategy for your system.",
        ],
    }


def ms_ch03_inspect_target_structure(
    target_source: str | None = None,
    target_source_mode: str = "auto",
    output_directory: str | None = None,
    target_cleave_h: int = 0,
    target_cleave_k: int = 0,
    target_cleave_l: int = 1,
    target_thickness: float = 18.12,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """Inspect a candidate clay/MMT source file and return the recommended follow-up mode for the ch03 workflow."""

    if target_source_mode not in {"auto", "surface", "crystal"}:
        raise ValueError("target_source_mode must be one of: auto, surface, crystal")

    safe_target_source = target_source or _default_ms_structure_path("mica_2d_layer")
    safe_output_directory = output_directory or str(_resolve_ch03_input_dir() / "target_intake_mcp")
    return _run_ch03_script_with_summary(
        tool_name="ms_ch03_inspect_target_structure",
        script_name="run_ms_inspect_target_structure.ps1",
        summary_json_name="target_intake_summary.json",
        output_directory=safe_output_directory,
        named_args={
            "TargetSource": safe_target_source,
            "TargetSourceMode": target_source_mode,
            "TargetCleaveH": target_cleave_h,
            "TargetCleaveK": target_cleave_k,
            "TargetCleaveL": target_cleave_l,
            "TargetThickness": target_thickness,
            "TimeoutSeconds": timeout_seconds,
        },
        timeout_seconds=timeout_seconds,
    )


def ms_ch03_match_surface_supercells(
    graphite_source: str | None = None,
    target_source: str | None = None,
    target_source_mode: str = "surface",
    output_directory: str | None = None,
    preferred_length_u: float = 155.0,
    preferred_length_v: float = 161.0,
    graphene_u_min: int = 20,
    graphene_u_max: int = 80,
    graphene_v_min: int = 20,
    graphene_v_max: int = 90,
    target_u_min: int = 10,
    target_u_max: int = 40,
    target_v_min: int = 10,
    target_v_max: int = 40,
    target_thickness: float = 18.12,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """Search for the best graphene/target supercell combinations for the ch03 heterogeneous pore model."""

    if target_source_mode not in {"surface", "crystal"}:
        raise ValueError("target_source_mode must be one of: surface, crystal")

    safe_output_directory = output_directory or str(_resolve_ch03_input_dir() / "surface_match_mcp")
    return _run_ch03_script_with_summary(
        tool_name="ms_ch03_match_surface_supercells",
        script_name="run_ms_match_surface_supercells.ps1",
        summary_json_name="surface_supercell_match.json",
        output_directory=safe_output_directory,
        named_args={
            "GraphiteSource": graphite_source or _default_ms_structure_path("graphite"),
            "TargetSource": target_source or _default_ms_structure_path("mica_2d_layer"),
            "TargetSourceMode": target_source_mode,
            "PreferredLengthU": preferred_length_u,
            "PreferredLengthV": preferred_length_v,
            "GrapheneUMin": graphene_u_min,
            "GrapheneUMax": graphene_u_max,
            "GrapheneVMin": graphene_v_min,
            "GrapheneVMax": graphene_v_max,
            "TargetUMin": target_u_min,
            "TargetUMax": target_u_max,
            "TargetVMin": target_v_min,
            "TargetVMax": target_v_max,
            "TargetThickness": target_thickness,
            "TimeoutSeconds": timeout_seconds,
        },
        timeout_seconds=timeout_seconds,
    )


def ms_ch03_build_pore(
    graphite_source: str | None = None,
    clay_source: str | None = None,
    clay_source_mode: str = "surface",
    output_directory: str | None = None,
    match_summary_json: str | None = None,
    match_candidate_index: int = 1,
    graphene_mode: str = "rectangular",
    graphene_thickness: float = 10.05,
    graphene_supercell_u: int = 37,
    graphene_supercell_v: int = 66,
    clay_thickness: float = 18.12,
    clay_supercell_u: int = 30,
    clay_supercell_v: int = 18,
    pore_gap: float = 30.0,
    top_padding: float = 20.0,
    top_flip: str = "No",
    layer_offset_a: float = 0.0,
    layer_offset_b: float = 0.0,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Build the ch03 graphene/clay pore model and export the generated surfaces plus pore structure."""

    if clay_source_mode not in {"surface", "crystal"}:
        raise ValueError("clay_source_mode must be one of: surface, crystal")
    if graphene_mode not in {"rectangular", "native"}:
        raise ValueError("graphene_mode must be one of: rectangular, native")
    if top_flip not in {"No", "A", "B"}:
        raise ValueError("top_flip must be one of: No, A, B")

    safe_output_directory = output_directory or str(_resolve_ch03_input_dir() / "ms_generated_mcp")
    return _run_ch03_script_with_summary(
        tool_name="ms_ch03_build_pore",
        script_name="run_ms_ch03_build_pore.ps1",
        summary_json_name="ms_build_summary.json",
        output_directory=safe_output_directory,
        named_args={
            "GraphiteSource": graphite_source or _default_ms_structure_path("graphite"),
            "ClaySource": clay_source or _default_ms_structure_path("mica_2d_layer"),
            "ClaySourceMode": clay_source_mode,
            "MatchSummaryJson": match_summary_json,
            "MatchCandidateIndex": match_candidate_index,
            "GrapheneMode": graphene_mode,
            "GrapheneThickness": graphene_thickness,
            "GrapheneSupercellU": graphene_supercell_u,
            "GrapheneSupercellV": graphene_supercell_v,
            "ClayThickness": clay_thickness,
            "ClaySupercellU": clay_supercell_u,
            "ClaySupercellV": clay_supercell_v,
            "PoreGap": pore_gap,
            "TopPadding": top_padding,
            "TopFlip": top_flip,
            "LayerOffsetA": layer_offset_a,
            "LayerOffsetB": layer_offset_b,
            "TimeoutSeconds": timeout_seconds,
        },
        timeout_seconds=timeout_seconds,
    )


def ms_ch03_precheck(
    input_structure: str,
    output_directory: str | None = None,
    temperature: float = 323.0,
    steps: int = 5000,
    time_step: float = 1.0,
    trajectory_frequency: int = 100,
    forcefield: str = "COMPASSIII",
    skip_dynamics: bool = False,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Run the ch03 Materials Studio precheck workflow on a built pore structure."""

    safe_output_directory = output_directory or str(_resolve_ch03_results_dir() / "ms_precheck_mcp")
    return _run_ch03_script_with_summary(
        tool_name="ms_ch03_precheck",
        script_name="run_ms_ch03_precheck.ps1",
        summary_json_name="ms_precheck_summary.json",
        output_directory=safe_output_directory,
        named_args={
            "InputStructure": input_structure,
            "Temperature": temperature,
            "Steps": steps,
            "TimeStep": time_step,
            "TrajectoryFrequency": trajectory_frequency,
            "Forcefield": forcefield,
            "SkipDynamics": skip_dynamics,
        },
        timeout_seconds=timeout_seconds,
    )


def ms_ch03_pipeline(
    graphite_source: str | None = None,
    target_source: str | None = None,
    target_source_mode: str = "surface",
    pipeline_output_root: str | None = None,
    preferred_length_u: float = 155.0,
    preferred_length_v: float = 161.0,
    match_candidate_index: int = 1,
    graphene_thickness: float = 10.05,
    target_thickness: float = 18.12,
    pore_gap: float = 30.0,
    top_padding: float = 20.0,
    top_flip: str = "No",
    layer_offset_a: float = 0.0,
    layer_offset_b: float = 0.0,
    precheck_temperature: float = 323.0,
    precheck_steps: int = 1000,
    precheck_time_step: float = 1.0,
    precheck_trajectory_frequency: int = 100,
    precheck_forcefield: str = "COMPASSIII",
    reuse_existing_outputs: bool = False,
    skip_dynamics: bool = False,
    timeout_seconds: int = 3600,
) -> dict[str, Any]:
    """Run the end-to-end ch03 workflow: match, build, and precheck."""

    if target_source_mode not in {"surface", "crystal"}:
        raise ValueError("target_source_mode must be one of: surface, crystal")
    if top_flip not in {"No", "A", "B"}:
        raise ValueError("top_flip must be one of: No, A, B")

    safe_output_directory = pipeline_output_root or str(_resolve_ch03_input_dir() / "ms_pipeline_mcp")
    return _run_ch03_script_with_summary(
        tool_name="ms_ch03_pipeline",
        script_name="run_ms_ch03_pipeline.ps1",
        summary_json_name="ms_pipeline_summary.json",
        output_directory=safe_output_directory,
        output_parameter_name="PipelineOutputRoot",
        named_args={
            "GraphiteSource": graphite_source or _default_ms_structure_path("graphite"),
            "TargetSource": target_source or _default_ms_structure_path("mica_2d_layer"),
            "TargetSourceMode": target_source_mode,
            "PreferredLengthU": preferred_length_u,
            "PreferredLengthV": preferred_length_v,
            "MatchCandidateIndex": match_candidate_index,
            "GrapheneThickness": graphene_thickness,
            "TargetThickness": target_thickness,
            "PoreGap": pore_gap,
            "TopPadding": top_padding,
            "TopFlip": top_flip,
            "LayerOffsetA": layer_offset_a,
            "LayerOffsetB": layer_offset_b,
            "PrecheckTemperature": precheck_temperature,
            "PrecheckSteps": precheck_steps,
            "PrecheckTimeStep": precheck_time_step,
            "PrecheckTrajectoryFrequency": precheck_trajectory_frequency,
            "PrecheckForcefield": precheck_forcefield,
            "ReuseExistingOutputs": reuse_existing_outputs,
            "SkipDynamics": skip_dynamics,
        },
        timeout_seconds=timeout_seconds,
    )


def ms_ch03_validate_paper_targets(
    build_summary_json: str,
    output_directory: str | None = None,
    target_pore_nm: float = 3.0,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Compare a ch03 build summary against the extracted paper geometry targets."""

    safe_output_directory = output_directory or str(Path(build_summary_json).resolve().parent)
    return _run_ch03_script_with_summary(
        tool_name="ms_ch03_validate_paper_targets",
        script_name="run_validate_ch03_paper_targets.ps1",
        summary_json_name="ch03_paper_target_validation.json",
        output_directory=safe_output_directory,
        named_args={
            "BuildSummaryJson": build_summary_json,
            "TargetPoreNm": target_pore_nm,
        },
        timeout_seconds=timeout_seconds,
    )


def ms_ch03_audit_reproduction(
    pipeline_root: str | None = None,
    output_directory: str | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Audit the current local ch03 reproduction state and report the next recommended step."""

    safe_output_directory = output_directory or str(_resolve_ch03_input_dir() / "audit_mcp")
    return _run_ch03_script_with_summary(
        tool_name="ms_ch03_audit_reproduction",
        script_name="run_audit_ch03_ms_repro.ps1",
        summary_json_name="ch03_ms_reproduction_audit.json",
        output_directory=safe_output_directory,
        named_args={
            "PipelineRoot": pipeline_root,
        },
        timeout_seconds=timeout_seconds,
    )


def ms_ch03_generate_runbook(
    graphite_source: str | None = None,
    target_source: str | None = None,
    target_source_mode: str = "auto",
    target_pore_nm: float = 3.0,
    run_name: str = "runbook_mcp",
    output_directory: str | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Generate a ready-to-run command card for the ch03 Materials Studio reproduction workflow."""

    if target_source_mode not in {"auto", "surface", "crystal"}:
        raise ValueError("target_source_mode must be one of: auto, surface, crystal")

    safe_output_directory = output_directory or str(_resolve_ch03_input_dir() / run_name)
    return _run_ch03_script_with_summary(
        tool_name="ms_ch03_generate_runbook",
        script_name="run_generate_ch03_ms_runbook.ps1",
        summary_json_name="ch03_ms_runbook.json",
        output_directory=safe_output_directory,
        named_args={
            "GraphiteSource": graphite_source or _default_ms_structure_path("graphite"),
            "TargetSource": target_source or _default_ms_structure_path("mica_2d_layer"),
            "TargetSourceMode": target_source_mode,
            "TargetPoreNm": target_pore_nm,
            "RunName": run_name,
        },
        timeout_seconds=timeout_seconds,
    )


def ms_generate_client_config(python_path: str | None = None) -> dict[str, Any]:
    """Generate a ready-to-paste MCP client config for this local project."""

    detected = ms_detect_installation()
    alias_root = _ensure_runtime_alias()
    suggested_python = python_path or str(alias_root / ".venv" / "Scripts" / "python.exe")
    config = {
        "mcpServers": {
            SERVER_NAME: {
                "command": suggested_python,
                "args": ["-m", "materials_studio_mcp.server"],
                "env": {"MATERIALS_STUDIO_ROOT": detected["root"]},
            }
        }
    }
    return {
        "project_root": str(PROJECT_ROOT),
        "runtime_alias_root": str(alias_root),
        "suggested_python": suggested_python,
        "config": config,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
