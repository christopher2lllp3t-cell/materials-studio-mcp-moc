from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from materials_studio_mcp.pipeline_config import load_pipeline_config
from materials_studio_mcp.vmd_validation import _SAFE_TCL, _parse_vmd_markers, validate_vmd_text_trajectory


class VmdValidationTests(unittest.TestCase):
    def test_tcl_is_constant_and_has_no_path_interpolation_surface(self) -> None:
        malicious = 'evil\"; exec calc; #.lammpstrj'
        self.assertNotIn(malicious, _SAFE_TCL)
        self.assertNotIn("exec ", _SAFE_TCL)
        self.assertIn("trajectory.wrapped.lammpstrj", _SAFE_TCL)
        self.assertIn("trajectory.unwrapped_for_vmd.lammpstrj", _SAFE_TCL)

    def test_vmd_marker_parser_is_strict(self) -> None:
        text = """MCP_VMD_WRAPPED_ATOMS 3
MCP_VMD_WRAPPED_FRAMES 11
MCP_VMD_WRAPPED_CELL 20 20 20 90 90 90
MCP_VMD_WRAPPED_FINITE_COORD_ROWS 3
MCP_VMD_UNWRAPPED_ATOMS 3
MCP_VMD_UNWRAPPED_FRAMES 11
MCP_VMD_UNWRAPPED_CELL 20 20 20 90 90 90
MCP_VMD_UNWRAPPED_FINITE_COORD_ROWS 3
MCP_VMD_VALIDATION_COMPLETE 1
"""
        parsed = _parse_vmd_markers(text)
        self.assertTrue(parsed["complete"])
        self.assertEqual(parsed["wrapped_frames"], 11)
        self.assertEqual(parsed["unwrapped_cell"], [20.0, 20.0, 20.0, 90.0, 90.0, 90.0])

    def test_bad_data_blocks_vmd_launch(self) -> None:
        with tempfile.TemporaryDirectory(dir=r"E:\ms_mcp\ms_mcp_jobs") as temp:
            root = Path(temp)
            data = root / "bad.data"
            dump = root / "evil];exec calc;#.lammpstrj"
            data.write_text("bad\n\n0 atoms\n", encoding="utf-8")
            dump.write_text("untrusted", encoding="utf-8")
            with patch("materials_studio_mcp.vmd_validation.subprocess.run") as run:
                result = validate_vmd_text_trajectory(str(data), str(dump), str(root / "output"))
            self.assertEqual(result["status"], "blocked_static_preflight")
            run.assert_not_called()

    def test_malicious_source_filename_never_reaches_tcl_or_command(self) -> None:
        markers = """MCP_VMD_WRAPPED_ATOMS 3
MCP_VMD_WRAPPED_FRAMES 1
MCP_VMD_WRAPPED_CELL 20 20 20 90 90 90
MCP_VMD_WRAPPED_FINITE_COORD_ROWS 3
MCP_VMD_UNWRAPPED_ATOMS 3
MCP_VMD_UNWRAPPED_FRAMES 1
MCP_VMD_UNWRAPPED_CELL 20 20 20 90 90 90
MCP_VMD_UNWRAPPED_FINITE_COORD_ROWS 3
MCP_VMD_VALIDATION_COMPLETE 1
"""
        dump_text = """ITEM: TIMESTEP
0
ITEM: NUMBER OF ATOMS
3
ITEM: BOX BOUNDS ff ff ff
-10 10
-10 10
-10 10
ITEM: ATOMS id mol type q x y z xu yu zu ix iy iz
1 1 1 -0.834 0 0 0 0 0 0 0 0 0
2 1 2 0.417 0.9572 0 0 0.9572 0 0 0 0 0
3 1 2 0.417 -0.239987 0.926627 0 -0.239987 0.926627 0 0 0 0
"""
        workspace = Path(load_pipeline_config()["policy"]["workspace_roots"][0])
        data = workspace / "07_mcp_materials_studio" / "golden_science" / "G01_water" / "g01_reference.data"
        with tempfile.TemporaryDirectory(dir=r"E:\ms_mcp\ms_mcp_jobs") as temp:
            root = Path(temp)
            malicious_name = "evil];exec_calc;#.lammpstrj"
            dump = root / malicious_name
            dump.write_text(dump_text, encoding="utf-8")
            output = root / "output"
            fake = SimpleNamespace(returncode=0, stdout=markers, stderr="")
            with patch("materials_studio_mcp.vmd_validation.subprocess.run", return_value=fake) as run:
                result = validate_vmd_text_trajectory(str(data), str(dump), str(output), expected_frames=1)
            self.assertEqual(result["status"], "pass")
            self.assertNotIn(malicious_name, (output / "validate_trajectory.tcl").read_text(encoding="utf-8"))
            command = run.call_args.args[0]
            self.assertNotIn(str(dump), command)
            self.assertEqual(command[-1], "validate_trajectory.tcl")

    def test_output_path_outside_workspace_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=r"E:\ms_mcp\ms_mcp_jobs") as temp:
            root = Path(temp)
            data, dump = root / "bad.data", root / "bad.dump"
            data.write_text("bad", encoding="utf-8")
            dump.write_text("bad", encoding="utf-8")
            with self.assertRaises(PermissionError):
                validate_vmd_text_trajectory(str(data), str(dump), r"C:\Windows\Temp\evil-output")


if __name__ == "__main__":
    unittest.main()
