# Time checkpoint and daily plan verification, 2026-11-20

The full suite passed **318 tests** on Windows with Python 3.13 and disposable
PostgreSQL 15. Combined application line and branch coverage was **99.51%**.
Black, isort, Compose profile validation and `git diff --check` passed. A fresh
database bootstrapped through Alembic revision `20261120_07`. A separate test
migrated a populated previous-schema checkpoint and recovered its optimizer and
replay.

| Check | Result |
|---|---|
| Resume the 14-day online software check from the end of day 5 | Byte-identical daily CSV and summary to a continuous run on the same machine. Both reached 2,260 updates, 56 sends, zero constraint violations and zero pending transitions. |
| Frozen 14-day check | 56 sends, zero gradient updates, fixed parameter hash and zero constraint violations. |
| Interrupt offline pretraining during the second device fit | Both exported device bundles matched uninterrupted fixture training byte-for-byte. |
| Export a participant between daily refits | The restored personal replay retained its insertion order after wraparound. The next refit reproduced the live checkpoint, including Adam and RNG state. |
| Revise or cancel an RCT daily plan | A stale revision was ignored, confirmed sends stayed available, and a superseded future decision yielded its grid slot and next-state link to the replacement. |

An end-to-end check exercised a wearer and a nonwearer for three synthetic days
with the shipped device artifacts, FastAPI, the worker and the Artemis message
handler. The fixture confirmed 18 sends across the two users. Each user
accumulated 132 personal transitions and applied 528 gradient updates. Each
export restored to the corresponding database checkpoint. Broker delivery and
live CS and VU acceptance require deployment verification. The full archived
simulator log was unavailable in this checkout. The offline interruption check
used a small source-log fixture.

Run instructions and the historical restore cutoff are in the
[checkpoint guide](../../README.md#resume-a-participant-at-a-time-checkpoint).

## Implementation verification, 2026-11-04

The completed checks used Python 3.12.3 and a disposable PostgreSQL 15 container.
No live Artemis messages were consumed and no physical notifications were sent.

The full suite passed **294 tests**. Combined application line and branch
coverage was **99.90%** (2,466 of 2,467 statements and 632 of 634 branches).
`black --check app scripts tests migrations`,
`isort --check-only app scripts tests migrations` and `git diff --check` passed.
The remaining unmeasured paths are defensive time-zone and dispatch branches.
Only command-line
entry guards are excluded from coverage. The full command is:

```bash
pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=99 tests/
```

For local tests, `POSTGRES_*` and `TEST_DATABASE_URL` must select a disposable
test database, and `POLICY_LEARNING_MODE=frozen` supplies the suite's default.
Tests of online learning set their own mode. Docker Compose's `checks` profile
provides this isolated arrangement.

The checks verify source sampling across every participant and across recorded
days, identical shared initialization for new RCT IDs, the exact 8 shared-offline
and 24 personal-online samples in each batch, isolation between RCT users,
restart fidelity, and rejection of missing shared replay. They also verify
exploration probabilities, forced actions, selected settings, daily fitting,
replay-ring wraparound, corrupt checkpoints, arm-F reward recovery and sampling
archive checksums.

## Learning check outputs

The actual policy and network were run for 14 artificial days with seed 11.
Four ordered items were supplied per day. Response probabilities were 0.6
within the fixture's two specified time windows and 0.1 elsewhere; opened
messages had a uniformly sampled delay between one and 20 minutes. These are
software test assumptions and are not the pilot-calibrated DBN model.

| Check | Online | Frozen artifact |
|---|---:|---:|
| Gradient updates | 2,260 | 0 |
| Network weights changed | Yes | No |
| Sends | 56 | 56 |
| Constraint violations | 0 | 0 |
| Pending transitions at completion | 0 | 0 |

The results establish that fitting executes, frozen inference keeps fixed
weights, and the tested queue remains feasible. They do not establish increasing
reward, a clinical benefit, or general superiority over another algorithm.

- [Online daily CSV](online/daily.csv) and [summary](online/summary.json)
- [Frozen daily CSV](frozen/daily.csv) and [summary](frozen/summary.json)
- [Source and environment manifest](implementation_manifest.json)

Recreate the outputs from the module root:

```bash
python -m scripts.check_policy_learning --days 14 --seed 11 --out build/online
python -m scripts.check_policy_learning --days 14 --seed 11 --mode frozen --out build/frozen
```

The JSON files include initial and final parameter hashes and the artifact
version. A deterministic-repeat test compares the full daily trajectory and
summary for two runs with the same seed. Numerical-library or platform changes
may affect floating-point hashes; the manifest records the tested versions.

## UML rendering

All four `.puml` files were rendered to SVG with the local Docker image
`ghcr.io/plantuml/plantuml@sha256:9b5071ab56a4192aac458ab4b0d42261c22c8109695894ae5be6013c3be6cac1`.
No diagram source was sent to a public rendering service.

On 2026-11-07, the module component view was revised to follow
`docs/diagrams/workflow-modules.drawio`, and a separate OMI implementation
component view was added. Both use UML component symbols and usage dependencies.
The SVGs passed PlantUML rendering and XML parsing and were visually checked.

Live broker verification, physical delivery, trial policy assignment and the
external data mappings listed in `PLAN.md` remain outside these completed checks.
