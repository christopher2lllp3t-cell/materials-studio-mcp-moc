# MS MOC status

Last checked: 2026-07-18

## Current state

- MOC status: `ready`
- MCP public tool count: read from `release-manifest.json`
- Main MCP regression: `179/179`
- MOC integration suite: `30/30`
- Chapter 3 REFPROP 10 pressure-mapping suite: `5/5`
- G02/G04/G06 focused gates: `27/27`, `30/30`, `8/8`
- MOC to MCP stdio bridge: `ready`
- MCP to MOC status adapter: `ready`
- MCP/MOC deployment-root binding: `pass`; the active version is read from `release-manifest.json`
- MCP to MOC document opening: dry run and confirmed live launch passed
- Materials Studio 23.1, RunMatScript, LAMMPS, msi2lmp, MPI, VMD and Packmol: `ready`

## Real evidence

- Project: `D:\分子动力学模拟\07_mcp_materials_studio\mcp_projects\geology_hydroxylation_smoke_v1`
- Document: `model\surfaces\hydroxylated\quartz101_single_oh_api_smoke_v2.xsd`
- SHA-256: `7E0985DB354886F10A49B0AB11CAABEAF6B9376CC8952A1339C2B9C77F37A7B8`
- MCP dry run: `status=dry_run`, no desktop launch
- Confirmed live run: `status=launched`, stable MatStudio desktop instances returned
- MaterialsScript import: 31 asymmetric-unit atoms
- MatStudio log: `Completion status: (OK)`
- Audit directory: `E:\ms_mcp\ms_mcp_jobs\moc_interface\import_xsd_test_20260714_231636_140775`
- MOC CAR/MDF export audit: `E:\ms_mcp\ms_mcp_jobs\moc_interface\export_xsd_to_car_mdf_20260715_093216_136028`
- MOC export SHA-256: CAR `8D3FB1B643ACA6A888C349C13EAA80139FC6190776AC97915587F7E837EB75CD`; MDF `4C2BC1D22DB4103758D9386CAA5A9E4DE0E87E92CE838D4E9449FFFB023BF6A6`
- MCP checked export SHA-256: CAR `6518DC5CCF5F8EF5CD68A339E8907DB122B4CCE932EA03B3A175BA761F82160D`; MDF `A1D35EF21686080877667E7CC68F4FBA38FB877F8AE2731E3B4AA4F1633549DF`
- G01 checked Class II PCFF conversion project: `D:\分子动力学模拟\07_mcp_materials_studio\mcp_projects\g01_checked_conversion_smoke_v1`
- Checked LAMMPS data SHA-256: `D1F4D568360E68BD126C034F9894486138435DD65A20C84E31BFA9F0221159BD`; 3 atoms / 2 bonds / 1 angle, net charge 0, atom-count match and data preflight passed
- Fresh G01 v1 reproduction: `D:\分子动力学模拟\07_mcp_materials_studio\mcp_projects\g01_v1_reproduction_20260715_r2`
- G01 final report SHA-256: `33F650D44C4B2CBFA6E2A021BE7E3DE2A20B48C1461A3B17B3DA5A171CF72249`; project status `validated`; 22 registered artifacts re-hashed successfully
- Immutable offline release bundle: the path and version are read from `release-manifest.json`
- The historical 1.0.11 architecture audit remains historical; current release identity is never hand-written here.
- Historical `1.0.1` cannot be used as a fresh rollback baseline because its immutable bundle lacks the `config` directory. The installer used by `1.0.2` and later rejects incomplete bundle layouts before creating a deployment target.
- MOC `doctor`: `ready`; MOC `acceptance`: `pass`

The model remains a `hydroxylation_geometry_pass` candidate with `production_released=false`; this desktop verification does not release G06.

## 1.0.7 stability correction

- The `1.0.6` bundled client example still pointed to the mutable development runtime, and the installed MCP-to-MOC adapter selected the workspace MOC script without binding it to the installed deployment root.
- `1.0.7` discovers `moc\ms_moc.py` from the same immutable deployment as the installed MCP package, passes that deployment through `MS_MOC_MCP_ROOT`, and supports an explicit `MS_MOC_SCRIPT` launch override.

## 1.0.8 forcefield preparation and failure evidence

- The governed Forcite tool now exposes closed preparation profiles for COMPASSIII, PCFF, Dreiding/QEq, and Universal/QEq without accepting open module settings.
- The only relaxed input gate is an otherwise valid XSD whose sole failure is missing `ForcefieldType`; all other structure failures remain blocked.
- Postflight requires unchanged atom count, element inventory, bond count, and bond-order distribution, complete type and partial-charge coverage, and a neutral net partial charge within tolerance.
- Real Type II-C kerogen validation passed COMPASSIII, Dreiding/QEq, and Universal/QEq. PCFF failed closed on five untyped particles and retained its complete failure evidence; no PCFF candidate was published.
- Failed governed Forcite runs now preserve and register scripts, stdout, MatStudio logs, execution audits, parameters, environment data, and a structured failure receipt.

## 1.0.9 immutable acceptance correction

- `1.0.8` built and passed 188 source-tree tests but did not copy `tests/` into the installed deployment, so installed MOC acceptance correctly failed its regression stage.
- `1.0.9` includes the complete Python regression suite in the release manifest, bundle hashes, immutable deployment, and post-install verification.
- MOC acceptance can now rerun the same suite from the installed deployment instead of depending on the mutable development tree.

