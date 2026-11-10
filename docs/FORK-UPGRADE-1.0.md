# Rumi fork upgrade to Open Wearables 1.0

Pinned upstream: `7ca121dd4e483ec2f397f141a326e2bc81a960bd` ("update OW to 1.0").
Fork base: `b70b92cb`, the revision Railway runs. This is a real upstream merge, the
same procedure as [the 0.10.0 upgrade](FORK-UPGRADE-0.10.0.md).

## Preserved fork behavior

API-key hashes, opaque IDs and in-place rotation; connection credentials; the Railway
PORT override; `/healthz` and `/readyz`; supervision of both Celery workers (now with
upstream's prefetch flags on the CPU worker); WHOOP recovery revisions replaced in
place; account-scoped WHOOP sleep IDs and event-linked sleep scores; the OAuth
callback redirect guard; `provider` on activity summaries; `filter_by_priority` on
sleep summaries; failed or truncated pulls keeping their cursor; the cycles API and
its scheduler flag; Docker publishing restricted to upstream.

## Decisions where 1.0 changed the same behavior

Rumi reads WHOOP through this fork in production, so where 1.0 changed what Rumi
reads, the fork keeps the answer it gave before 1.0.

- **Recovery summary.** 1.0 builds each day from the highest-priority source and fills
  missing fields from lower-priority ones, so at the default priorities (Apple 1,
  Garmin 2, WHOOP 5) a WHOOP and Apple day came back labelled `apple`, with Apple's
  resting HR and SDNN beside WHOOP's score. Without `provider`, the fork returns what
  it did before 1.0: per day, the best-ranked source's recovery score with the vitals
  sent alongside it and no sleep fields. Garmin and Apple write no recovery score, so
  they never displace WHOOP's day. With `provider`, the fork returns 1.0's row for
  that provider only: its day vitals, main sleep and score, never filled from another
  provider.
- **WHOOP samples in raw `/timeseries`.** 1.0 writes WHOOP recovery vitals only as
  sleep session values and cycle energy and steps only as provider day rows, and its
  aggregate backfill moves the old samples to `series_sample_backup`. Rumi reads them
  from raw `/timeseries`. The fork writes each one as a sample too, at the recovery's
  time or the day's local midnight with `is_daily_total: true`, beside its aggregate
  row. The backfill copies WHOOP's old samples into the aggregate tables but leaves
  them in `series_sample`.
- **`is_daily_total`.** 1.0 stores every sample with a null flag. Rumi sums steps and
  energy only where it is `false` and keys measurements on it, so samples keep the
  flag their writer gives them, as before.
- **Sleep vitals.** Upstream 1.0 fills a sleep's missing vitals from the user's other
  devices. The fork keeps the sleep's own provider, writer and device only, so
  `vital_provenance: same_provider_writer_device` stays true. Rumi reads that field.
- **WHOOP sleep scores.** Upstream records them at the wake-up; the fork adopts that,
  keeping its event link and its explicit null for unscored nights. The startup
  script `restamp_provider_sleep_scores.py` moves stored scores, including linked
  ones; the rehearsal below checks a linked fork score.
- **WHOOP cycle projection.** 1.0 stores provider day values in `series_aggregate`,
  which keeps no per-row ID. The projection now writes those day rows beside its
  samples. On a touched day it removes its old day row, unless an unowned pre-upgrade
  sample or archive row holds that day: those still block the projection, as before.
  The aggregate backfill leaves WHOOP's samples in `series_sample`, so they keep
  blocking it after the backfill too.
- **Activity `provider` filter.** Applied to upstream's day aggregates before source
  priority, so the metrics upstream fills from lower-priority sources also come
  only from the requested provider.
- **Health score key.** Upstream adds `data_source_id` to the unique key; the fork's
  WHOOP recovery upsert targets the renamed constraint.

## Migration review required before shared deployment

One head, `63c70566c6d3`, joins the fork head `f82e01ac749d` (provider cycles) with
upstream's `3f6b9c2d8e41`. It changes no schema. Upstream's chain renames
`data_point_series` to `series_sample`, adds the aggregate tables, rebuilds the
health-score unique key concurrently, and adds history rollup and sleep
`provider_state`. Its statements use a 5 s lock timeout, so a busy database fails
fast and the next start retries.

Startup runs more data corrections (`backend/scripts/start/app.sh`) and queues the
aggregate backfill in Celery. Summaries read samples until that backfill finishes.
Take a restorable backup first: the downgrade refuses once provider day values exist.

1.0 refuses to start unless `SECRET_KEY` has at least 32 characters.

## Repeatable fixture verification

No real users, credentials, vendor calls or deployed database are used. Docker is
required. The rehearsal creates its own PostgreSQL containers.

```sh
cd backend
uv sync --frozen --all-groups --extra otel
uv run python scripts/check_migrations.py --base fork/main
uv run python scripts/verify_fork_upgrade.py /tmp/ow-fork-upgrade-report.json postgres:17
OW_CYCLE_E2E_OUTPUT=/tmp/ow-cycle-journey.json uv run pytest -q tests/integration/test_whoop_cycle_journey.py
uv run pytest -q
```

The report covers three paths to the head: a fresh database, the pre-0.10 fork head
and the current fork head with fictional samples, scores, a sleep and a cycle. It
checks replay, key and connection preservation, sample IDs and values after the
rename, health-score links, the renamed constraint, the sleep-score restamp and
deletion cascades.
