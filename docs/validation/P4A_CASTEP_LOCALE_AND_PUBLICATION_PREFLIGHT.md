# P4-A CASTEP locale-safe environment and publication preflight

## Locale finding and resolution

The Codex parent environment supplies `LC_ALL=C.UTF-8`,
`LC_CTYPE=C.UTF-8`, and `LANG=C.UTF-8`. Windows uses code page 936 and
the Materials Studio 2023 bundled Perl does not support `C.UTF-8`, causing a
fallback warning to the Simplified Chinese system locale.

P4-A does not change the parent process, user environment, machine
environment, registry, Materials Studio installation, or immutable deployment.
It creates a child-only environment with all three locale variables set to
`C`.

The fixed MS Perl binary is SHA-256 bound to
`31F0629C1A9A6489505376AA76AE5A742667615487F35338E8306DE1317EF95F`.
A harmless fixed print command completed with exit code 0, exact expected
stdout, zero-byte stderr, and no locale/fallback markers. It did not start
CASTEP or consume a license.

## Publication preflight boundary

P4-A records the exact P3-C alpha-quartz fixed profile that may be considered
for P4-B, but remains nonexecuting:

- `execution_allowed=false`;
- `public_tool_added=false`;
- public tool count remains 49;
- general `castep.calculation` and `results.castep_parsing` remain
  unverified;
- caller-selected materials or CASTEP parameters remain unsupported.

P4-B still requires frozen request/response schemas, confirmation and
authorization lifecycle review, and deployment/rollback review.

## Verification

- P4-A targeted tests: 8/8 pass.
- Complete suite: 314/314 pass.
- P3-C final maintenance verifier: pass at the new full-suite count.
- Source dependency and release-manifest integrity: pass.
- Immutable deployment dependency and integrity: pass.
- CASTEP/MS/MPI processes before and after: zero.
- P4-A verifier ran twice with stable receipt SHA-256:
  `75B82B6122EAD9A0FE33C4BFC192C4E15BAF5049C3F92407220F8D75419D81F7`.