## 1.0.10 portable installed acceptance

- `1.0.9` packaged the tests but omitted source and installer scripts required by two release-manifest tests, so installed acceptance still failed closed.
- `1.0.10` packages and hashes `src/`, `scripts/`, and `tests/`, binds release-manifest discovery to `MS_MOC_MCP_ROOT`, and installs the complete acceptance source tree.

## 1.0.11 trusted preparation gates

- A successful governed forcefield preparation now records `structure=pass` and `forcefield=pass` through private trusted validators using the postflight evidence.
- Public callers still cannot claim a passing quality gate directly.
- Science-contract and production-science gates remain separate and are not changed by type assignment.
- The bundled MCP client example now points to `E:\ms_mcp\deployments\current` and explicitly binds both the MOC script and MCP root to that pointer.
- Three focused discovery/binding tests and the full `179/179` regression pass. Platform stability does not change any scientific release decision.

## Safety behavior

- MCP can open only a hash-bound document inside its bound initialized project.
- Dry run does not consume a confirmation token or start MatStudio.
- Live launch requires a short-lived single-use confirmation bound to all parameters.
- Project-level idempotency prevents the same request from launching twice.
- MOC subprocess stdin/stdout/stderr are isolated from the MCP protocol transport.
- MatStudio launch reports dispatcher and stable desktop PIDs; no surviving desktop instance is a failure.
- MaterialsScript success requires an explicit OK MatStudio log, not only exit code 0.
- Invalid nanopore contracts return nonzero; valid but blocked contracts return a successful assessment with `construction_released=false`.
- Deprecated direct conversion and Forcite functions are absent from MCP registration. Checked conversion and `ms_forcite_calculation_checked` are the governed replacements.
- Persisted `md_task_submit/query/cancel/retry` uses a fixed target allowlist, one-time owner capabilities (SHA-256 only at rest), exact confirmation, atomic JSON records and Task Scheduler workers that survive MCP session closure.

## Governed Forcite and task evidence

- Project: `D:\分子动力学模拟\07_mcp_materials_studio\mcp_projects\forcite_checked_smoke_1_0_5`
- Input preflight: 16 independent atoms, 14 bonds, complete `c3u/o1u/n3u/h1n` typing, formal charge 0, minimum distance `1.02077 A`.
- Synchronous `energy_compassiii_v1`: dry-run and exact confirmation passed; MatStudio log OK; output SHA-256 `39E2C7C9E9AD5AF7F78AE04E0CF5BCBDE3A21AC0737C1317971A02354E91D0AE`; receipt SHA-256 `6DCCE8027DB4F400F1D3E913C19EBEE115BE13770C93326178639BEAF1C9DB7B`.
- Detached task `86552cb8-0387-40de-aa3c-9c73cebd5745`: first MCP session closed before completion; a second session queried `succeeded` with `forcite_calculation_pass`.
- Cancel/retry task chain: `ef66e9a7-4095-4950-876e-de505fbef634` cancelled after dry-run and exact confirmation; retry `bf669efd-c63c-499d-8cfe-4c0af793b188` used a new idempotency key and completed successfully.
- No `MaterialsStudioMCP-*` scheduled tasks remained after terminal cleanup.

## Remaining scientific blocks

- G02-CS-02 passed all six arms, all three paired-cutoff comparisons and both cross-seed groups; the frozen decision selects a `9.0 Å` cutoff. `G02-CS-02B` preserved six original raw `failed` states, derived six complete states, and hash-proved three external-interruption recoveries. Evidence SHA-256: `6F2777C7B2B0263A2FB046DAD06CF2D981538589583C99D183DD7FCE3E75C26D`.
- G04 Na force-field provenance, real LAMMPS/VMD smoke gates and the frozen three-seed G04-TH-01 coupled variable-cell dry thermodynamic gate pass. G04 is a qualification fixture and now has `qualification_released=true`; it is not incorrectly required to produce a paper observable.
- G06 has 6 hash-verified literature sources and 18 visual reviews. The public 2024 SI closes much of the force-field transcription gap, but 38 construction-contract fields remain unresolved and `construction_released=false`.
- The chapter 3 CH4-UA and rigid TraPPE-CO2 PDB templates are generated, geometry-validated and hash-bound; three formal inputs remain absent: the combined pore PDB, combined PSF and complete NAMD parameter file.
- The paper does not distinguish periodic mineral box lengths from the smaller reported atom-coordinate projection extents. A local neutral D220 coordinate candidate matches `Si8Al4O20(OH)4`, but remains non-authoritative until box/extent/wall registry is frozen.
- A local Heinz/INTERFACE `interface_pcff.frc` candidate contains mica/montmorillonite/pyrophyllite PCFF 9-6 types, but the thesis appendix omits mineral charges and parameters, and a lossless NAMD 12-6/cross-family mapping is not established.
- NAMD 3.0.2 Win64-multicore is installed at `E:\ms_mcp\software\NAMD_3.0.2_Win64-multicore\namd3.exe` and passed a real Tcl smoke run. Licensed REFPROP 10 is API-version checked and hash-bound through `CH03-EOS-RP01`; its nine-point CO2/CH4 density-to-pressure grid passed with evidence SHA-256 `1286E667E3F31C1565C33ED81B53CCEA5B71C3536A0796DB72C6C8027582BAC7`. MOC reports seven remaining blockers and no longer emits the NAMD or REFPROP dependency blockers. G02 and G04 have `qualification_released=true`; G06 remains blocked.
