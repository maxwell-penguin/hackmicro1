# MigraLoop — Agentic Database Schema Drift Reconciler & Data Loss Guardian

live at : https://maxwell-penguin.github.io/hackmicro1/
## The Problem

Production database schemas drift from application ORM models over
time — manual emergency hotfixes, unmerged branches, legacy migrations
that never ran. Reconciling that drift by hand is slow and carries
real outage risk.

Asking an LLM to "just generate a fix" makes this worse, not better: a
single-shot model frequently resolves a type mismatch or rename by
dropping the column and recreating it — syntactically correct,
semantically catastrophic, silent production data loss.

**Target user:** backend/platform engineers managing relational
databases and ORMs who need schema drift resolved safely, not just
plausibly.

## The Solution

MigraLoop is a multi-agent migration engine with a deterministic,
non-AI **Data Loss Guardian** sitting between "the agent generated a
migration" and "the migration is accepted." Every candidate migration
is applied inside an isolated sandbox database, checksummed before and
after, and rejected outright if any pre-existing data disappears
without being explicitly declared as an intentional drop.

## Scoping Notes (read before judging the architecture)

**SQLite, not Postgres.** The pitch and much of the domain language
here (table locks, `ALTER COLUMN` semantics, online index creation)
comes from a Postgres mental model. We built the sandbox on SQLite
instead — it spins up an isolated, disposable database in milliseconds
with no Docker dependency, which matters for a 3-day solo build and
for reproducibility on a judge's machine. SQLite's lack of most
`ALTER TABLE` operations (no `ALTER COLUMN`, no adding constraints to
existing tables) actually *forces* the add-column → backfill →
drop-column pattern we wanted to demonstrate anyway. The multi-step
backfill pattern the agents learn to apply generalizes directly to
Postgres; the specific DDL syntax would need to change, the reasoning
would not.

**6 of the original 10 benchmark cases.** Cut down to the highest-
signal subset for a solo 3-day timeline: simple add, rename-with-data,
type cast, table split, composite unique index (dedup), and safe
deprecation. Each was chosen to exercise a different part of the
Guardian (straightforward pass-through, the core "don't silently drop
data" case, value-representation normalization, cross-table content
tracking, declared row-count decrease, declared column drop).

## Architecture

1. **State Extractor** — introspects the physical DB schema and the
   ORM's target schema, produces a structured drift report.
2. **Migration Synthesizer** — generates migration SQL *and* a
   `MigrationManifest` declaring anything it's intentionally
   discarding (a deprecated column, a deduplicated row).
3. **Sandbox Verifier** — applies the migration to an isolated,
   disposable SQLite database inside an explicit transaction.
4. **Data Loss Guardian** (deterministic, no LLM) — snapshots database
   content before and after, and fails the migration if anything
   disappeared that wasn't declared in the manifest.
5. **Retry loop** (implemented as a plain loop inside `orchestrator.py`,
   not a separate class) — on failure (SQL error or
   `DATA_LOSS_DETECTED`), feeds the error back to the Synthesizer for
   a corrected attempt, up to a retry cap.

## Known Limitations

- **The Guardian confirms a value still exists *somewhere* in the
  database, not that it ended up in the *correct* row.** If a
  table-split migration attached the wrong address to the wrong user,
  content-preservation would still pass — that's a correctness bug,
  not a data-loss bug, and would need to be caught by the Sandbox
  Verifier's integration checks instead. We chose to keep the
  Guardian's scope narrow and non-negotiable (nothing disappears
  undeclared) rather than broad and probabilistic.
- **The manifest is currently trusted, not cross-checked.** The
  Synthesizer declares what it's dropping; nothing yet verifies that
  a declared drop actually corresponds to what the drift report says
  should be dropped. An agent could in principle over-declare drops to
  route around the Guardian. Flagged as a stretch goal, not fixed —
  fixing it needs the Extractor's drift report to already exist.

## Improvement Changelog

| Case | Baseline | Advanced | Improved? |
|---|---|---|---|
| 01_simple_add | ok | success | — |
| 02_rename_column_with_data | ok | success | — |
| 03_type_migration | ok | success | — |
| 05_table_split | SQL error | success | ✓ |
| 08_composite_unique_index | DATA LOSS | success | ✓ |
| 10_safe_deprecation | DATA LOSS | success | ✓ |

- **The extractor's table-level-diff fix is why 08_composite_unique_index gets
  attempted at all.** Before the fix in `5487a1b`, drift detection only compared
  column lists, so a UNIQUE constraint added to `tags` — same columns, different
  table-level SQL — registered as "no drift" and the orchestrator's `has_drift`
  gate never called the Synthesizer. Adding `table_definition_changed` (raw
  `CREATE TABLE` text comparison, `src/agents/extractor.py`) is what turns this
  case from silently skipped into an actual attempt, which is why it shows up as
  a success at all in `advanced_results.json` rather than not appearing.
