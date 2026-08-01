from __future__ import annotations

"""Guarded input generation for the official standalone CASTEP launcher.

This module deliberately stops at generating a hash-bound ``.cell``/``.param``
candidate.  It does not invoke RunCASTEP.bat, acquire a CASTEP license, select a
Gateway, or interpret a calculation result.
"""

from collections import Counter
import hashlib
import json
import math
import re
import shutil
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from .castep_pl import safe_name
from .geology_modeling import inspect_xsd_geometry, sha256_file


STANDALONE_INPUT_SCHEMA_VERSION = 1
STANDALONE_GENERATOR_REVISION = "ms-mcp.standalone-inputs.1.3.0-r1"
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_ELEMENT = re.compile(r"^[A-Z][a-z]?$")
_STANDARD_ELEMENTS = {
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg", "Al", "Si", "P", "S",
    "Cl", "Ar", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn", "Ga",
    "Ge", "As", "Se", "Br", "Kr", "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd",
    "Ag", "Cd", "In", "Sn", "Sb", "Te", "I", "Xe", "Cs", "Ba", "La", "Ce", "Pr", "Nd", "Pm",
    "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu", "Hf", "Ta", "W", "Re", "Os",
    "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi", "Po", "At", "Rn", "Fr", "Ra", "Ac", "Th", "Pa",
    "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm", "Md", "No", "Lr", "Rf", "Db", "Sg",
    "Bh", "Hs", "Mt", "Ds", "Rg", "Cn", "Nh", "Fl", "Mc", "Lv", "Ts", "Og",
}

_CONTEXT_KEYS = {
    "schema_version",
    "task",
    "purpose",
    "input_sha256",
    "electronic_character",
    "magnetism",
    "dispersion",
    "pseudopotentials",
    "xc_functional",
    "energy_cutoff_ev",
    "kpoint_mp_grid",
    "convergence_evidence",
}

_DOCUMENTATION_EVIDENCE = (
    {
        "path": "modules/castep/tskcasteprunstandalone.htm",
        "sha256": "8F1D1A3F1194DFE33960114A014737DD07B05292BB559B74054158895B1E70A7",
        "role": "official Windows standalone launcher and -np syntax",
    },
    {
        "path": "modules/castep/keywords/k_lattice_cart_castep.htm",
        "sha256": "666155157D38BCEF7EFC8CD3216592F4DFE67C788752D405E99F56CB89FBBFFA",
        "role": "LATTICE_CART cell syntax",
    },
    {
        "path": "modules/castep/keywords/k_positions_frac_castep.htm",
        "sha256": "B1DA4D3712DBAABBAB1CDD0897F6EB32DCC7D325F3E34E00EA63942D197906FB",
        "role": "POSITIONS_FRAC cell syntax",
    },
    {
        "path": "modules/castep/keywords/k_kpoints_mp_grid_castep.htm",
        "sha256": "C2DFB33C47736229832B2C131A4180F87CD0A519404B489C62BC4AD49644BA17",
        "role": "explicit Monkhorst-Pack grid syntax",
    },
    {
        "path": "modules/castep/keywords/k_species_pot_castep.htm",
        "sha256": "E17BCD61E777C250CAB85FCCD7AE516A79763C3760023A0C33230D4F292F52CF",
        "role": "standard elements may use the documented default OTFG pseudopotential",
    },
    {
        "path": "modules/castep/keywords/k_task_castep.htm",
        "sha256": "BD19A9B1BA90FC19DA3696FAC0C1760A38673ED606DB1D7DBDAE12B7CDB72723",
        "role": "SinglePoint task keyword",
    },
    {
        "path": "modules/castep/keywords/k_cut_off_energy_castep.htm",
        "sha256": "4428F2341E216AE1935F7263B2F666D9B524635EC6C8A0687397AE16B51653A2",
        "role": "explicit plane-wave cutoff keyword",
    },
    {
        "path": "modules/castep/keywords/k_xc_functional_castep.htm",
        "sha256": "D3D53AA3E9C40128E80FE16EADA760FAA2633EC2DCB405D48EF2D38320965BAB",
        "role": "PBE exchange-correlation functional keyword",
    },
    {
        "path": "modules/castep/keywords/k_spin_polarized_castep.htm",
        "sha256": "0FA7F7A96931563B13F6FF940DC64DA6F1660C9756A4AB225746243394CD5677",
        "role": "non-spin-polarized parameter keyword",
    },
    {
        "path": "modules/castep/keywords/k_fix_occupancy_castep.htm",
        "sha256": "B9E59AC6648E2B1217259EFA6070D9207FA894003D8627E19FF310FFB21219E6",
        "role": "fixed insulating occupancy parameter keyword",
    },
)


