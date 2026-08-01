from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from materials_studio_mcp.lammps_validation import (
    FIXED_VALIDATION_SEED,
    _FATAL_PATTERNS,
    _parameter_and_topology_gate,
    _phase_rows,
    validate_lammps_short_chain,
)


class LammpsValidationTests(unittest.TestCase):
    def test_phase_parser_reads_only_finite_rows_inside_markers(self) -> None:
        log = """outside
MCP_PHASE_MD_BEGIN
Step Atoms Temp PotEng KinEng TotEng
0 3 50 -1.0 0.5 -0.5
10 3 49 -0.9 0.4 -0.5
MCP_PHASE_MD_END
Step Atoms Temp PotEng KinEng TotEng
20 3 1 100 100 200
"""
        rows = _phase_rows(log, "MCP_PHASE_MD_BEGIN", "MCP_PHASE_MD_END")
        self.assertEqual([row["Step"] for row in rows], [0.0, 10.0])

    def test_fatal_log_patterns_cover_required_instability_modes(self) -> None:
        samples = {
            "lost_atoms": "ERROR: Lost atoms: original 3 current 2",
            "non_numeric": "Step Temp PotEng\n10 nan inf",
            "bond_atoms_missing": "Bond atoms missing on proc 0",
            "angle_atoms_missing": "Angle atoms missing on proc 0",
        }
        for name, sample in samples.items():
            with self.subTest(name=name):
                self.assertIsNotNone(_FATAL_PATTERNS[name].search(sample))

    def test_lost_atoms_warning_is_not_a_fatal_marker(self) -> None:
        warning = "WARNING: Proc sub-domain size < neighbor skin, could lead to lost atoms"
        self.assertIsNone(_FATAL_PATTERNS["lost_atoms"].search(warning))

    def test_class2_cross_terms_do_not_pollute_angle_coefficients(self) -> None:
        with tempfile.TemporaryDirectory(dir=r"E:\ms_mcp\ms_mcp_jobs") as temp:
            candidate = Path(temp) / "class2.data"
            candidate.write_text("""fixture

3 atoms
2 bonds
1 angles
2 atom types
1 bond types
1 angle types

Masses

1 15.9994
2 1.00797

Pair Coeffs

1 0.274 3.608
2 0.013 1.098

Bond Coeffs

1 0.97 563.28 -1428.22 1902.12

Angle Coeffs

1 103.7 49.84 -11.6 -8.0

BondBond Coeffs

1 -9.5 0.97 0.97

BondAngle Coeffs

1 22.35 22.35 0.97 0.97

Atoms # full

1 1 1 -0.7982 0 0 0
2 1 2 0.3991 1 0 0
3 1 2 0.3991 0 1 0

Bonds

1 1 1 2
2 1 1 3

Angles

1 1 2 1 3
""", encoding="ascii")
            result = _parameter_and_topology_gate(candidate)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["coverage"]["angle"]["found"], 1)

    def test_static_failure_prevents_subprocess_execution(self) -> None:
        with tempfile.TemporaryDirectory(dir=r"E:\ms_mcp\ms_mcp_jobs") as temp:
            root = Path(temp)
            bad = root / "bad.data"
            bad.write_text("bad\n\n0 atoms\n", encoding="utf-8")
            output = root / "must_not_run"
            with patch("materials_studio_mcp.lammps_validation.subprocess.run") as run:
                result = validate_lammps_short_chain(str(bad), str(output))
            self.assertEqual(result["status"], "blocked_static_preflight")
            self.assertFalse(result["executed"])
            run.assert_not_called()
            self.assertFalse(output.exists())

    def test_protocol_limits_and_fixed_seed_fail_closed(self) -> None:
        for kwargs in (
            {"md_steps": 501},
            {"timestep_fs": 2.0},
            {"temperature_k": 1000.0},
            {"seed": FIXED_VALIDATION_SEED + 1},
            {"timeout_seconds": 61},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                validate_lammps_short_chain("unused.data", "unused-output", **kwargs)

    def test_missing_forcefield_coefficients_block_execution(self) -> None:
        with tempfile.TemporaryDirectory(dir=r"E:\ms_mcp\ms_mcp_jobs") as temp:
            root = Path(temp)
            candidate = root / "missing-pair.data"
            candidate.write_text("""fixture

1 atoms
0 bonds
0 angles
0 dihedrals
0 impropers
1 atom types
0 bond types
0 angle types
0 dihedral types
0 improper types
0 10 xlo xhi
0 10 ylo yhi
0 10 zlo zhi

Masses

1 1.0

Atoms # full

1 1 1 0.0 5 5 5
""", encoding="utf-8")
            with patch("materials_studio_mcp.lammps_validation.subprocess.run") as run:
                result = validate_lammps_short_chain(str(candidate), str(root / "output"))
            self.assertEqual(result["status"], "blocked_static_preflight")
            self.assertEqual(result["parameter_and_topology_gate"]["status"], "fail")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
