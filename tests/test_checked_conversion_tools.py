from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from materials_studio_mcp import server
from materials_studio_mcp.project_manager import initialize_project


WORKSPACE_ROOT = Path(r"D:\分子动力学模拟")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class CheckedConversionToolTests(unittest.TestCase):
    def test_checked_export_is_confirmed_registered_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="checked_export_", dir=WORKSPACE_ROOT / "tmp") as directory:
            project = Path(
                initialize_project("checked-export", "Checked export", projects_root=directory)["project_directory"]
            )
            source = project / "model" / "source.xsd"
            source.write_bytes(b"xsd model")
            parameters = {
                "project_directory": str(project),
                "input_xsd": str(source),
                "input_sha256": digest(source),
                "output_slot": "reviewed_pair",
                "idempotency_key": "checked-export-key",
                "timeout_seconds": 30,
            }
            confirmation = server.md_prepare_production_confirmation(
                "md_export_xsd_to_car_mdf_checked", parameters
            )

            def fake_export(input_xsd, output_car, output_mdf, timeout_seconds):
                Path(output_car).write_bytes(b"car output")
                Path(output_mdf).write_bytes(b"mdf output")
                return {"success": True, "job_id": "export-job"}

            with patch.object(server, "md_export_xsd_to_car_mdf", side_effect=fake_export) as export:
                first = server.md_export_xsd_to_car_mdf_checked(
                    **parameters, confirmation_token=confirmation["confirmation_token"],
                    dry_run=False,
                )
                second = server.md_export_xsd_to_car_mdf_checked(**parameters, dry_run=False)

            self.assertTrue(first["ok"])
            self.assertEqual(first["data"]["status"], "export_pass")
            self.assertFalse(first["replayed"])
            self.assertTrue(second["ok"])
            self.assertTrue(second["replayed"])
            self.assertEqual(len(first["data"]["artifact_registrations"]), 2)
            export.assert_called_once()

    def test_checked_conversion_binds_pair_and_forcefield_hashes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="checked_convert_", dir=WORKSPACE_ROOT / "tmp") as directory:
            project = Path(
                initialize_project("checked-convert", "Checked convert", projects_root=directory)["project_directory"]
            )
            car = project / "conversion" / "model.car"
            mdf = project / "conversion" / "model.mdf"
            forcefield = project / "forcefield" / "reviewed.frc"
            car.write_bytes(b"car model")
            mdf.write_bytes(b"mdf model")
            forcefield.write_bytes(b"forcefield")
            parameters = {
                "project_directory": str(project),
                "car_path": str(car),
                "car_sha256": digest(car),
                "mdf_path": str(mdf),
                "mdf_sha256": digest(mdf),
                "forcefield_file": str(forcefield),
                "forcefield_sha256": digest(forcefield),
                "forcefield_class": "I",
                "output_slot": "reviewed_data",
                "idempotency_key": "checked-convert-key",
                "timeout_seconds": 30,
            }
            confirmation = server.md_prepare_production_confirmation(
                "md_convert_to_lammps_checked", parameters
            )
            preflight = {
                "status": "pass",
                "forcefield_file": str(forcefield),
                "car_path": str(car),
                "mdf_path": str(mdf),
                "car_detected_record_count": 1,
            }

            def fake_convert(car_path, mdf_path, output_data_path, forcefield_file, forcefield_class, timeout_seconds):
                Path(output_data_path).write_bytes(b"LAMMPS data")
                return {
                    "success": True,
                    "stage": "complete",
                    "data_preflight": {"status": "pass", "header_counts": {"atoms": 1}},
                }

            with patch.object(server, "inspect_msi2lmp_inputs", return_value=preflight), patch.object(
                server, "convert_car_mdf", side_effect=fake_convert
            ) as convert:
                first = server.md_convert_to_lammps_checked(
                    **parameters, confirmation_token=confirmation["confirmation_token"],
                    dry_run=False,
                )
                second = server.md_convert_to_lammps_checked(**parameters, dry_run=False)

            self.assertTrue(first["ok"])
            self.assertEqual(first["data"]["status"], "conversion_pass")
            self.assertFalse(first["data"]["production_released"])
            self.assertTrue(first["data"]["validation"]["atom_count_matches"])
            self.assertEqual(first["data"]["forcefield_sha256"], digest(forcefield))
            self.assertTrue(second["replayed"])
            convert.assert_called_once()

    def test_checked_export_rejects_hash_mismatch_before_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="checked_hash_", dir=WORKSPACE_ROOT / "tmp") as directory:
            project = Path(
                initialize_project("checked-hash", "Checked hash", projects_root=directory)["project_directory"]
            )
            source = project / "model" / "source.xsd"
            source.write_bytes(b"xsd model")
            with patch.object(server, "md_export_xsd_to_car_mdf") as export:
                result = server.md_export_xsd_to_car_mdf_checked(
                    str(project), str(source), "0" * 64, "pair", "hash-key"
                )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "invalid_request")
            export.assert_not_called()

    def test_conversion_failure_surfaces_missing_forcefield_types(self) -> None:
        error = server._checked_conversion_failure({
            "stage": "msi2lmp",
            "diagnostics": {
                "missing_mass_types": ["C_3"],
                "inconsistent_connectivity_warning_count": 2,
                "recommendation": "Select a compatible reviewed parameter file.",
            },
        })
        self.assertIsInstance(error, ValueError)
        self.assertIn("C_3", str(error))
        self.assertIn("connectivity warnings: 2", str(error))


if __name__ == "__main__":
    unittest.main()
