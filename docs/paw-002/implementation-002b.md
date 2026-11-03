# PAW 002B implementation evidence

Work started 2026-11-04 on `paw-002b-ledger`. Acceptance is pending until all eight slices and the real-container/browser gates pass.

## Slice 1 migration and schema

Migration 0011 preserves existing conversations and tool audit metadata, distinguishes unavailable historical result bodies, and introduces the immutable ledger, removable content, summaries, roles and lineage. Unique indexes prevent duplicate user/assistant messages per run and duplicate call/result messages per invocation. Forgotten content cannot be reinserted.

Evidence: 70 schema and lifecycle tests passed against disposable PostgreSQL databases (including migration backfill and upgrade/downgrade). Existing backfill test had an erroneous SQL placeholder replacement; corrected before verification.

## Accepted addition task oriented compaction

The owner approved structured task checkpoints on 2026-11-04. Agent-reported task boundaries guide compaction, with the context size limit retained as a fallback. Checkpoints are guidance rather than proof of an external operation; original ledger evidence remains authoritative.
