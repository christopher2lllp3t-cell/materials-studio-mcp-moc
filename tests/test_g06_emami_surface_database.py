from __future__ import annotations

import hashlib
import json
import math
import unittest
from collections import Counter
from pathlib import Path


G06 = Path(
    r"D:\分子动力学模拟\07_mcp_materials_studio\production_gates\G06_quartz_nanopore"
)
EVIDENCE = G06 / "emami_surface_database_evidence.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _artifact_records(data: dict) -> list[dict]:
    records: list[dict] = []
    records.extend(data["publication"]["supporting_information"])
    records.append(data["database"]["description_docx"])
    records.append(data["database"]["parameter_file"])
    records.extend(data["database"]["reviewed_models"])
    records.extend(data["visual_review"])
    return records


def _parse_lammps_atoms(path: Path) -> list[tuple[int, int, float, float, float, float]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    atoms_start = lines.index("Atoms") + 1
    bonds_start = lines.index("Bonds")
    atoms: list[tuple[int, int, float, float, float, float]] = []
    for line in lines[atoms_start:bonds_start]:
        fields = line.split()
        if len(fields) < 7 or not fields[0].isdigit():
            continue
        atoms.append(
            (
                int(fields[0]),
                int(fields[2]),
                float(fields[3]),
                float(fields[4]),
                float(fields[5]),
                float(fields[6]),
            )
        )
    return atoms


def _parse_lammps_bonds(path: Path) -> list[tuple[int, int, int]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    bonds_start = lines.index("Bonds") + 1
    angles_start = lines.index("Angles")
    bonds: list[tuple[int, int, int]] = []
    for line in lines[bonds_start:angles_start]:
        fields = line.split()
        if len(fields) < 4 or not fields[0].isdigit():
            continue
        bonds.append((int(fields[1]), int(fields[2]), int(fields[3])))
    return bonds


class G06EmamiSurfaceDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    def test_all_recorded_artifact_hashes_match(self) -> None:
        for record in _artifact_records(self.data):
            path = G06 / record["file" if "file" in record else "render"]
            self.assertTrue(path.is_file(), path)
            self.assertEqual(_sha256(path), record["sha256"], path)
            if "bytes" in record:
                self.assertEqual(path.stat().st_size, record["bytes"], path)

    def test_q2_reference_model_charge_and_silanol_ledger(self) -> None:
        model = next(
            item
            for item in self.data["database"]["reviewed_models"]
            if item["name"] == "silica_Q2_9_4OH_0pct_ion"
        )
        atoms = _parse_lammps_atoms(G06 / model["file"])
        self.assertEqual(len(atoms), 1848)
        self.assertEqual(Counter(atom[1] for atom in atoms), Counter({1: 896, 2: 504, 3: 224, 4: 224}))
        self.assertTrue(math.isclose(sum(atom[2] for atom in atoms), 0.0, abs_tol=1.0e-9))
        by_id = {atom[0]: atom for atom in atoms}
        oh_bonds = [bond for bond in _parse_lammps_bonds(G06 / model["file"]) if bond[0] == 3]
        self.assertEqual(len(oh_bonds), 224)
        self.assertEqual(
            Counter(tuple(sorted((by_id[a][1], by_id[b][1]))) for _, a, b in oh_bonds),
            Counter({(3, 4): 224}),
        )
        self.assertEqual(Counter(a for _, a, _ in oh_bonds).most_common(1)[0][1], 1)
        self.assertEqual(Counter(b for _, _, b in oh_bonds).most_common(1)[0][1], 1)
        h_z = [atom[5] for atom in atoms if atom[1] == 4]
        self.assertEqual(sum(z < 9.0 for z in h_z), 112)
        self.assertEqual(sum(z > 9.0 for z in h_z), 112)
        area_nm2 = model["box_A"][0] * model["box_A"][1] / 100.0
        self.assertTrue(math.isclose(area_nm2, model["lateral_area_nm2"], abs_tol=1.0e-12))
        self.assertTrue(
            math.isclose(
                model["silanol_groups_per_face"] / area_nm2,
                model["silanol_density_OH_nm2"],
                abs_tol=1.0e-12,
            )
        )
        self.assertTrue(math.isclose(model["silanol_density_OH_nm2"], 9.4, abs_tol=0.02))

    def test_database_cannot_release_frozen_quartz_101_target(self) -> None:
        assessment = self.data["release_assessment"]
        self.assertEqual(
            self.data["gate_status_after_audit"],
            "blocked_target_101_surface_chemistry_not_resolved",
        )
        self.assertFalse(self.data["construction_released"])
        self.assertFalse(assessment["target_alpha_quartz_101_coordinate_source_found"])
        self.assertFalse(assessment["target_alpha_quartz_101_termination_proved"])
        self.assertFalse(assessment["target_alpha_quartz_101_hydroxylation_proved"])
        self.assertFalse(assessment["fixed_region_rule_proved"])
        self.assertFalse(assessment["accessible_width_proved"])
        self.assertFalse(assessment["production_released"])
        self.assertTrue(all(not item["target_101_surface"] for item in self.data["database"]["reviewed_models"]))

    def test_parameter_family_is_not_silently_relabelled_clayff(self) -> None:
        parameter = self.data["database"]["parameter_file"]
        self.assertEqual(parameter["force_field_family"], "PCFF-INTERFACE")
        self.assertTrue(parameter["not_clayff"])


if __name__ == "__main__":
    unittest.main()
