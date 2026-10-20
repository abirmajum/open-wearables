# Rumi fork upgrade to Open Wearables 0.10.0

Pinned upstream: `d18304f8076a7710c48a4b279f8e8abd5a16887e` (tag `0.10.0`).
Fork base: `6655de01`. This is a real upstream merge, including its Svelte dashboard,
Python 3.14 runtime, API contracts and schema migrations.

## Preserved fork behavior

API-key hashes, opaque IDs and connection credentials survive the upgrade. Rotation
keeps the opaque ID while invalidating the old secret. The upstream hashing
migration depends on the fork's existing hash migration and renames its prefix
column without rehashing. No applied fork migration is rewritten.

The upgrade retains the Railway PORT override, readiness checks, worker process
supervision, recovery corrections, WHOOP sleep revisions, OAuth callback path,
activity provider filters, sleep priority opt-out and failed/truncated-sync cursor
protection. Sleep summary vitals now use the same provider, writer and device as
the sleep record; they no longer average unrelated devices in the same window.

## Ongoing cycles

`GET /api/v1/users/{user_id}/summaries/cycles` exposes WHOOP cycle revisions under
normal API-key authentication. Range filters select provider update timestamps;
they do not mean the cycle overlaps that calendar date. The response preserves
cycle ID, interval, nullable readings, score state, provider update and fetch time.
Whole-cycle energy includes basal energy. Pending and unscorable scores remain
null; an explicit zero step count remains zero. An ongoing cycle never enters the
legacy daily totals. Newer provider revisions replace older ones, including
corrections that lower a count. User deletion cascades to owned cycles.

The new `CYCLE_SYNC_ENABLED` flag defaults to false. Enable it on the scheduler
only after the coordinated release. `CYCLE_SYNC_INTERVAL_SECONDS` defaults to 900
and is constrained to 900–1800. It queues a two-day WHOOP cycle refresh for active,
pull-eligible accounts through the existing sync leases and completion webhooks.
The normal hourly sync remains the broader repair path. There is no per-chat
WHOOP call. Ordinary provider syncs also persist cycles once the upgrade runs.

## Migration review required before shared deployment

The application startup runs Alembic and upstream data-correction scripts. Review
these effects and take a restorable database backup before releasing:

- One merged schema head, `f82e01ac749d`, adds the user-owned cycle table and joins
  the fork and upstream histories. Other upstream additions include sync runs,
  telemetry state, meal details, coverage, seed-user flags and indexes.
- SDK refresh tokens for deleted or missing applications are revoked by upstream
  migration `c4e1a7b93d20`. Valid application tokens gain explicit foreign keys.
- Data-source identity is made stable, duplicate sources/events may be merged or
  removed, and device classifications are corrected.
- WHOOP strain is linked to workouts; closed-cycle legacy dates are corrected;
  WHOOP energy is relabelled from active to total and zero-energy no-wear rows
  are removed. New cycle payloads preserve reported zero values independently.
- SDK workout calories recorded as misleading zero become unknown. Google
  provider identities and Ultrahuman metric labels are also corrected.

Startup data corrections can warn and continue, so a green health endpoint does
not prove they completed. Inspect their migration receipts and logs. Do not
blindly downgrade: code rollback alone cannot undo deleted rows, revoked tokens,
merged sources or the API-key column change. Restore the reviewed backup with
matching application code if a database rollback is necessary.

## Repeatable fixture verification

No real users, credentials, vendor calls or deployed database are used here.
Docker is required; the migration rehearsal creates its own PostgreSQL containers
and does not accept an existing database URL. Its default PostgreSQL 17 matches
the current Railway database; pass `postgres:18` as the second argument to also
rehearse against upstream CI's major version.

```sh
cd backend
uv sync --frozen --all-groups --extra otel
uv run python scripts/check_migrations.py --base origin/main
uv run python scripts/verify_fork_upgrade.py /tmp/ow-fork-upgrade-report.json
OW_CYCLE_E2E_OUTPUT=/tmp/ow-cycle-journey.json uv run pytest -q tests/integration/test_whoop_cycle_journey.py
uv run pytest -q
```

`ow-fork-upgrade-report.json` records fresh install, fork upgrade, replay, key and
connection preservation, the schema head and cycle deletion ownership.
`ow-cycle-journey.json` records the committed revision and authenticated API
responses after real HTTP fixture ingestion and PostgreSQL persistence. Pass that
file into Rumi's `pnpm run eval:wearables -- --ow-cycle-artifact=...` journey to
verify native tool reads from the same contract. The fixture proves mechanics;
it does not establish real WHOOP freshness, iOS authentication or device latency.

Release order: checked Rumi PR with `RUMI_WHOOP_CYCLES_ENABLED=false`, then reviewed
OW migration and the same OW revision for backend, worker, scheduler and
dashboard (the API-key UI contract also changes), then
`CYCLE_SYNC_ENABLED=true` and Rumi's flag. Verify service revisions, auth, provider
connections, sync receipts and source/interval/freshness before declaring success.
