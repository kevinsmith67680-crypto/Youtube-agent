# Backup and recovery

A backup that has not been restored is not known to work. This system therefore tracks a
**restore drill**, not just a backup schedule.

## 1. What is backed up

The whole PostgreSQL database, which holds all state:

* observations;
* events and analyses;
* models' metadata (artifacts are files; see §5);
* recommendations;
* creator data;
* operational state.

`trend-agent system backup --out DIR` writes:

* `<db>_<UTC timestamp>.dump`: `pg_dump` custom format (compressed, restorable table by
  table, without owners or privileges);
* `<db>_<UTC timestamp>.manifest.json`: SHA-256 of the dump, schema revision and the row
  count of every table at backup time.

`scripts/backup.sh` (cron) wraps it and keeps the newest `KEEP` dumps (default 14).
Production should *also* use the managed database's own backups and point-in-time recovery.
This application-level dump is portable and is what the drill verifies.

## 2. Restore drill

```
trend-agent system restore-drill backups/trend_agent_20261114T120000Z.dump
```

The drill:

1. verifies the dump's SHA-256 against the manifest, and refuses a corrupted file;
2. drops and recreates only `<db>_restore_drill`, the only database this code ever
   drops; the name is enforced, and the source database is only read;
3. runs `pg_restore --exit-on-error` into it;
4. checks the restored schema revision and compares the row count of **every table** with
   the manifest;
5. drops the scratch database (keep it with `--keep`), and records the result in
   `system_state.restore_drill`.

The readiness item `backup_restore_drill` passes only with a successful drill in the last
30 days.

### Verified

* `tests/integration/test_ops.py::test_backup_and_restore_drill`: a real dump and restore
  of the test database. All ≥ 70 tables matched, and a corrupted dump was rejected by its
  checksum.
* E2E `s38`: the CLI path, recorded and then read by the readiness scorecard.
* Development database, 2026-11-14 (`trend-agent system backup` + `restore-drill`):
  see `phase8_report.md`.

## 3. Recovery procedure (production)

1. Stop the scheduler and workers (`docker compose stop scheduler worker`). The API can stay
   up read-only, or be stopped.
2. Choose the restore source:
   * managed point-in-time recovery to a **new** instance (preferred); or
   * `pg_restore` of the latest verified dump into a **new, empty** database.

   Never restore over the live database.
3. Run the `migrate` service against the restored database if it is behind.
4. Point `TREND_AGENT_DATABASE_URL` at it and start the services in order (migrate → api →
   scheduler → worker → frontend).
5. Verify with `trend-agent system health`, `system readiness` and
   `evaluate pipeline --no-db` (offline checks).

Data written since the backup is lost, unless point-in-time recovery was used. Collection
resumes from the targets' schedules; the backfill is bounded by the provider's history.

## 4. Destructive operations policy

Nothing in this code base drops or truncates a production table:

* retention deletes only expired rows, under the 30-day policy;
* the drill drops only `*_restore_drill`;
* migrations are additive by policy.

Tests run on separate test databases only.

## 5. Model artifacts

Forecast model artifacts (JSON parameters plus LightGBM text, with a checksum in the
database) live in `TREND_AGENT_FORECAST_ARTIFACT_DIR`. Back that directory up with the
database. A missing or altered artifact is detected at load time, and the scope falls back
to baseline-only forecasts.
