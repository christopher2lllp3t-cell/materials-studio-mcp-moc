# Materials Studio MCP 1.3.0 baseline verification

## Scope and release boundary

- Release: `1.3.0`
- Channel: candidate
- `production_science_released`: `false`
- Baseline verification entry: `scripts/verify_baseline_v1.ps1`
- Structured receipt: `docs/validation/receipts/v1.3.0-baseline-verification.json`

This baseline covers the accepted source tree and the already-installed 1.3.0
deployment. It does not start Materials Studio, select a Gateway, invoke
CASTEP, consume a license, or parse a CASTEP result.

## Validation result

The verification entry completed successfully with the following fixed checks:

| Check | Result |
| --- | --- |
| `python -m unittest discover -s tests -q` | pass, exactly 252 tests |
| source `python -m pip check` | pass |
| source release-manifest integrity | pass, `9C55537484C77C53CEA2E53260ED470AB059F6DE6423300B9BD9CBD0EB99791D` |
| deployed `python -m pip check` | pass |
| immutable deployment verify | pass, bundle `207AB795043A264038A179974D8E86A518F20CB85A7D457C2A90C58A7D5DE723` |

## Scientific and execution status

- Standalone CASTEP input generation is candidate-input preparation only.
- Real `castep.calculation` remains unverified and unavailable.
- CASTEP result parsing remains unverified and unavailable.
- This Git baseline is not a production-science release or an authorization to
  execute any simulation.

## Repository hygiene

The baseline repository intentionally excludes host-only configuration and
generated state, including local MCP/environment configuration, Python virtual
environments, build products, package metadata, caches, release bundles,
installation receipts, and task/output directories. The local files remain on
the machine for validation but are ignored by Git.
