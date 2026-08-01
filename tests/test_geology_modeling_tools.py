from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from materials_studio_mcp import geology_modeling as geology
from materials_studio_mcp import periodic_packing as packing
from materials_studio_mcp import server
from materials_studio_mcp.public_registry import public_tool_names


def write_xsd(
    path: Path,
    *,
    atoms: list[tuple[str, str, tuple[float, float, float], str]],
    vectors: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
    bonds: list[tuple[str, str, str]] | None = None,
    periodic_dimension: int = 3,
) -> None:
    atom_lines = [
        f'<Atom3d ID="{atom_id}" Name="{element}{atom_id}" Components="{element}" XYZ="{x},{y},{z}" FormalCharge="{charge}" />'
        for atom_id, element, (x, y, z), charge in atoms
    ]
    bond_lines = [
        f'<Bond3d ID="{bond_id}" Connects="{connects}" />' for bond_id, connects, _ in (bonds or [])
    ]
    a, b, c = vectors
    group_tag = "SpaceGroup" if periodic_dimension == 3 else "PlaneGroup"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<XSD>\n"
        + "\n".join(atom_lines + bond_lines)
        + f'\n<{group_tag} GroupName="P1" ITNumber="1" '
          f'Operators="1,0,0,0,0,1,0,0,0,0,1,0" AVector="{a[0]},{a[1]},{a[2]}" '
          f'BVector="{b[0]},{b[1]},{b[2]}" CVector="{c[0]},{c[1]},{c[2]}" />\n</XSD>\n',
        encoding="utf-8",
    )