- **The backfill-pattern system prompt rule is the direct cause of the two DATA
  LOSS → success flips.** The baseline LLM resolves both 08 and 10 with the
  classic `CREATE new_table -> INSERT ... GROUP BY / SELECT -> DROP old ->
  RENAME` pattern, which silently drops rows the Guardian later catches
  (`missing values: ['1', 'urgent']`, row count 2→1 on `tags`; `missing values:
  ['xyz123']` on 10_safe_deprecation). The advanced Synthesizer's system prompt
  (`src/agents/synthesizer.py`) states the ADD → backfill → DROP pattern as a
  CRITICAL RULE up front and forces a `propose_migration` tool call with
  `intentional_drops` / `allow_row_count_decrease` as required fields — the
  model can't emit SQL without also declaring what it's discarding.
- **05_table_split fails on the baseline for an ordering bug, not a data-loss
  bug**, and the same manifest-driven Synthesizer fixes it too: baseline SQL
  inserts into `addresses` before creating `users_new`/dropping the FK's target
  in the wrong order, hitting `FOREIGN KEY constraint failed`
  (`trajectories/baseline/05_table_split.json`). This is a different failure
  mode than 08/10 but the same root cause — a single-shot model with no
  SQLite-specific constraints in its prompt — and the advanced prompt's
  explicit list of SQLite `ALTER TABLE` limitations resolves it too.
- **The orchestrator's per-attempt sandbox re-provisioning fix
  (`e90f86d`) prevents a compounding-failure bug that never triggered in this
  run but was caught by `tests/test_orchestrator.py` before the live run
  happened.** `SandboxVerifier.apply_migration` only rolls back on a SQL-level
  error (`src/core/sandbox.py:144-155`, explicit `BEGIN`/`COMMIT`/`ROLLBACK`) —
  a migration that executes cleanly but trips the Guardian's data-loss check
  leaves its DDL committed to that sandbox. The orchestrator provisions a fresh
  sandbox from the original `physical.db` on every retry attempt instead of
  reusing one across attempts, so a corrected second attempt reasons about the
  real starting schema instead of the wreckage of the first attempt's partial
  changes.
- All 6 advanced cases succeeded in a single Synthesizer call (`attempts: 1` in
  `results/advanced_results.json` for every case) — the empirical gains above
  came entirely from getting the first attempt right, not from the retry loop
  correcting a bad one. See [Hot Take](#hot-take).

## Hot Take

Every one of the 6 advanced cases in `results/advanced_results.json` has
`"attempts": 1` and an empty `attempt_errors` list. The retry loop in
`src/orchestrator.py` — re-provision a fresh sandbox, feed the Guardian's
failure back to the Synthesizer, try again up to `MAX_ATTEMPTS = 3` — never
fired against the live model. It's exercised directly in
`tests/test_orchestrator.py` (`case_always_bad`, `case_rename_retry`) with a
mocked Synthesizer that's scripted to fail then succeed, so the mechanics are
proven correct. But "proven correct in a unit test with a scripted failure"
and "battle-tested in production" are different claims, and I don't want the
changelog above to blur them: on the one live run this project has, the retry
path is unit-tested, not observed.

What actually did the work was constraining the first attempt: the
CRITICAL RULE against drop-and-recreate, the explicit SQLite `ALTER TABLE`
limitations (no `ALTER COLUMN`, `CREATE UNIQUE INDEX` instead of `ADD
CONSTRAINT`), and forcing a `propose_migration` tool call so
`intentional_drops`/`allow_row_count_decrease` are structured fields instead
of something the model could just forget to mention. Every one of the three
flips in the comparison table (05, 08, 10) is explainable by one of those
prompt constraints landing on the first try, not by an error message
teaching the model something on a second try. If I deleted the entire retry
loop and kept only the system prompt and the Guardian as a hard gate, this
specific 6-case benchmark would score identically.

The honest engineering conclusion isn't "retries don't matter" — a
harder or more adversarial case set would probably need them, and the
Guardian's whole design assumes some fraction of attempts will be wrong. It's
that for a well-scoped, well-understood domain (SQLite DDL, a fixed small set
of drift shapes), the marginal value of a first attempt built from specific,
enumerated domain constraints was higher than the marginal value of retry
sophistication — at least on this benchmark, at this size. If I had 3 more
days I'd spend them adversarially expanding the benchmark until the retry
loop actually fires for real, rather than adding more retry logic on top of
a loop that's currently unproven outside its unit tests.

## Reproduction

See [REPRODUCTION.md](REPRODUCTION.md) for exact setup + CLI commands.
