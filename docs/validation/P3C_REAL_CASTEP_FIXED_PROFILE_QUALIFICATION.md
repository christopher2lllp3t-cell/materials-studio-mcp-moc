# P3-C fixed-profile real CASTEP qualification

## Final result

The corrected P3-C attempt completed successfully on Materials Studio 2023
CASTEP using the exact private alpha-quartz qualification profile.

- Material: 9-atom alpha-quartz Si3O6
- Task: SinglePoint
- XC: PBE
- Cutoff: 600 eV
- k-point grid: 3x3x3
- pseudopotentials: default OTFG
- execution: local, four cores, no queue
- process exit: 0
- CASTEP total time: 44.23 s
- final energy: -3158.163551162 eV
- output SHA-256:
  `EE91F3319375DEFD581644840F64718C066291027D2E837ACD7B6DCEB468E851`
- runner receipt SHA-256:
  `12FB79B370A783618C5F0580192D2B40E459A4E6DD4D9875210CED05415EB872`

CASTEP explicitly reported successful checkout of `MS_castep_site` for four
copies. The output parser independently bound the output hash, classified the
run as completed, and found finite final energy and total time without failure
markers.

## Control evidence

The staged manifest, XSD, cell, param, and contract hashes were identical
before launch and after exit. The runner observed the cmd, Perl, MPI, SMPD,
CASTEP, and console descendant tree and found zero owned processes remaining
after completion.

The successful authorization SHA-256 was
`8F3C910EB03B6CD3222B345314EB28F743441F17892007723B85EEFE50FD0D4C`.
Its marker was atomically consumed. The P3-C plan is now retired before any
future authorization or process start.

## Verification

- corrected Windows quoted-batch fixture: pass on the real Windows command interpreter;
- P3-C targeted tests: 16/16 pass;
- complete offline suite: 306/306 pass;
- source and immutable deployment dependency/integrity checks: pass;
- public MCP tool count: unchanged at 49;
- final verifier ran twice with stable receipt SHA-256:
  `6F6773861D217ADD8186270B3DA25C3F346B28A858A9F3D4F3C9645EC7EBCCFA`.

## Capability boundary

Only `castep.real_qualification_execution_candidate` is verified, and only
for this exact private fixed profile and local platform chain.

The public/general `castep.calculation` and `results.castep_parsing`
capabilities remain `unverified/not_implemented`. No public MCP calculation
tool was added.

The 600 eV cutoff, 3x3x3 grid, and reported energy are platform-qualification
evidence only. They do not establish scientific convergence or production
fitness.
