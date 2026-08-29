# MigraLoop — Agentic Database Schema Drift Reconciler & Data Loss Guardian

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
5. **Constraint Resolver** — on failure (SQL error or
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

_(filled in as the advanced solution is built — every entry here
should tie a specific change to a specific benchmark result, not a
general claim)_

## Hot Take

_(filled in after running both baseline and advanced against all 6
cases — this should name one specific, real failure mode we observed
and the concrete engineering lesson it taught us)_

## Reproduction

See `REPRODUCTION.md` (not yet written) for exact setup + CLI commands.