class GeologyModelingTests(unittest.TestCase):
    def test_repeat_and_surface_contracts_fail_closed(self) -> None:
        for bad in (True, 0, -1, 65, 1.5):
            with self.assertRaises(ValueError):
                geology.validate_repeats(bad, 1, 1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, r"\(0,0,0\)"):
            geology.validate_surface_parameters(0, 0, 0, 20.0, [0.0], 2)
        with self.assertRaisesRegex(ValueError, "unique"):
            geology.validate_surface_parameters(1, 0, 1, 20.0, [0.1, 0.1], 2)
        with self.assertRaisesRegex(ValueError, r"\[0,1\)"):
            geology.validate_surface_parameters(1, 0, 1, 20.0, [1.0], 2)

    def test_scripts_use_only_verified_local_ms_api_and_typed_literals(self) -> None:
        crystal_import = geology.build_crystal_parent_import_script()
        self.assertIn('Documents->Import("{{input.structure}}")', crystal_import)
        self.assertIn('$doc->Export("{{output.structure}}")', crystal_import)
        supercell = geology.build_supercell_script((2, 3, 4))
        self.assertIn("$doc->BuildSuperCell(2, 3, 4);", supercell)
        self.assertNotIn("Tools->Symmetry->BuildSuperCell", supercell)
        surface_supercell = geology.build_supercell_script((4, 4, 1), periodic_dimension=2)
        self.assertIn("$doc->BuildSuperCell(4, 4);", surface_supercell)
        with self.assertRaisesRegex(ValueError, "repeat_c=1"):
            geology.build_supercell_script((4, 4, 2), periodic_dimension=2)
        surface = geology.build_surface_enumeration_script((1, 0, 1), 20.0, [0.0, 0.25])
        self.assertIn("Tools->SurfaceBuilder->CleaveSurface", surface)
        self.assertIn("MillerIndex(H => 1, K => 0, L => 1)", surface)
        self.assertIn('$cleaver->SetThickness(20, "Angstrom");', surface)
        self.assertEqual(surface.count("$cleaver->Cleave;"), 2)
        mesh = geology.validate_surface_mesh_vectors((1, 0, 1), [2, 1, -2], [0, 2, 0])
        self.assertEqual(mesh, ((2, 1, -2), (0, 2, 0)))
        conventional = geology.build_surface_enumeration_script(
            (1, 0, 1), 18.0, [0.0691551350555], *mesh
        )
        self.assertIn("Point(X => 2, Y => 1, Z => -2)", conventional)
        self.assertIn("Point(X => 0, Y => 2, Z => 0)", conventional)
        with self.assertRaisesRegex(ValueError, "not in the requested Miller plane"):
            geology.validate_surface_mesh_vectors((1, 0, 1), [1, 0, 0], [0, 1, 0])
        with self.assertRaisesRegex(ValueError, "colinear"):
            geology.validate_surface_mesh_vectors((1, 0, 1), [1, 0, -1], [2, 0, -2])

    def test_crystal_parent_request_and_postvalidation_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "quartz.cif"
            source.write_text("data_quartz\n", encoding="ascii")
            output = root / "quartz.xsd"
            write_xsd(
                output,
                atoms=[
                    ("1", "Si", (0.1, 0.1, 0.1), "0"),
                    ("2", "Si", (0.4, 0.4, 0.4), "0"),
                    ("3", "Si", (0.7, 0.7, 0.7), "0"),
                    ("4", "O", (0.1, 0.4, 0.7), "0"),
                    ("5", "O", (0.2, 0.5, 0.8), "0"),
                    ("6", "O", (0.3, 0.6, 0.9), "0"),
                    ("7", "O", (0.4, 0.7, 0.1), "0"),
                    ("8", "O", (0.5, 0.8, 0.2), "0"),
                    ("9", "O", (0.6, 0.9, 0.3), "0"),
                ],
                vectors=((4.913, 0, 0), (-2.4565, 4.255, 0), (0, 0, 5.404)),
            )
            request = geology.validate_crystal_parent_request(source, {"Si": 3, "O": 6}, 20)
            self.assertEqual(request["expected_atom_count"], 9)
            result = geology.validate_crystal_parent_import_result(
                output, request["expected_elements"], request["max_atoms"]
            )
            self.assertEqual(result["status"], "crystal_parent_import_pass")
            with self.assertRaisesRegex(RuntimeError, "element inventory"):
                geology.validate_crystal_parent_import_result(output, {"Si": 2, "O": 7}, 20)
            with self.assertRaisesRegex(ValueError, "CIF or XSD"):
                geology.validate_crystal_parent_request(root / "quartz.exe", {"Si": 3, "O": 6}, 20)

    def test_supercell_postvalidation_checks_atoms_composition_charge_bonds_and_cell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.xsd"
            output = root / "output.xsd"
            write_xsd(
                source,
                atoms=[("1", "Na", (0.1, 0.1, 0.1), "1"), ("2", "Cl", (0.6, 0.6, 0.6), "-1")],
                vectors=((10, 0, 0), (0, 11, 0), (0, 0, 12)),
                bonds=[("3", "1,2", "1")],
            )
            write_xsd(
                output,
                atoms=[
                    ("1", "Na", (0.05, 0.1, 0.1), "1"), ("2", "Cl", (0.30, 0.6, 0.6), "-1"),
                    ("3", "Na", (0.55, 0.1, 0.1), "1"), ("4", "Cl", (0.80, 0.6, 0.6), "-1"),
                ],
                vectors=((20, 0, 0), (0, 11, 0), (0, 0, 12)),
                bonds=[("5", "1,2", "1"), ("6", "3,4", "1")],
            )
            model = geology.inspect_xsd_geometry(source)
            result = geology.validate_supercell_result(model, output, (2, 1, 1), 10)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["expected_atom_count"], 4)
            output.write_text(output.read_text(encoding="utf-8").replace("0,11,0", "0,12,0"), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "BVector"):
                geology.validate_supercell_result(model, output, (2, 1, 1), 10)

    def test_symmetry_image_mappings_expand_to_unique_unit_cell_sites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "symmetry.xsd"
            source.write_text(
                "<XSD>\n"
                '<Atom3d ID="1" Components="Na" FormalCharge="1" />\n'
                '<ImageMapping Element="1,0,0,0.5,0,1,0,0.5,0,0,1,0">'
                '<Atom3d ID="2" ImageOf="1" /></ImageMapping>\n'
                '<ImageMapping Element="1,0,0,0.5,0,1,0,0.5,0,0,1,1">'
                '<Atom3d ID="3" ImageOf="1" /></ImageMapping>\n'
                '<SpaceGroup ITNumber="2" '
                'Operators="1,0,0,0,0,1,0,0,0,0,1,0:1,0,0,0.5,0,1,0,0.5,0,0,1,0" '
                'AVector="10,0,0" BVector="0,10,0" CVector="0,0,10" />\n'
                "</XSD>\n",
                encoding="utf-8",
            )
            model = geology.inspect_xsd_geometry(source)
            self.assertEqual(model["asymmetric_atom_count"], 1)
            self.assertEqual(model["atom_count"], 2)
            self.assertEqual(model["elements"], {"Na": 2})
            self.assertEqual(model["formal_charge_e"], 2.0)

    def test_plane_group_keeps_surface_normal_nonperiodic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            surface = Path(directory) / "surface.xsd"
            surface.write_text(
                "<XSD>\n"
                '<Atom3d ID="1" Components="Si" XYZ="0.25,0.25,0" />\n'
                '<Atom3d ID="2" Components="Si" XYZ="0.25,0.25,1" />\n'
                '<PlaneGroup GroupName="p 1" ITNumber="1" '
                'Operators="1,0,0,0,0,1,0,0,0,0,1,0" '
                'AVector="10,0,0" BVector="0,10,0" CVector="0,0,1" />\n'
                "</XSD>\n",
                encoding="utf-8",
            )
            model = geology.inspect_xsd_geometry(surface)
            self.assertEqual(model["periodic_dimension"], 2)
            self.assertEqual(model["atom_count"], 2)

    def test_input_hash_is_mandatory_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.xsd"
            path.write_bytes(b"model")
            expected = hashlib.sha256(b"model").hexdigest()
            self.assertEqual(geology.validate_input_hash(path, expected), expected.upper())
            with self.assertRaisesRegex(ValueError, "mismatch"):
                geology.validate_input_hash(path, "0" * 64)

    def test_public_registry_and_confirmation_allowlist_include_geology_tools(self) -> None:
        names = public_tool_names()
        self.assertIn("ms_geology_import_crystal_parent", names)
        self.assertIn("ms_geology_build_periodic_slab_cell", names)
        self.assertIn("ms_pack_periodic_aqueous_nacl", names)
        self.assertIn("md_build_clayff_spce_nacl_lammps", names)
        self.assertIn("ms_geology_build_supercell", names)
        self.assertIn("ms_geology_enumerate_surface_terminations", names)
        self.assertIn("ms_geology_apply_substitutions", names)
        self.assertIn("ms_geology_place_counterions", names)
        self.assertIn("ms_geology_apply_hydroxylation_ledger", names)
        self.assertIn("ms_geology_assess_nanopore_contract", names)
        issued = server.md_prepare_production_confirmation(
            "ms_geology_build_supercell", {"project_directory": "x"}, ttl_seconds=60
        )
        self.assertTrue(issued["single_use"])

    def test_periodic_aqueous_nacl_packmol_contract_and_distance_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cell = Path(directory) / "cell.xsd"
            write_xsd(
                cell,
                atoms=[("1", "Si", (0.0, 0.0, 0.0), "0"), ("2", "O", (0.0, 0.0, 0.2), "0")],
                vectors=((10, 0, 0), (0, 10, 0), (0, 0, 10)),
            )
            frame = packing.periodic_orthorhombic_frame(cell)
            self.assertAlmostEqual(frame["framework_normal_span_angstrom"], 2.0)
            request = packing.validate_aqueous_nacl_request(
                frame,
                water_count=1,
                sodium_count=1,
                chloride_count=1,
                packmol_tolerance_angstrom=1.5,
                normal_boundary_clearance_angstrom=0.5,
                random_seed=123,
                max_total_atoms=7,
                required_final_formal_charge_e=0.0,
            )
            self.assertEqual(request["expected_total_atoms"], 7)
            self.assertEqual(request["expected_total_bonds"], 2)
            text = packing.packmol_input_text(
                lengths=frame["lengths_angstrom"],
                region=request["packing_region_angstrom"],
                tolerance=request["packmol_tolerance_angstrom"],
                seed=request["random_seed"],
                water_count=1,
                sodium_count=1,
                chloride_count=1,
            )
            self.assertIn("pbc 0.0 0.0 0.0 10 10 10", text)
            self.assertIn("fixed 0.0 0.0 0.0", text)
            records = [
                {"element": "Si", "local_xyz": [0.0, 0.0, 0.0]},
                {"element": "O", "local_xyz": [0.0, 0.0, 2.0]},
                {"element": "O", "local_xyz": [5.0, 5.0, 5.0]},
                {"element": "H", "local_xyz": [6.0, 5.0, 5.0]},
                {"element": "H", "local_xyz": [4.667, 5.943, 5.0]},
                {"element": "Na", "local_xyz": [8.0, 8.0, 8.0]},
                {"element": "Cl", "local_xyz": [3.0, 8.0, 8.0]},
            ]
            audit = packing.audit_packed_xyz(records, frame, request)
            self.assertEqual(audit["molecule_counts"]["spce_water"], 1)
            self.assertGreaterEqual(audit["minimum_inter_molecular_distance_angstrom"], 1.5)
            ledger = packing.packed_fluid_tsv(records, frame, request)
            self.assertEqual(len(ledger.splitlines()), 6)
            script = packing.build_packed_fluid_import_script()
            self.assertIn("FromFractionalPosition", script)
            self.assertIn("CreateBond", script)

    def test_periodic_aqueous_nacl_contract_rejects_charge_and_nonorthogonal_cell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cell = Path(directory) / "cell.xsd"
            write_xsd(
                cell,
                atoms=[("1", "Si", (0.0, 0.0, 0.0), "0")],
                vectors=((10, 0, 0), (1, 10, 0), (0, 0, 10)),
            )
            with self.assertRaisesRegex(ValueError, "orthorhombic"):
                packing.periodic_orthorhombic_frame(cell)

            write_xsd(
                cell,
                atoms=[("1", "Si", (0.0, 0.0, 0.0), "0")],
                vectors=((10, 0, 0), (0, 10, 0), (0, 0, 10)),
            )
            frame = packing.periodic_orthorhombic_frame(cell)
            with self.assertRaisesRegex(ValueError, "formal charge"):
                packing.validate_aqueous_nacl_request(
                    frame,
                    water_count=1,
                    sodium_count=1,
                    chloride_count=0,
                    packmol_tolerance_angstrom=1.5,
                    normal_boundary_clearance_angstrom=0.5,
                    random_seed=123,
                    max_total_atoms=10,
                    required_final_formal_charge_e=0.0,
                )

    def test_periodic_slab_cell_script_span_and_postvalidation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            surface = root / "surface.xsd"
            output = root / "cell.xsd"
            write_xsd(
                surface,
                atoms=[("1", "Si", (0.1, 0.1, -1.0), "0"), ("2", "O", (0.2, 0.2, 1.0), "0")],
                vectors=((10, 0, 0), (0, 12, 0), (0, 0, 1)),
                periodic_dimension=2,
            )
            write_xsd(
                output,
                atoms=[("1", "Si", (0.1, 0.1, 0.2), "0"), ("2", "O", (0.2, 0.2, 0.4), "0")],
                vectors=((10, 0, 0), (0, 12, 0), (0, 0, 10)),
                periodic_dimension=3,
            )
            self.assertAlmostEqual(geology.surface_normal_span_angstrom(surface), 2.0)
            script = geology.build_periodic_slab_cell_script(8.0)
            self.assertIn("VacuumThickness => 8", script)
            result = geology.validate_periodic_slab_cell_result(
                geology.inspect_xsd_geometry(surface), output, 10.0, 1.0e-6
            )
            self.assertEqual(result["status"], "periodic_slab_cell_pass")

    def test_crystal_parent_mcp_adapter_imports_once_and_registers_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model").mkdir()
            (root / "reports").mkdir()
            source = root / "quartz.cif"
            source.write_text("data_quartz\n", encoding="ascii")
            source_hash = geology.sha256_file(source)

            def fake_job(**kwargs):
                destination = Path(kwargs["output_files"]["structure"]["destination_path"])
                write_xsd(
                    destination,
                    atoms=[
                        ("1", "Si", (0.1, 0.1, 0.1), "0"),
                        ("2", "O", (0.4, 0.4, 0.4), "0"),
                        ("3", "O", (0.7, 0.7, 0.7), "0"),
                    ],
                    vectors=((5, 0, 0), (0, 5, 0), (0, 0, 5)),
                )
                return {
                    "success": True,
                    "job_id": "job",
                    "audit_path": "audit",
                    "run_mat_script_exit_code": 0,
                    "timed_out": False,
                }

            with patch.object(server, "get_project", return_value={"project_directory": str(root)}), patch.object(
                server, "resolve_workspace_path", return_value=source
            ), patch.object(server.confirmation_manager, "consume") as consume, patch.object(
                server, "_run_materialsscript_job", side_effect=fake_job
            ) as execute, patch.object(
                server, "register_artifact", side_effect=lambda *args, **kwargs: {"status": "registered"}
            ), patch.object(
                server, "run_idempotent", side_effect=lambda project, key, tool, parameters, implementation: (implementation(), False)
            ):
                result = server.ms_geology_import_crystal_parent(
                    str(root), str(source), source_hash, {"Si": 1, "O": 2}, "quartz_parent", 10,
                    "parent-key", confirmation_token="token", timeout_seconds=30, dry_run=False,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["status"], "crystal_parent_import_pass")
            self.assertTrue((root / "model" / "parents" / "quartz_parent.xsd").is_file())
            self.assertTrue((root / "reports" / "quartz_parent.crystal_parent.receipt.json").is_file())
            consume.assert_called_once()
            execute.assert_called_once()

    def test_crystal_parent_source_allows_only_workspace_or_installed_structure_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            structures = root / "share" / "Structures"
            structures.mkdir(parents=True)
            allowed = structures / "quartz.xsd"
            allowed.write_text("<XSD/>\n", encoding="ascii")
            denied = root / "etc" / "secret.xsd"
            denied.parent.mkdir()
            denied.write_text("<XSD/>\n", encoding="ascii")
            with patch.object(server, "resolve_workspace_path", side_effect=PermissionError("outside")), patch.object(
                server, "_materials_studio_paths", return_value={"root": str(root)}
            ):
                self.assertEqual(server._resolve_crystal_parent_source(str(allowed)), allowed.resolve())
                with self.assertRaisesRegex(PermissionError, "outside"):
                    server._resolve_crystal_parent_source(str(denied))

    def test_substitution_ledger_script_and_postvalidation_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.xsd"
            output = root / "output.xsd"
            write_xsd(
                source,
                atoms=[("1", "Si", (0.1, 0.1, 0.1), "4"), ("2", "O", (0.6, 0.6, 0.6), "-2")],
                vectors=((10, 0, 0), (0, 10, 0), (0, 0, 10)),
            )
            substitutions = [{
                "atom_index": 0, "atom_name": "Si1", "expected_fractional_xyz": [0.1, 0.1, 0.1],
                "from_element": "Si", "to_element": "Al",
                "from_formal_charge_e": 4, "to_formal_charge_e": 3,
            }]
            request = geology.validate_substitution_ledger(source, substitutions)
            script = geology.build_substitution_script(request["substitutions"])
            self.assertIn('$atom->ElementSymbol = "Al";', script)
            self.assertIn("$atom->FormalCharge->Numerator = 3;", script)
            self.assertIn("$atom->FormalCharge->Denominator = 1;", script)
            self.assertIn("$doc->AsymmetricUnit->Atoms(0)", script)
            write_xsd(
                output,
                atoms=[("1", "Al", (0.1, 0.1, 0.1), "3"), ("2", "O", (0.6, 0.6, 0.6), "-2")],
                vectors=((10, 0, 0), (0, 10, 0), (0, 0, 10)),
            )
            output.write_text(output.read_text(encoding="utf-8").replace('Name="Al1"', 'Name="Si1"'), encoding="utf-8")
            result = geology.validate_substitution_result(request["input"], output, request["substitutions"])
            self.assertEqual(result["status"], "substitution_geometry_pass")
            self.assertEqual(result["formal_charge_after_e"], 1.0)

    def test_counterion_ledger_enforces_triclinic_pbc_clearance_and_postvalidation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.xsd"
            output = root / "output.xsd"
            vectors = ((10, 0, 0), (2, 9, 0), (0, 0, 12))
            write_xsd(source, atoms=[("1", "O", (0.1, 0.1, 0.1), "-1")], vectors=vectors)
            placements = [{"atom_name": "Na_site_1", "element": "Na", "formal_charge_e": 1, "fractional_xyz": [0.6, 0.6, 0.6]}]
            request = geology.validate_counterion_ledger(source, placements, 2.0, 2.0, 10)
            script = geology.build_counterion_script(request["placements"])
            self.assertIn('$doc->CreateAtom("Na"', script)
            self.assertIn('$ion->Name = "Na_site_1";', script)
            write_xsd(
                output,
                atoms=[("1", "O", (0.1, 0.1, 0.1), "-1"), ("2", "Na", (0.6, 0.6, 0.6), "1")],
                vectors=vectors,
            )
            output.write_text(output.read_text(encoding="utf-8").replace('Name="Na2"', 'Name="Na_site_1"'), encoding="utf-8")
            result = geology.validate_counterion_result(request["input"], output, request["placements"], 2.0, 2.0)
            self.assertEqual(result["status"], "counterion_geometry_pass")
            self.assertEqual(result["formal_charge_after_e"], 0.0)
            near_boundary = [{"atom_name": "Na_bad", "element": "Na", "formal_charge_e": 1, "fractional_xyz": [0.99, 0.1, 0.1]}]
            with self.assertRaisesRegex(ValueError, "clearance"):
                geology.validate_counterion_ledger(source, near_boundary, 2.0, 2.0, 10)

    def test_materialsscript_charge_audit_overrides_omitted_xsd_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "charge.tsv"
            path.write_text(
                "stage\tatom_count\tformal_charge_e\n"
                "before\t16\t0\n"
                "after\t17\t1\n",
                encoding="utf-8",
            )
            audit = geology.parse_charge_audit(path)
            self.assertEqual(audit["before"], {"atom_count": 16, "formal_charge_e": 0.0})
            self.assertEqual(audit["after"], {"atom_count": 17, "formal_charge_e": 1.0})
            path.write_text("stage\tatom_count\tformal_charge_e\nbefore\t16\t0\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "before and after"):
                geology.parse_charge_audit(path)

    def test_hydroxylation_runtime_charge_precondition_handles_omitted_xsd_charge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            surface = Path(directory) / "surface.xsd"
            write_xsd(
                surface,
                atoms=[("1", "Si", (0.0, 0.0, 0.0), "4"), ("2", "O", (0.0, 0.0, 1.6), "-2")],
                vectors=((10, 0, 0), (0, 10, 0), (0, 0, 1)),
                bonds=[("3", "1,2", "1")],
                periodic_dimension=2,
            )
            text = surface.read_text(encoding="utf-8")
            text = text.replace(' FormalCharge="4"', '').replace(' FormalCharge="-2"', '')
            surface.write_text(text, encoding="utf-8")
            sites = [{
                "oxygen_atom_index": 1,
                "oxygen_atom_name": "O2",
                "expected_oxygen_fractional_xyz": [0.0, 0.0, 1.6],
                "oxygen_from_formal_charge_e": -2,
                "oxygen_to_formal_charge_e": -2,
                "hydrogen_name": "H_SURF_1",
                "hydrogen_fractional_xyz": [0.0, 0.0, 2.6],
                "hydrogen_formal_charge_e": 1,
                "surface_side": "top",
            }]
            request = geology.validate_hydroxylation_ledger(surface, sites, 0.9, 1.1, 0.6, 10)
            script = geology.build_hydroxylation_script(request["sites"])
            self.assertIn("$oxygen_charge_num_0 == -2", script)
            self.assertIn("$oxygen_0->FormalCharge = $oxygen_charge_value_0", script)
            self.assertIn("$hydrogen_0->FormalCharge = $hydrogen_charge_value_0", script)
            self.assertIn("actual=$oxygen_charge_num_0/$oxygen_charge_den_0 expected=-2/1", script)

    def test_explicit_surface_hydroxylation_ledger_checks_coordination_bonds_and_density(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "surface.xsd"
            output = root / "hydroxylated.xsd"
            vectors = ((10, 0, 0), (0, 10, 0), (0, 0, 1))
            write_xsd(
                source,
                atoms=[("1", "Si", (0.0, 0.0, 0.0), "0"), ("2", "O", (0.0, 0.0, 1.6), "0")],
                vectors=vectors,
                bonds=[("3", "1,2", "1")],
                periodic_dimension=2,
            )
            sites = [{
                "oxygen_atom_index": 1,
                "oxygen_atom_name": "O2",
                "expected_oxygen_fractional_xyz": [0.0, 0.0, 1.6],
                "oxygen_from_formal_charge_e": 0,
                "oxygen_to_formal_charge_e": 0,
                "hydrogen_name": "H_SURF_1",
                "hydrogen_fractional_xyz": [0.0, 0.0, 2.56],
                "hydrogen_formal_charge_e": 0,
                "surface_side": "top",
            }]
            request = geology.validate_hydroxylation_ledger(source, sites, 0.8, 1.2, 0.7, 10)
            script = geology.build_hydroxylation_script(request["sites"])
            self.assertIn('$doc->CreateBond($oxygen_0, $hydrogen_0, "Single");', script)
            write_xsd(
                output,
                atoms=[
                    ("1", "Si", (0.0, 0.0, 0.0), "0"),
                    ("2", "O", (0.0, 0.0, 1.6), "0"),
                    ("4", "H", (0.0, 0.0, 2.56), "0"),
                ],
                vectors=vectors,
                bonds=[("3", "1,2", "1"), ("5", "2,4", "1")],
                periodic_dimension=2,
            )
            output.write_text(
                output.read_text(encoding="utf-8").replace('Name="H4"', 'Name="H_SURF_1"'),
                encoding="utf-8",
            )
            charge_audit = {
                "before": {"atom_count": 2, "formal_charge_e": 0.0},
                "after": {"atom_count": 3, "formal_charge_e": 0.0},
            }
            result = geology.validate_hydroxylation_result(
                request["input"], request["input_bonds"], output, request["sites"], 0.7, charge_audit
            )
            self.assertEqual(result["status"], "hydroxylation_geometry_pass")
            self.assertAlmostEqual(result["candidate_silanol_density_OH_nm2"]["top"], 1.0)
            with self.assertRaisesRegex(RuntimeError, "final formal charge"):
                geology.validate_hydroxylation_result(
                    request["input"], request["input_bonds"], output, request["sites"], 0.7,
                    charge_audit, required_final_formal_charge_e=1.0,
                )

    def test_public_nanopore_contract_adapter_preserves_blocked_decision(self) -> None:
        decision = {
            "schema_version": 2,
            "status": "blocked",
            "construction_released": False,
            "errors": [],
            "blockers": ["Hydroxylation algorithm is required"],
        }
        with patch.object(server, "assess_geopore_contract", return_value=decision) as assess:
            result = server.ms_geology_assess_nanopore_contract("contract.json")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["status"], "blocked")
        self.assertFalse(result["data"]["construction_released"])
        assess.assert_called_once_with("contract.json")

    def test_supercell_mcp_adapter_executes_once_and_registers_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model").mkdir()
            (root / "reports").mkdir()
            source = root / "source.xsd"
            write_xsd(
                source,
                atoms=[("1", "Na", (0.1, 0.1, 0.1), "1"), ("2", "Cl", (0.6, 0.6, 0.6), "-1")],
                vectors=((10, 0, 0), (0, 10, 0), (0, 0, 10)),
            )
            source_hash = geology.sha256_file(source)

            def fake_job(**kwargs):
                destination = Path(kwargs["output_files"]["structure"]["destination_path"])
                write_xsd(
                    destination,
                    atoms=[
                        ("1", "Na", (0.05, 0.1, 0.1), "1"), ("2", "Cl", (0.30, 0.6, 0.6), "-1"),
                        ("3", "Na", (0.55, 0.1, 0.1), "1"), ("4", "Cl", (0.80, 0.6, 0.6), "-1"),
                    ],
                    vectors=((20, 0, 0), (0, 10, 0), (0, 0, 10)),
                )
                return {"success": True, "job_id": "job", "audit_path": "audit", "run_mat_script_exit_code": 0, "timed_out": False}

            with patch.object(server, "get_project", return_value={"project_directory": str(root)}), patch.object(
                server, "resolve_workspace_path", return_value=source
            ), patch.object(server.confirmation_manager, "consume") as consume, patch.object(
                server, "_run_materialsscript_job", side_effect=fake_job
            ) as execute, patch.object(
                server, "register_artifact", side_effect=lambda *args, **kwargs: {"status": "registered"}
            ), patch.object(
                server, "run_idempotent", side_effect=lambda project, key, tool, parameters, implementation: (implementation(), False)
            ):
                result = server.ms_geology_build_supercell(
                    str(root), str(source), source_hash, 2, 1, 1, "salt_2x", 10,
                    "supercell-key", confirmation_token="token", timeout_seconds=30, dry_run=False,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["status"], "candidate_model_pass")
            self.assertTrue((root / "model" / "salt_2x.xsd").is_file())
            self.assertTrue((root / "reports" / "salt_2x.supercell.receipt.json").is_file())
            consume.assert_called_once()
            execute.assert_called_once()

    def test_invalid_adapter_request_returns_versioned_error_without_execution(self) -> None:
        with patch.object(server, "_run_materialsscript_job") as execute:
            result = server.ms_geology_enumerate_surface_terminations(
                "project", "bulk.xsd", "0" * 64, 0, 0, 0, 20.0, [0.0],
                "quartz101", 4, "surface-key", confirmation_token="token", timeout_seconds=30,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_request")
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
