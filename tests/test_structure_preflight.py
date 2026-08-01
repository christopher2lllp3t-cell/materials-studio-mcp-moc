from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from materials_studio_mcp.structure_preflight import inspect_lammps_data, inspect_xsd_preflight


WORKSPACE = Path(r"D:\分子动力学模拟")


class StructurePreflightTests(unittest.TestCase):
    def test_xsd_periodic_image_nodes_are_not_counted_as_atoms(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsd_images_", dir=WORKSPACE / "tmp") as temporary:
            path = Path(temporary) / "mapped.xsd"
            path.write_text(
                '<XSD><AtomisticTreeRoot><SymmetrySystem Mapping="2">'
                '<Atom3d ID="1" Name="Na1" Components="Na" XYZ="0,0,0" ForcefieldType="na" FormalCharge="1" />'
                '<Atom3d ID="2" Name="Cl1" Components="Cl" XYZ="0.5,0.5,0.5" ForcefieldType="cl" FormalCharge="-1" />'
                '<SpaceGroup ID="3" GroupName="P1" AVector="5,0,0" BVector="0,5,0" CVector="0,0,5">'
                '<ImageMapping ID="4"><Atom3d ID="5" ImageOf="1" Mapping="4" />'
                '<Atom3d ID="6" ImageOf="2" Mapping="4" /></ImageMapping></SpaceGroup>'
                '</SymmetrySystem></AtomisticTreeRoot></XSD>',
                encoding="ascii",
            )
            result = inspect_xsd_preflight(str(path))
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["atom_count"], 2)
            self.assertEqual(result["elements"], {"Na": 1, "Cl": 1})
            self.assertEqual(result["missing_forcefield_type_count"], 0)

    def test_current_xsd_bond_nodes_and_orders_are_audited(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsd_bonds_", dir=WORKSPACE / "tmp") as temporary:
            path = Path(temporary) / "bonds.xsd"
            path.write_text(
                '<XSD><AtomisticTreeRoot>'
                '<Atom3d ID="1" Components="C" XYZ="0,0,0" ForcefieldType="c" Connections="3" />'
                '<Atom3d ID="2" Components="O" XYZ="1.2,0,0" ForcefieldType="o" Connections="3" />'
                '<Bond ID="3" Connects="1,2" Type="Double" />'
                '</AtomisticTreeRoot></XSD>',
                encoding="ascii",
            )
            result = inspect_xsd_preflight(str(path))
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["bond_count"], 1)
            self.assertEqual(result["explicit_bond_node_count"], 1)
            self.assertEqual(result["bond_types"], {"Double": 1})

    @classmethod
    def setUpClass(cls) -> None:
        (WORKSPACE / "tmp").mkdir(parents=True, exist_ok=True)

    def test_real_xsd_is_parsed(self) -> None:
        result = inspect_xsd_preflight(str(WORKSPACE / "ms_output" / "tight_oil_fig1b_step6_density_tighten_box.xsd"))
        self.assertEqual(result["status"], "pass")
        self.assertGreater(result["atom_count"], 100)
        self.assertTrue(result["cell"]["valid"])

    def test_lammps_count_mismatch_is_rejected(self) -> None:
        sample = WORKSPACE / "tmp" / "preflight_bad.data"
        sample.write_text("Bad\n\n2 atoms\n1 atom types\n\nMasses\n\n1 1.0\n\nAtoms # full\n\n1 1 1 0.0 0 0 0\n", encoding="utf-8")
        try:
            result = inspect_lammps_data(str(sample))
            self.assertEqual(result["status"], "fail")
            self.assertTrue(any("2 atoms" in item for item in result["errors"]))
        finally:
            sample.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
