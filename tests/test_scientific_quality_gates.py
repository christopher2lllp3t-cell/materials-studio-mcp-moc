from __future__ import annotations

import unittest
from pathlib import Path

from materials_studio_mcp.structure_preflight import (
    inspect_lammps_data,
    inspect_msi2lmp_inputs,
    inspect_xsd_preflight,
)


ROOT = Path(r"D:\分子动力学模拟\tmp\quality_gate_tests")


class ScientificQualityGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ROOT.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, content: str) -> Path:
        path = ROOT / name
        path.write_text(content, encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_xsd_rejects_invalid_cell_overlap_charge_and_missing_type(self) -> None:
        path = self.write("bad.xsd", '''<Root>
<SpaceGroup AVector="0,0,0" BVector="0,1,0" CVector="0,0,1" GroupName="P1"/>
<Atom3d ID="1" Components="Na" XYZ="0,0,0" FormalCharge="1" ForcefieldType="na"/>
<Atom3d ID="2" Components="Cl" XYZ="0,0,0" FormalCharge="0"/>
</Root>''')
        result = inspect_xsd_preflight(str(path))
        self.assertEqual(result["status"], "fail")
        joined = "\n".join(result["errors"])
        for expected in ("cell vectors", "Minimum atom distance", "net charge", "coverage"):
            self.assertIn(expected, joined)

    def test_xsd_rejects_nan_coordinate(self) -> None:
        path = self.write("nan.xsd", '<Root><Atom3d ID="1" Components="C" XYZ="NaN,0,0" ForcefieldType="c"/></Root>')
        result = inspect_xsd_preflight(str(path))
        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("XYZ" in item for item in result["errors"]))

    def test_lammps_rejects_zero_mass_bad_type_nan_and_charge(self) -> None:
        path = self.write("bad.data", '''Bad

1 atoms
1 atom types

Masses

1 0.0

Atoms # full

1 1 2 1.0 NaN 0 0
''')
        result = inspect_lammps_data(str(path))
        self.assertEqual(result["status"], "fail")
        joined = "\n".join(result["errors"])
        for expected in ("finite and positive", "outside 1..1", "NaN or infinite"):
            self.assertIn(expected, joined)

    def test_lammps_empty_model_is_rejected(self) -> None:
        path = self.write("empty.data", "Empty\n\n0 atoms\n0 atom types\n")
        self.assertEqual(inspect_lammps_data(str(path))["status"], "fail")

    def test_lammps_rejects_illegal_box_and_overlap(self) -> None:
        path = self.write("box.data", '''Bad box

2 atoms
1 atom types
0 0 xlo xhi
0 10 ylo yhi
0 10 zlo zhi

Masses

1 12.0

Atoms # full

1 1 1 0 1 1 1
2 1 1 0 1 1 1
''')
        result = inspect_lammps_data(str(path))
        joined = "\n".join(result["errors"])
        self.assertIn("Invalid x box bounds", joined)
        self.assertIn("Minimum atom distance", joined)

    def test_car_mdf_pair_headers_and_basename_are_checked(self) -> None:
        car = self.write("one.car", "not a car\n")
        mdf = self.write("two.mdf", "not an mdf\n")
        result = inspect_msi2lmp_inputs(str(car), str(mdf), "cvff.frc")
        self.assertEqual(result["status"], "blocked")
        joined = "\n".join(result["errors"])
        self.assertIn("basenames", joined)
        self.assertIn("BIOSYM archive", joined)
        self.assertIn("BIOSYM molecular_data", joined)


if __name__ == "__main__":
    unittest.main()
