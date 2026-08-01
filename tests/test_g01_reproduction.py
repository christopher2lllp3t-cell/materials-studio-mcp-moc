from __future__ import annotations

import unittest

from materials_studio_mcp.g01_reproduction import _fatal_lammps_markers, _parse_run0


class G01ReproductionTests(unittest.TestCase):
    def test_parses_last_finite_run_zero_thermo_row(self) -> None:
        log = """
Step Atoms PotEng E_bond E_angle
0 3 9.5898501 0.1 0.2
Loop time of 1e-06 on 1 procs for 0 steps with 3 atoms
"""
        row = _parse_run0(log)
        self.assertEqual(row["Atoms"], 3.0)
        self.assertAlmostEqual(row["PotEng"], 9.5898501)

    def test_fatal_marker_detection_does_not_match_benign_words(self) -> None:
        log = "WARNING: Proc sub-domain size < neighbor skin, could lead to lost atoms\nfinite energy reported."
        self.assertEqual(_fatal_lammps_markers(log), [])

    def test_fatal_marker_detection_matches_errors_and_nonfinite_numbers(self) -> None:
        log = "ERROR on proc 0: bad state\nLost atoms: original 3 current 2\nPotEng nan\n"
        self.assertEqual(_fatal_lammps_markers(log), ["ERROR", "Lost atoms", "non-finite numeric value"])


if __name__ == "__main__":
    unittest.main()
