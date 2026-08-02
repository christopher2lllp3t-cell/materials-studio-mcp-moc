# P3-B real CASTEP qualification attempt 1 — failed before CASTEP start

## Outcome

The one authorized P3-B r1 attempt was consumed on 2026-08-02. The fixed
`cmd.exe` root process exited with code 1 after approximately 0.22 seconds.
No `.castep` output was created, no CASTEP result was parsed, and no license
availability was established.

This attempt is a failed platform qualification. It is not a CASTEP
calculation result and provides no scientific evidence.

## Root cause

The frozen P3-A command preview was represented as a Python argv list whose
final item contained an internally quoted batch-file path. On Windows,
`subprocess.Popen` serialized those internal quotes with backslashes before
passing them to `cmd.exe /s /c`. The command interpreter therefore received
the batch path as a literal `\"...RunCASTEP.bat\"` token and reported:

`is not recognized as an internal or external command, operable program or batch file`.

This is a runner/command-serialization design defect. It occurred before
RunCASTEP could start CASTEP, acquire a license, or create a CASTEP output.

## Integrity and cleanup evidence

- authorization SHA-256:
  `34E07A795C681FD0E8D71C18A0E6479AF02E9446576A90D6E9F867BDD6BC3C2F`
- runner receipt SHA-256:
  `DFBC27FB62A490D9D8559A804C55B413A9803C11060E2976E72C51646AC0B187`
- stderr SHA-256:
  `7D1178AB04BFD033FD4BBA3467858BA3086CBF0CD980893ED8A33775DE04906D`
- stdout SHA-256 (empty):
  `E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855`
- staged input hashes before and after exit are identical;
- observed descendant process count: zero;
- owned processes remaining: zero;
- post-attempt CASTEP/MS/MPI process check: zero.

The complete immutable receipt is archived as
`docs/validation/receipts/p3b-real-castep-attempt-1.json`.

## Containment and next gate

The P3-B r1 plan is now permanently retired in code before any plan,
authorization, or process handling. A harmless batch fixture with a path
containing spaces proves the corrected raw Windows command-line quoting, but
that correction is not authorized for a second CASTEP attempt.

Any retry requires:

1. a new frozen plan hash that binds the corrected Windows serialization;
2. full offline and harmless-batch regression verification;
3. a new explicit single-use user authorization.