def _canonical_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Standalone input contract must contain finite JSON values") from exc
    return hashlib.sha256(encoded).hexdigest().upper()


def _parse_vector(raw: str | None, *, label: str) -> tuple[float, float, float]:
    if not raw:
        raise ValueError(f"{label} is missing")
    try:
        values = tuple(float(item.strip()) for item in raw.split(","))
    except ValueError as exc:
        raise ValueError(f"{label} must contain three finite numeric values") from exc
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        raise ValueError(f"{label} must contain three finite numeric values")
    return values  # type: ignore[return-value]


def _fractional_key(values: tuple[float, float, float]) -> tuple[float, float, float]:
    normalized: list[float] = []
    for value in values:
        wrapped = value - math.floor(value)
        if math.isclose(wrapped, 0.0, abs_tol=1.0e-10) or math.isclose(wrapped, 1.0, abs_tol=1.0e-10):
            wrapped = 0.0
        normalized.append(round(wrapped, 10))
    return tuple(normalized)  # type: ignore[return-value]


def _same_site(left: tuple[float, float, float], right: tuple[float, float, float]) -> bool:
    for a, b in zip(left, right):
        delta = abs(a - b)
        if min(delta, 1.0 - delta) > 1.0e-8:
            return False
    return True


def _normal_element(atom: ET.Element) -> str:
    element = (atom.get("Components") or atom.get("Name") or "").split(",")[0].strip()
    if _ELEMENT.fullmatch(element) is None or element not in _STANDARD_ELEMENTS:
        raise ValueError(
            "Standalone OTFG input supports only standard periodic-table element symbols; "
            f"atom {atom.get('ID', '<unknown>')} has {element!r}"
        )
    return element


def _expanded_periodic_sites(path: Path) -> dict[str, Any]:
    """Return a deterministic P1 site list after validating a 3D XSD cell.

    This intentionally follows the existing strict symmetry rules used by
    :func:`inspect_xsd_geometry`: explicit SpaceGroup operators are mandatory
    outside P1, source display images are not atoms, and colliding asymmetric
    sites fail closed.
    """

    root = ET.parse(path).getroot()
    independent_atoms = [
        item for item in root.iter()
        if item.tag.rsplit("}", 1)[-1] == "Atom3d" and item.get("ImageOf") is None
    ]
    if not independent_atoms:
        raise ValueError("XSD contains no independent Atom3d entries")
    space_groups = [item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "SpaceGroup"]
    if not space_groups:
        raise ValueError("Standalone CASTEP candidates require a 3D XSD SpaceGroup cell")
    sg = space_groups[0]
    vectors = {
        name: _parse_vector(sg.get(name), label=f"SpaceGroup {name}")
        for name in ("AVector", "BVector", "CVector")
    }
    raw_operators = sg.get("Operators")
    operator_chunks = raw_operators.split(":") if raw_operators else []
    if not operator_chunks:
        group_name = (sg.get("GroupName") or "").replace(" ", "").upper()
        if sg.get("ITNumber") == "1" or group_name == "P1":
            operator_chunks = ["1,0,0,0,0,1,0,0,0,0,1,0"]
        else:
            raise ValueError("Non-P1 XSD requires explicit SpaceGroup Operators")
    operators: list[tuple[float, ...]] = []
    for raw in operator_chunks:
        try:
            transform = tuple(float(item.strip()) for item in raw.split(","))
        except ValueError as exc:
            raise ValueError("SpaceGroup Operators contain a nonnumeric transform") from exc
        if len(transform) != 12 or not all(math.isfinite(value) for value in transform):
            raise ValueError("Each SpaceGroup operator must be a finite 3x4 transform")
        operators.append(transform)

    def transform(xyz: tuple[float, float, float], matrix: tuple[float, ...]) -> tuple[float, float, float]:
        return _fractional_key((
            matrix[0] * xyz[0] + matrix[1] * xyz[1] + matrix[2] * xyz[2] + matrix[3],
            matrix[4] * xyz[0] + matrix[5] * xyz[1] + matrix[6] * xyz[2] + matrix[7],
            matrix[8] * xyz[0] + matrix[9] * xyz[1] + matrix[10] * xyz[2] + matrix[11],
        ))

    sites: list[dict[str, Any]] = []
    for atom in independent_atoms:
        atom_id = atom.get("ID")
        if not atom_id:
            raise ValueError("XSD contains an independent atom without ID")
        element = _normal_element(atom)
        xyz = _parse_vector(atom.get("XYZ"), label=f"Atom {atom_id} XYZ")
        orbit: list[tuple[float, float, float]] = []
        for operator in operators:
            candidate = transform(xyz, operator)
            if not any(_same_site(candidate, existing) for existing in orbit):
                orbit.append(candidate)
        for fractional in orbit:
            collision = next((item for item in sites if _same_site(fractional, item["fractional"])), None)
            if collision is not None:
                raise ValueError(
                    f"Distinct asymmetric atoms {collision['asymmetric_atom_id']} and {atom_id} "
                    f"occupy the same periodic site {fractional}"
                )
            sites.append({
                "element": element,
                "fractional": list(fractional),
                "asymmetric_atom_id": atom_id,
            })
    sites.sort(key=lambda item: (item["element"], *item["fractional"], item["asymmetric_atom_id"]))
    return {
        "cell_vectors": {name: list(vector) for name, vector in vectors.items()},
        "sites": sites,
        "symmetry_operator_count": len(operators),
        "elements": dict(sorted(Counter(item["element"] for item in sites).items())),
    }


