# P2 — private synthetic standalone CASTEP runner qualification

## Scope and non-execution boundary

P2 adds the private module `materials_studio_mcp.castep_standalone_runner` for
process-control qualification only. It is not imported by `server.py` and adds
no public MCP tool.

- `run_standalone_castep` is unconditionally blocked. It never resolves or
  starts `RunCASTEP.bat`, `castep.exe`, Materials Studio, MPI, or a Gateway.
- The only spawn path is the reviewed fixed test helper
  `tests/fixtures/castep_runner/synthetic_castep_helper.py`, SHA-256
  `850247944B5F80EEA2DE6638C5F3E8855D7CCC32F72281126FEA720382AF172E`.
- Its `.castep` output has an explicit `SYNTHETIC P2` marker. A successful run
  is `qualified_process_control`, never a CASTEP execution result.
- `castep.calculation` and `results.castep_parsing` remain
  `unverified/not_implemented`; the P2 capability is private
  `todo/not_implemented`.

## Reused safety components

P2 reuses, rather than duplicates, the existing:

1. P1 manifest/contract/XSD/cell/param hash validator before copying and again
   immediately before `Popen`;
2. cross-process `acquire_execution_slot` single-job lock; and
3. Windows process-tree cleanup rooted strictly at the PID returned from the
   runner's own `Popen`, using `taskkill /PID <owned pid> /T /F`.

Every synthetic job gets a new ASCII leaf directory created with exclusive
creation; fixed helper/interpreter paths and SHA-256 values, closed stdin,
stdout/stderr file paths and hashes, timestamps, PID, exit, timeout/cancel and
exception data are recorded in a versioned runner receipt.

## Process-control evidence

`tests/test_castep_standalone_runner.py` covers normal exit; unconditional real
runner blocking; start failure; timeout; explicit cancellation; nonzero exit;
missing output; P1 parse failure; lock competition; directory collision;
pre-launch input mutation; fixed-helper hash mismatch; and registry exposure.

The tree test starts a synthetic parent that starts a child and grandchild. On
timeout it records their PIDs and confirms each PID no longer exists after the
owned-root tree termination. No Windows Job Object was needed because the
existing root-PID tree termination passed this live synthetic check.

## Candidate validation and idempotence

P2 uses `scripts/verify_candidate_p2.ps1`, which is separate from both the
frozen 252-test baseline entry and the historical P1 262-test candidate entry.
It writes only
`docs/validation/receipts/p2-castep-runner-qualification-verification.json`.

The entry is run twice consecutively after the P2 commit. Both runs must pass,
produce a byte-identical P2 receipt, and leave a clean worktree after the second
run. The final receipt records the 274-test suite, source/deployment `pip
check`, source manifest verification, and immutable deployment verification.

This is software/process-control qualification only. No real CASTEP execution,
license checkout, scientific calculation or result interpretation is claimed.

P2 source manifest SHA-256:
`E5C782922D70987A9F9E152323E23F21331CD0745489B3572DFC33015D79AC54`.
The stable P2 receipt SHA-256 is
`451BBA52BC5647272481691F755C0CD14897ED7BC5DE90555BDD44DCC39B79D9`;
its immutable deployment bundle SHA-256 is
`207AB795043A264038A179974D8E86A518F20CB85A7D457C2A90C58A7D5DE723`.
