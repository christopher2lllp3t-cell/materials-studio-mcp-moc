# P3-B real CASTEP single-use qualification runner — pre-run acceptance

## Scope

This candidate prepares exactly one private, local Materials Studio 2023 CASTEP
platform-qualification attempt. It is not an MCP tool and does not release
general CASTEP execution or result parsing.

The only authorized profile is the frozen P3-A alpha-quartz Si3O6 SinglePoint
plan: PBE, 600 eV, 3x3x3 k-point grid, default OTFG, four local cores, no queue,
and a 600-second hard timeout. These are platform-qualification settings, not
research convergence evidence.

## Frozen bindings

- P3-A plan SHA-256:
  `E461D57676903DEA6A19886D1AE85EB28859DC4AE2DC933D9890AA1E8D59C35E`
- standalone input manifest SHA-256:
  `8CAF21ABEB448A6D2669AA10684362652B2E97A1677D8C1AC1682F11CECA1C79`
- RunCASTEP.bat SHA-256:
  `FE09BD22E729E03D1B75027CAC9ECF2A0CC250A170FE5EE309CC33CCD070C027`
- cmd.exe SHA-256:
  `65EC268ADD3973B6DCA64222985DA47CAEAEE44A340B0EC1466782914FD743D9`

## Safety controls reviewed

- exact authorization fields and canonical SHA-256 are mandatory;
- a 64-hex nonce is atomically consumed with exclusive file creation;
- replay cannot create a second process;
- a new non-overwriting full-ASCII job directory is required;
- staged XSD/cell/param/contract hashes are checked before launch and after exit;
- the command is a fixed argv list with `shell=False`;
- stdin is closed and stdout/stderr are persisted and hashed;
- only the owned root process tree is terminated on timeout or cancellation;
- parser output must be externally hash-bound and completed;
- success requires zero exit, no staged-input drift, no errors, and no observed
  owned descendants remaining.

## Verification evidence

- P3-B targeted tests: 13/13 pass.
- Complete offline suite: 303/303 pass.
- Source `pip check` and source release-manifest verification: pass.
- Immutable 1.3.0 deployment `pip check` and verification: pass.
- Public MCP tools: unchanged at 49.
- Pre-run verifier executed twice with identical receipt SHA-256:
  `805160218187639E192E9DDE74D1DC6DD7CBDD9E89FC1DC7ABCAA012CBE61BEE`.
- No RunCASTEP, CASTEP, Materials Studio, Gateway, or MPI process was started by
  pre-run verification.
- No real authorization marker was consumed during pre-run verification.

## Capability status before the real attempt

`castep.real_qualification_execution_candidate` remains private
`todo/not_implemented`. Public `castep.calculation` and
`results.castep_parsing` remain `unverified/not_implemented`.

A failed or interrupted real attempt must not be retried without a new explicit
user authorization.
