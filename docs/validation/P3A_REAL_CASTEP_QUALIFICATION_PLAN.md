# P3-A real CASTEP qualification plan (execution blocked)

Date: 2026-08-02

## Scope

P3-A prepares one exact local CASTEP platform-qualification plan. It does not
run Materials Studio, RunCASTEP, CASTEP, MPI, or Gateway and does not acquire a
license. The private execution function remains unconditionally blocked and no
public MCP tool is added.

The plan is stored at
`docs/validation/receipts/p3a-real-castep-qualification-plan.json` with canonical
SHA-256 `E461D57676903DEA6A19886D1AE85EB28859DC4AE2DC933D9890AA1E8D59C35E`.

## Frozen qualification candidate

- Input manifest SHA-256: `8CAF21ABEB448A6D2669AA10684362652B2E97A1677D8C1AC1682F11CECA1C79`
- Source XSD SHA-256: `12B9147B763EBD2BAB08F04F2D304E51DB422854B47F93890B52BFB2A1AEF8EE`
- Seed: `quartz_alpha_sp_4c`
- Cell: alpha-quartz, 9 runtime atoms, Si3O6
- Task: SinglePoint; PBE; default OTFG; non-spin-polarized; fixed occupancy
- Qualification candidates: 600 eV and 3x3x3 Monkhorst-Pack grid
- Resources: local, 4 cores, one job, hard timeout 600 seconds, no queue

The cutoff and k-point grid are syntax/platform qualification values only. They
are not convergence evidence and cannot support a research conclusion.

## Runtime evidence frozen by P3-A

- `RunCASTEP.bat`: SHA-256 `FE09BD22E729E03D1B75027CAC9ECF2A0CC250A170FE5EE309CC33CCD070C027`
- `RunCASTEP.Readme`: SHA-256 `08F9E048CFC9D9C98054D77EEB7F0AF3A2AD61F79182B2E9D81CC75A57D1712B`
- `C:\Windows\System32\cmd.exe`: SHA-256 `65EC268ADD3973B6DCA64222985DA47CAEAEE44A340B0EC1466782914FD743D9`
- Local Gateway and license services were observed running; a license seat is
  still unverified. No remote queue is configured.

## Controls required by a future P3-B attempt

- A new non-overwriting ASCII job directory under the fixed qualification root.
- Exact staged-input hashes before launch and after exit.
- A single cross-process execution slot and four-core ceiling.
- Closed stdin and hash-bound stdout/stderr/result receipts.
- Timeout or cancellation targets only the owned root process tree.
- Success requires finite final energy and total time, no failure markers, an
  output SHA-256 binding, unchanged inputs, and no owned processes remaining.
- The authorization must be single-use and bind the exact plan, input,
  launcher, command interpreter, resources, timeout, and new job directory.

## Remaining blockers

1. The user has not separately authorized a licensed CASTEP execution.
2. The real runner is not released; the current execution entry is blocked.
3. An available CASTEP license seat has not been demonstrated.
4. Scientific convergence is not established.

P3-B must not begin until the user explicitly confirms this exact one-job plan.

## Verification

- P3-A targeted tests: 11/11 passed.
- Full candidate suite: 290/290 passed.
- Source dependency check and source manifest verification: pass.
- Immutable deployed 1.3.0 dependency and deployment verification: pass.
- Candidate verification ran twice; receipt SHA-256 remained
  `96C5DD9F7411305E3EB54E20000811278ED5A7F5F5E8BA81D021DA341DD25791`.
- Source manifest SHA-256:
  `1958F5A7BA0098E2F27EC72F28E8C4E37C021A7A39A4280DFC71D72811E5DA96`.
- No CASTEP, MatServer, or MS server process was started by P3-A.
