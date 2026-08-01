from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from materials_studio_mcp.structure_preflight import inspect_lammps_data
from materials_studio_mcp.triclinic import (
    cartesian_to_fractional,
    fractional_to_cartesian,
    image_unwrap,
    minimum_image_displacement,
    wrap_cartesian,
)
from materials_studio_mcp.vmd_validation import _SAFE_G05_TCL, _parse_g05_markers, _project_g05_triclinic_dump


FIXTURE = Path(r"D:\分子动力学模拟\07_mcp_materials_studio\golden_science\G05_triclinic_cell\g05.data")


class G05TriclinicTests(unittest.TestCase):
    def test_restricted_triclinic_box_and_minimum_image(self) -> None:
        result = inspect_lammps_data(str(FIXTURE))
        self.assertEqual(result["status"], "pass", result["errors"])
        box = result["cell"]
        self.assertEqual(box["kind"], "restricted_triclinic")
        self.assertEqual(box["tilt"], {"xy": 2.0, "xz": 1.0, "yz": 1.5})
        self.assertEqual(box["cell_rows"], [[10.0, 0.0, 0.0], [2.0, 9.0, 0.0], [1.0, 1.5, 8.0]])
        self.assertAlmostEqual(box["volume"], 720.0, places=12)
        self.assertAlmostEqual(result["minimum_atom_distance_angstrom"], 0.741080292545956, places=12)

    def test_fractional_round_trip_and_image_semantics(self) -> None:
        box = inspect_lammps_data(str(FIXTURE))["cell"]
        fractional = [0.37, 0.21, 0.83]
        cartesian = fractional_to_cartesian(fractional, box)
        for actual, expected in zip(cartesian_to_fractional(cartesian, box), fractional):
            self.assertAlmostEqual(actual, expected, places=12)
        wrapped = [0.26, 0.21, 0.16]
        unwrapped = image_unwrap(wrapped, [1, 1, 1], box)
        self.assertEqual(unwrapped, [13.26, 10.71, 8.16])
        recovered = wrap_cartesian(unwrapped, box, ("x", "y", "z"))
        for actual, expected in zip(recovered["wrapped"], wrapped):
            self.assertAlmostEqual(actual, expected, places=12)
        self.assertEqual(recovered["image"], [1, 1, 1])

    def test_periodic_axes_are_independent(self) -> None:
        result = inspect_lammps_data(str(FIXTURE), periodic_axes=("x",))
        displacement = minimum_image_displacement(
            [12.74, 10.29, 7.84], [0.26, 0.21, 0.16], result["cell"], ("x",)
        )
        self.assertGreater(math.sqrt(sum(value * value for value in displacement)), 10.0)
        self.assertEqual(result["periodic_axes"], ["x"])

    def test_non_full_atom_style_fails_closed(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8").replace("Atoms # full", "Atoms # charge")
        temporary = FIXTURE.with_name("g05_invalid_style.data")
        try:
            temporary.write_text(text, encoding="utf-8")
            result = inspect_lammps_data(str(temporary))
            self.assertEqual(result["status"], "fail")
            self.assertTrue(any("Atoms # full" in error for error in result["errors"]))
        finally:
            temporary.unlink(missing_ok=True)

    def test_g05_vmd_tcl_and_marker_parser_are_strict(self) -> None:
        self.assertNotIn("exec ", _SAFE_G05_TCL)
        self.assertIn("molinfo $molid set frame 0", _SAFE_G05_TCL)
        markers = """MCP_G05_WRAPPED_ATOMS 2
MCP_G05_WRAPPED_FRAMES 3
MCP_G05_WRAPPED_CELL 10 9.219544 8.200610 78.169884 82.995796 77.471191
MCP_G05_WRAPPED_COORDSUM 13 10.5 8
MCP_G05_UNWRAPPED_ATOMS 2
MCP_G05_UNWRAPPED_FRAMES 3
MCP_G05_UNWRAPPED_CELL 10 9.219544 8.200610 78.169884 82.995796 77.471191
MCP_G05_UNWRAPPED_COORDSUM 26 21 16
MCP_G05_VALIDATION_COMPLETE 1
"""
        parsed = _parse_g05_markers(markers)
        self.assertTrue(parsed["complete"])
        self.assertEqual(parsed["wrapped_atoms"], 2)
        self.assertEqual(parsed["unwrapped_coordsum"], [26.0, 21.0, 16.0])

    def test_g05_vmd_projection_preserves_tilt_and_distinguishes_coordinates(self) -> None:
        dump = """ITEM: TIMESTEP
0
ITEM: NUMBER OF ATOMS
2
ITEM: BOX BOUNDS xy xz yz pp pp pp
0 13 2
0 10.5 1
0 8 1.5
ITEM: ATOMS id mol type q x y z xu yu zu ix iy iz
1 1 1 0 12.74 10.29 7.84 12.74 10.29 7.84 0 0 0
2 2 1 0 0.26 0.21 0.16 13.26 10.71 8.16 1 1 1
"""
        with tempfile.TemporaryDirectory(dir=r"E:\ms_mcp\ms_mcp_jobs") as temp:
            root = Path(temp)
            source = root / "source.dump"
            source.write_text(dump, encoding="utf-8")
            wrapped = _project_g05_triclinic_dump(source, root / "wrapped.dump", unwrapped=False)
            unwrapped = _project_g05_triclinic_dump(source, root / "unwrapped.dump", unwrapped=True)
        self.assertEqual(wrapped["cell_rows_angstrom"], [[10.0, 0.0, 0.0], [2.0, 9.0, 0.0], [1.0, 1.5, 8.0]])
        self.assertEqual(wrapped["tilt_angstrom"], [2.0, 1.0, 1.5])
        self.assertEqual(wrapped["boundary_flags"], ["pp", "pp", "pp"])
        self.assertNotEqual(wrapped["first_coordinate_sum_angstrom"], unwrapped["first_coordinate_sum_angstrom"])


if __name__ == "__main__":
    unittest.main()
