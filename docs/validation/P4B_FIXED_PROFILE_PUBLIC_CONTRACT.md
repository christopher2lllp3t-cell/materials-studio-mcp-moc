# P4-B fixed-profile public API contract

## Scope

P4-B freezes, but does not register, a public-facing contract for the exact
P3-C alpha-quartz qualification profile.

The future preflight endpoint is reserved as
`ms_castep_fixed_profile_preflight`. It accepts only the exact prepared P3-C
manifest and its SHA-256. It accepts no caller-selected material, functional,
cutoff, k-point grid, pseudopotential, cores, timeout, command, or output path.

The future execution endpoint is reserved as
`ms_castep_fixed_profile_execute`, risk R3. It is not implemented.

## Execution gate

Any future implementation must require all of the following:

1. a new frozen execution plan;
2. a new explicit external single-use authorization;
3. a short-lived, single-use public confirmation token bound to the exact
   preflight request hash;
4. independent P4-C review;
5. candidate deployment and rollback review.

Automatic retries and reactivation of the consumed P3-C plan are prohibited.

## Rollback

P4-B is source-candidate only. It does not modify the immutable deployment,
the current pointer, the public registry, or any MCP client-visible tool list.
Rollback is simply not registering or deploying the reserved interface.

## Verification

- P4-B contract tests: 5/5 pass.
- Complete suite: 319/319 pass.
- P3-C and P4-A maintenance verification: pass.
- Source and immutable deployment integrity: pass.
- Public tool count: remains 49.
- P4-B verifier ran twice with stable receipt SHA-256:
  `523B1EC784067173E2AD8B21C2AFE418D3A9EAD8D38ADDE17F9FF4A79B58D63C`.
- No CASTEP, MPI, Materials Studio, Gateway, or license process was started.
