# P1 — private offline CASTEP result-parser qualification

## Scope and release boundary

P1 adds `materials_studio_mcp.castep_result_parser` as a private, offline
qualification layer for already-existing standalone `.castep` text. It is not
imported by `server.py`, adds no public MCP tool, does not start a process, and
does not select a Gateway or consume a license.

- Release line: `1.3.0` candidate source, after immutable Git tag
  `v1.3.0-baseline`
- `production_science_released`: `false`
- `castep.calculation`: still `unverified` / `not_implemented`
- `results.castep_parsing`: still `unverified` / `not_implemented`
- Private registry entry: `results.standalone_castep_parser_qualification`,
  `todo` / `not_implemented`

The parser returns `completed` only when all of the following are present:

1. the generated standalone input manifest, copied XSD, `.cell`, `.param`, and
   canonical contract hashes verify;
2. the output file name is exactly `<manifest seedname>.castep`;
3. the caller supplies an external expected output SHA-256 and it matches the
   parsed bytes (otherwise `output_unbound`); and
4. finite final-energy and total-time evidence occur without a failure,
   timeout, cancellation, or contradictory marker.

This binding prevents an arbitrary same-named file from being accepted without
external attestation. It still cannot prove that the output is a freshly
executed job. A future controlled runner receipt must additionally bind its
start time, PID, exact command, input hashes, and final output hash; P1 does
not implement such a runner.

## Evidence boundary

No user research result was read or added to Git. The repository did not contain
a lawful frozen real `.castep` fixture. Two official Materials Studio 23.1
example outputs were inspected read-only outside the repository only:

| Example (under `<MS23>/share/Examples/Projects/CASTEP`) | SHA-256 | Observed completion form |
| --- | --- | --- |
| `Fe_phonons_Files/Documents/Fe CASTEP GeomOpt/Fe.castep` | `4F0E88D1C0582A162D04CA0DA9DCA00D678EAA778E32FEA7D1136EA432525657` | finite `Final energy, E = ... eV`, `BFGS: Geometry optimization completed successfully.`, and finite `Total time = ... s` |
| `l_alanine Files/Documents/l_alanine CASTEP Energy/l_alanine.castep` | `E3348DAD2C0D29AFC2732BCD11EC8F9F1400B0B5B8310EB801FD425EE74EF929` | finite `Final energy = ... eV` and finite `Total time = ... s` |

The completion recognisers accept both observed final-energy forms and require
finite total time. All repository fixtures under
`tests/fixtures/castep_result_parser/` explicitly say they are synthetic
boundary fixtures. License, SCF, fatal, timeout, cancellation, truncation,
non-finite numeric, and conflicting-marker cases are synthetic coverage, not
runtime evidence.

## Qualification coverage

`tests/test_castep_result_parser.py` exercises normal completion; license
unavailable; SCF/electronic-minimisation nonconvergence; fatal errors; timeout;
cancellation; incomplete output; `NaN`/`Infinity`; conflicting markers; a
nonzero exit code with a complete log; a zero exit code with a failed log; stale
seed output; input tampering; expected-hash mismatch; and an unbound same-named
output.

No P1 command launches Materials Studio, CASTEP, `RunCASTEP.bat`, or a Gateway.
The tests create only temporary non-executable standalone input candidates and
synthetic text files.

## Validation record

The committed P1 receipt is
`docs/validation/receipts/p1-castep-result-parser-verification.json`. It records
the 262-test full candidate suite, source manifest integrity, source/deployment
`pip check`, and immutable deployment verification. It is a software
qualification record only, not a CASTEP execution result.

- Overall result: pass
- Source candidate manifest SHA-256: `86AC721BDBD423EE6941C440C747E5D461B640CA6BC51B2B4044B22163B166BD`
- P1 receipt SHA-256: `63B8B6B4E9C66AE3A35D587E5A11269D82C092376E22275BAC6D71BAA7CE7ED0`
- Immutable deployment bundle SHA-256: `207AB795043A264038A179974D8E86A518F20CB85A7D457C2A90C58A7D5DE723`