def _decision(context: dict[str, Any], name: str) -> tuple[Any, str]:
    raw = context.get(name)
    if not isinstance(raw, dict) or set(raw) != {"value", "source"}:
        raise ValueError(f"standalone_context.{name} must contain exactly value and source")
    source = raw["source"]
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"standalone_context.{name}.source must be a non-empty string")
    return raw["value"], source.strip()


def _validate_context(
    context: dict[str, Any], *, input_sha256: str, cores: int
) -> tuple[dict[str, Any], dict[str, str]]:
    if not isinstance(context, dict):
        raise ValueError("standalone_context must be an object")
    unknown = set(context) - _CONTEXT_KEYS
    missing = _CONTEXT_KEYS - set(context)
    if unknown or missing:
        raise ValueError(
            "standalone_context must use the closed schema; "
            f"unknown={sorted(unknown)}, missing={sorted(missing)}"
        )
    if context["schema_version"] != STANDALONE_INPUT_SCHEMA_VERSION:
        raise ValueError(f"standalone_context.schema_version must be {STANDALONE_INPUT_SCHEMA_VERSION}")
    if context["task"] != "single_point":
        raise ValueError("standalone_context.task must be single_point")
    if context["purpose"] not in {"preliminary", "research"}:
        raise ValueError("standalone_context.purpose must be preliminary or research")
    if not isinstance(context["input_sha256"], str) or context["input_sha256"].upper() != input_sha256:
        raise ValueError("standalone_context.input_sha256 must exactly match input_sha256")

    electronic, electronic_source = _decision(context, "electronic_character")
    magnetism, magnetism_source = _decision(context, "magnetism")
    dispersion, dispersion_source = _decision(context, "dispersion")
    pseudopotentials, pseudopotentials_source = _decision(context, "pseudopotentials")
    xc, xc_source = _decision(context, "xc_functional")
    cutoff, cutoff_source = _decision(context, "energy_cutoff_ev")
    kgrid, kgrid_source = _decision(context, "kpoint_mp_grid")
    if electronic != "insulator":
        raise ValueError("R1 standalone inputs support electronic_character=insulator only")
    if magnetism != "nonmagnetic":
        raise ValueError("R1 standalone inputs support magnetism=nonmagnetic only")
    if dispersion != "off":
        raise ValueError("R1 standalone inputs support dispersion=off only")
    if pseudopotentials != "default_otfg":
        raise ValueError("R1 standalone inputs support pseudopotentials=default_otfg only")
    if xc != "PBE":
        raise ValueError("R1 standalone inputs support xc_functional=PBE only")
    if isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)):
        raise ValueError("standalone_context.energy_cutoff_ev.value must be numeric")
    cutoff_ev = float(cutoff)
    if not math.isfinite(cutoff_ev) or not 0.0 < cutoff_ev <= 99999.0:
        raise ValueError("standalone_context.energy_cutoff_ev.value must be finite and between 0 and 99999")
    if not isinstance(kgrid, list) or len(kgrid) != 3:
        raise ValueError("standalone_context.kpoint_mp_grid.value must contain exactly three integers")
    if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 128 for value in kgrid):
        raise ValueError("standalone_context.kpoint_mp_grid.value entries must be integers from 1 to 128")
    convergence = context["convergence_evidence"]
    if not isinstance(convergence, list) or any(not isinstance(item, str) or not item.strip() for item in convergence):
        raise ValueError("standalone_context.convergence_evidence must be a list of non-empty evidence strings")
    if context["purpose"] == "research" and not convergence:
        raise ValueError("Research standalone candidates require explicit convergence_evidence")
    settings = {
        "task": "SinglePoint",
        "xc_functional": "PBE",
        "energy_cutoff_ev": cutoff_ev,
        "kpoint_mp_grid": list(kgrid),
        "spin_polarized": False,
        "fix_occupancy": True,
        "species_pot": "omitted_standard_elements_use_default_otfg",
        "cores": cores,
    }
    origins = {
        "electronic_character": electronic_source,
        "magnetism": magnetism_source,
        "dispersion": dispersion_source,
        "pseudopotentials": pseudopotentials_source,
        "xc_functional": xc_source,
        "energy_cutoff_ev": cutoff_source,
        "kpoint_mp_grid": kgrid_source,
    }
    return settings, origins


