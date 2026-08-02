from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


API_VERSION = "1.0"
REQUEST_SCHEMA_VERSION = "1.0"
RESULT_SCHEMA_VERSION = "1.0"

Risk = Literal["R0", "R1", "R2", "R3"]
Lifecycle = Literal["stable", "deprecated"]


@dataclass(frozen=True)
class PublicTool:
    name: str
    risk: Risk
    lifecycle: Lifecycle = "stable"
    replacement: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


# This is the sole source of truth for MCP-visible tools.  Registration tests
# fail if a decorator is added or removed without updating this reviewed list.
PUBLIC_TOOLS: tuple[PublicTool, ...] = (
    PublicTool("ms_detect_installation", "R0"),
    PublicTool("md_pipeline_get_config", "R0"),
    PublicTool("md_pipeline_health_check", "R0"),
    PublicTool("md_architecture_compliance_audit", "R0"),
    PublicTool("md_prepare_production_confirmation", "R1"),
    PublicTool("md_task_submit", "R2"),
    PublicTool("md_task_query", "R0"),
    PublicTool("md_task_cancel", "R2"),
    PublicTool("md_task_retry", "R2"),
    PublicTool("md_project_initialize", "R1"),
    PublicTool("md_project_get", "R0"),
    PublicTool("md_project_update_specification", "R1"),
    PublicTool("md_project_register_artifact", "R1"),
    PublicTool("md_project_validate", "R0"),
    PublicTool("md_project_set_quality_gate", "R1"),
    PublicTool("md_project_transition", "R1"),
    PublicTool("md_structure_preflight", "R0"),
    PublicTool("md_msi2lmp_preflight", "R0"),
    PublicTool("md_convert_to_lammps_checked", "R2"),
    PublicTool("md_export_xsd_to_car_mdf_checked", "R2"),
    PublicTool("ms_search_local_help", "R0"),
    PublicTool("ms_read_local_help_page", "R0"),
    PublicTool("ms_find_code_examples", "R0"),
    PublicTool("ms_list_example_documents", "R0"),
    PublicTool("ms_task_catalog", "R0"),
    PublicTool("ms_recommend_workflow", "R0"),
    PublicTool("ms_execute_task_request", "R0"),
    PublicTool("ms_scan_workspace", "R0"),
    PublicTool("ms_inspect_document", "R0"),
    PublicTool("ms_prepare_castep_pl_package", "R1"),
    PublicTool("ms_prepare_castep_standalone_inputs", "R1"),
    PublicTool("ms_castep_fixed_profile_preflight", "R0"),
    PublicTool("ms_castep_preflight_checked", "R2"),
    PublicTool("ms_castep_gateway_readiness", "R0"),
    PublicTool("ms_geology_import_crystal_parent", "R2"),
    PublicTool("ms_geology_build_periodic_slab_cell", "R2"),
    PublicTool("ms_pack_periodic_aqueous_nacl", "R2"),
    PublicTool("md_build_clayff_spce_nacl_lammps", "R2"),
    PublicTool("ms_forcite_calculation_checked", "R3"),
    PublicTool("md_g01_qualification_vertical", "R3"),
    PublicTool("md_scientific_gate_audit", "R2"),
    PublicTool("ms_geology_build_supercell", "R2"),
    PublicTool("ms_geology_enumerate_surface_terminations", "R2"),
    PublicTool("ms_geology_apply_substitutions", "R2"),
    PublicTool("ms_geology_place_counterions", "R2"),
    PublicTool("ms_geology_apply_hydroxylation_ledger", "R2"),
    PublicTool("ms_geology_assess_nanopore_contract", "R0"),
    PublicTool("ms_moc_get_status", "R0"),
    PublicTool("ms_moc_open_document", "R2"),
    PublicTool("ms_list_analysis_targets", "R2"),
)

INTERNAL_TOOL_PROFILES: dict[str, tuple[str, ...]] = {
    "ch03_reproduction": (
        "ms_ch03_inspect_target_structure",
        "ms_ch03_match_surface_supercells",
        "ms_ch03_build_pore",
        "ms_ch03_precheck",
        "ms_ch03_pipeline",
        "ms_ch03_validate_paper_targets",
        "ms_ch03_audit_reproduction",
        "ms_ch03_generate_runbook",
    ),
    "developer": ("ms_run_materialsscript",),
}


def public_tool_names() -> frozenset[str]:
    return frozenset(item.name for item in PUBLIC_TOOLS)


def api_catalog() -> dict[str, object]:
    return {
        "api_version": API_VERSION,
        "request_schema_version": REQUEST_SCHEMA_VERSION,
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "tools": [item.as_dict() for item in PUBLIC_TOOLS],
        "internal_profiles": {
            name: {"visible": False, "tools": list(tools)}
            for name, tools in INTERNAL_TOOL_PROFILES.items()
        },
    }
