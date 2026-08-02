# P4-C public fixed-profile CASTEP preflight

## Public endpoint

P4-C registers one public R0 endpoint:

`ms_castep_fixed_profile_preflight(input_manifest, input_manifest_sha256)`

It is a read-only eligibility check for exactly the standalone P3-C alpha-quartz
input package. It verifies the complete manifest/XSD/cell/param/contract hash
chain and returns a deterministic request hash.

It accepts no cores, timeout, task, functional, cutoff, k-point grid,
pseudopotential, command, output path, queue, retry, or execution flag.

## Safety boundary

The endpoint:

- creates no file;
- starts no Perl, CASTEP, Materials Studio, Gateway, MPI, or license;
- returns `execution_allowed=false`;
- requires a new external authorization for any future execution;
- preserves the P3-C plan as retired;
- does not register a public execution endpoint.

General `castep.calculation` and `results.castep_parsing` remain
unverified and not implemented.

## Verification

- P4-C API tests: 3/3 pass.
- Combined P4/public-compatibility tests: 21/21 pass.
- Complete suite: 322/322 pass.
- P3-C qualification maintenance verifier: pass.
- Source and immutable deployment integrity: pass.
- Public tool count: 50, with only this additional R0 preflight endpoint.
- P4-C final verifier ran twice with stable receipt SHA-256:
  `19A5933EB41941C2BEFE8C52A44FFAEE1E7DD206E5039EC0B2226312FA991C14`.
- No CASTEP, MPI, Materials Studio, Gateway, or license process was started.