def _render_cell(*, cell_vectors: dict[str, list[float]], sites: list[dict[str, Any]], kpoint_mp_grid: list[int]) -> str:
    def vector_line(values: list[float]) -> str:
        return "  " + "  ".join(f"{value:.10f}" for value in values)

    lines = ["%BLOCK LATTICE_CART"]
    lines.extend(vector_line(cell_vectors[name]) for name in ("AVector", "BVector", "CVector"))
    lines.extend(["%ENDBLOCK LATTICE_CART", "", "%BLOCK POSITIONS_FRAC"])
    for site in sites:
        lines.append(
            f"  {site['element']:<2}  "
            + "  ".join(f"{value:.10f}" for value in site["fractional"])
        )
    lines.extend([
        "%ENDBLOCK POSITIONS_FRAC",
        "",
        f"KPOINTS_MP_GRID {kpoint_mp_grid[0]} {kpoint_mp_grid[1]} {kpoint_mp_grid[2]}",
        "",
    ])
    return "\n".join(lines)


def _render_param(*, energy_cutoff_ev: float) -> str:
    return "\n".join((
        "TASK : SinglePoint",
        "XC_FUNCTIONAL : PBE",
        f"CUT_OFF_ENERGY : {energy_cutoff_ev:.10f} eV",
        "SPIN_POLARIZED : FALSE",
        "FIX_OCCUPANCY : TRUE",
        "",
    ))


