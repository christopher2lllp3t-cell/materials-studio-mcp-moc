# P2 synthetic runner helper

`synthetic_castep_helper.py` is a test-only process-control adapter. It never
imports or invokes CASTEP, Materials Studio, RunCASTEP, MPI, or a Gateway. Any
`.castep` it writes starts with an explicit synthetic marker and exists solely
to exercise runner receipts and the private P1 parser boundary.