def _inspect_source(input_xsd: Path, input_sha256: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not input_xsd.is_file():
        raise FileNotFoundError(f"XSD not found: {input_xsd}")
    if input_xsd.suffix.lower() != ".xsd":
        raise ValueError("input_xsd must use the .xsd suffix")
    if _SHA256.fullmatch(input_sha256 or "") is None:
        raise ValueError("input_sha256 must contain exactly 64 hexadecimal characters")
    actual = sha256_file(input_xsd)
    if actual != input_sha256.upper():
        raise ValueError(f"Input XSD SHA-256 mismatch: expected {input_sha256.upper()}, got {actual}")
    geometry = inspect_xsd_geometry(input_xsd)
    if geometry["periodic_dimension"] != 3:
        raise ValueError("Standalone CASTEP candidates require a three-dimensionally periodic XSD")
    expanded = _expanded_periodic_sites(input_xsd)
    if len(expanded["sites"]) != geometry["atom_count"]:
        raise RuntimeError("Standalone symmetry expansion disagrees with the verified XSD unit-cell atom count")
    if expanded["elements"] != geometry["elements"]:
        raise RuntimeError("Standalone symmetry expansion disagrees with the verified XSD element inventory")
    source = {
        "path": str(input_xsd),
        "sha256": actual,
        "runtime_atom_count": geometry["atom_count"],
        "asymmetric_atom_count": geometry["asymmetric_atom_count"],
        "symmetry_operator_count": geometry["symmetry_operator_count"],
        "elements": geometry["elements"],
        "cell_volume_A3": geometry["cell_volume_A3"],
    }
    return source, expanded


def prepare_castep_standalone_inputs(
    *,
    input_xsd: Path,
    input_sha256: str,
    output_directory: Path,
    calculation_name: str,
    standalone_context: dict[str, Any],
    cores: int = 4,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Create a non-executable, self-contained standalone CASTEP input candidate."""

    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    if isinstance(cores, bool) or not isinstance(cores, int) or not 1 <= cores <= 4:
        raise ValueError("R1 standalone candidates require cores to be an integer from 1 to 4")
    source, expanded = _inspect_source(input_xsd, input_sha256)
    settings, setting_origins = _validate_context(
        standalone_context, input_sha256=source["sha256"], cores=cores
    )
    seedname = safe_name(f"{calculation_name}_sp_{cores}c", max_length=48)
    cell_text = _render_cell(
        cell_vectors=expanded["cell_vectors"],
        sites=expanded["sites"],
        kpoint_mp_grid=settings["kpoint_mp_grid"],
    )
    param_text = _render_param(energy_cutoff_ev=settings["energy_cutoff_ev"])
    contract = {
        "schema_version": STANDALONE_INPUT_SCHEMA_VERSION,
        "tool": "ms_prepare_castep_standalone_inputs",
        "generator_revision": STANDALONE_GENERATOR_REVISION,
        "source": source,
        "standalone_context": standalone_context,
        "settings": settings,
        "setting_origins": setting_origins,
        "rendered_structure": {
            "cell_vectors_A": expanded["cell_vectors"],
            "sites": expanded["sites"],
        },
        "resource_policy": {"cores": cores, "local_core_ceiling": 4, "max_parallel_jobs": 1},
        "execution_allowed": False,
        "execution_blockers": [{
            "code": "CASTEP_STANDALONE_RUNNER_UNQUALIFIED",
            "detail": "This R1 tool only writes reviewed input candidates; it never starts RunCASTEP.bat.",
        }, {
            "code": "CASTEP_RESULT_PARSING_UNVERIFIED",
            "detail": "No standalone CASTEP result parser is released by this candidate package.",
        }],
        "documentation_evidence": list(_DOCUMENTATION_EVIDENCE),
    }
    contract_sha256 = _canonical_sha256(contract)
    preview = {
        "status": "dry_run" if dry_run else "prepared",
        "automatic_submission": False,
        "gateway_selected": False,
        "execution_started": False,
        "execution_allowed": False,
        "writes_performed": False,
        "source": source,
        "output_directory": str(output_directory),
        "seedname": seedname,
        "settings": settings,
        "contract_sha256": contract_sha256,
        "planned_outputs": [
            str(output_directory / f"{seedname}.cell"),
            str(output_directory / f"{seedname}.param"),
            str(output_directory / "input_source.xsd"),
            str(output_directory / "standalone_input_contract.json"),
            str(output_directory / "standalone_input_manifest.json"),
        ],
        "next_actions": [
            "Review the exact .cell and .param candidate against the scientific context and convergence evidence.",
            "Do not invoke RunCASTEP.bat until a separate controlled runner and result parser are qualified.",
        ],
    }
    if dry_run:
        return preview

    output_directory.mkdir(parents=True, exist_ok=False)
    try:
        # Bind the source twice: before parsing and after all candidate content is rendered.
        # This prevents a source replacement between validation and package creation.
        if sha256_file(input_xsd) != source["sha256"]:
            raise RuntimeError("Input XSD changed during standalone input preparation")
        source_copy = output_directory / "input_source.xsd"
        shutil.copy2(input_xsd, source_copy)
        if sha256_file(source_copy) != source["sha256"]:
            raise RuntimeError("Copied XSD changed after source validation")
        cell_path = output_directory / f"{seedname}.cell"
        param_path = output_directory / f"{seedname}.param"
        contract_path = output_directory / "standalone_input_contract.json"
        cell_path.write_text(cell_text, encoding="ascii", newline="\n")
        param_path.write_text(param_text, encoding="ascii", newline="\n")
        contract_path.write_text(
            json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        if _canonical_sha256(json.loads(contract_path.read_text(encoding="utf-8"))) != contract_sha256:
            raise RuntimeError("Written standalone input contract hash does not match the rendered contract")
        manifest = {
            **preview,
            "status": "prepared",
            "writes_performed": True,
            "input_source_copy": {
                "path": str(source_copy),
                "sha256": sha256_file(source_copy),
            },
            "cell": {"path": str(cell_path), "sha256": sha256_file(cell_path)},
            "param": {"path": str(param_path), "sha256": sha256_file(param_path)},
            "contract": {"path": str(contract_path), "sha256": sha256_file(contract_path), "canonical_sha256": contract_sha256},
        }
        manifest_path = output_directory / "standalone_input_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        return {
            **manifest,
            "manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
        }
    except Exception:
        shutil.rmtree(output_directory, ignore_errors=True)
        raise
