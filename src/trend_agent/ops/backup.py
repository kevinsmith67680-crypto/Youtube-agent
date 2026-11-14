"""Backups and the restore drill.

``backup()`` writes a ``pg_dump`` custom-format archive (compressed, restorable table by
table) plus a SHA-256 manifest. ``restore_drill()`` proves a backup is usable: it restores
the archive into a **separate scratch database** (its name must end in ``_restore_drill``;
it is the only database this module ever drops), verifies the schema revision and compares
row counts per table with the counts captured when the backup was taken. The source
database is only ever read.

Passwords go to the client tools through ``PGPASSWORD`` in the child environment, never
on the command line (where ``ps`` would show them).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import sqlalchemy as sa

from trend_agent.db.session import create_db_engine

SCRATCH_SUFFIX = "_restore_drill"
_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class PgTarget:
    host: str
    port: int
    user: str
    password: str | None
    dbname: str

    @classmethod
    def from_url(cls, url: str) -> PgTarget:
        p = urlsplit(url)
        if not p.scheme.startswith("postgres"):
            raise BackupError("only PostgreSQL URLs are supported")
        db = p.path.lstrip("/")
        if not _NAME.match(db):
            raise BackupError(f"unsupported database name {db!r}")
        return cls(
            host=p.hostname or "localhost",
            port=p.port or 5432,
            user=unquote(p.username or ""),
            password=unquote(p.password) if p.password else None,
            dbname=db,
        )

    def sqlalchemy_url(self, dbname: str | None = None) -> str:
        from urllib.parse import quote

        auth = quote(self.user, safe="")
        if self.password:
            auth += ":" + quote(self.password, safe="")
        return f"postgresql+psycopg://{auth}@{self.host}:{self.port}/{dbname or self.dbname}"

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        if self.password:
            env["PGPASSWORD"] = self.password
        env["PGCONNECT_TIMEOUT"] = "10"
        return env

    def args(self, dbname: str | None = None) -> list[str]:
        return ["-h", self.host, "-p", str(self.port), "-U", self.user, "-d", dbname or self.dbname]


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BackupError(f"{name} is not installed (PostgreSQL client tools are required)")
    return path


def table_counts(url: str) -> dict[str, int]:
    engine = create_db_engine(url)
    try:
        with engine.connect() as conn:
            names = (
                conn.execute(
                    sa.text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
                    )
                )
                .scalars()
                .all()
            )
            out = {}
            for n in names:
                if not _NAME.match(n):
                    continue
                out[n] = int(conn.execute(sa.text(f'SELECT count(*) FROM "{n}"')).scalar_one())  # noqa: S608  # nosec B608
            return out
    finally:
        engine.dispose()


def schema_revision(url: str) -> str | None:
    engine = create_db_engine(url)
    try:
        with engine.connect() as conn:
            if not conn.execute(
                sa.text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            ).scalar_one():
                return None
            return conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
    finally:
        engine.dispose()


@dataclass
class BackupResult:
    path: str
    bytes: int
    sha256: str
    seconds: float
    created_at: str
    schema_revision: str | None
    table_counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def backup(url: str, out_dir: Path, *, now: datetime | None = None) -> BackupResult:
    target = PgTarget.from_url(url)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{target.dbname}_{stamp}.dump"
    counts = table_counts(url)
    revision = schema_revision(url)
    started = time.perf_counter()
    proc = subprocess.run(  # noqa: S603 - fixed argv
        [
            _tool("pg_dump"),
            *target.args(),
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(path),
        ],
        env=target.env(),
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    if proc.returncode != 0:
        path.unlink(missing_ok=True)
        raise BackupError(f"pg_dump failed: {proc.stderr.strip()[:500]}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = BackupResult(
        path=str(path),
        bytes=path.stat().st_size,
        sha256=digest,
        seconds=round(time.perf_counter() - started, 2),
        created_at=stamp,
        schema_revision=revision,
        table_counts=counts,
    )
    path.with_suffix(".manifest.json").write_text(json.dumps(result.as_dict(), indent=2))
    return result


@dataclass
class DrillResult:
    ok: bool
    backup_path: str
    scratch_database: str
    restore_seconds: float
    schema_revision: str | None
    tables_compared: int
    mismatches: dict[str, dict[str, int]]
    notes: list[str]
    at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def restore_drill(url: str, backup_path: Path, *, keep: bool = False) -> DrillResult:
    """Restore ``backup_path`` into ``<db>_restore_drill`` and verify it."""
    source = PgTarget.from_url(url)
    manifest_path = backup_path.with_suffix(".manifest.json")
    if not backup_path.exists():
        raise BackupError(f"backup file not found: {backup_path}")
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    notes: list[str] = []
    if manifest is not None:
        digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        if digest != manifest.get("sha256"):
            raise BackupError("backup checksum does not match its manifest")
        expected: dict[str, int] = manifest.get("table_counts", {})
    else:
        notes.append("no manifest: row counts compared with the live source database")
        expected = table_counts(url)
    scratch = f"{source.dbname}{SCRATCH_SUFFIX}"[:63]
    if not scratch.endswith(SCRATCH_SUFFIX) or scratch == source.dbname or not _NAME.match(scratch):
        raise BackupError("refusing: unsafe scratch database name")
    admin = create_db_engine(source.sqlalchemy_url("postgres"))
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
            conn.execute(sa.text(f'CREATE DATABASE "{scratch}"'))
        started = time.perf_counter()
        proc = subprocess.run(  # noqa: S603 - fixed argv
            [
                _tool("pg_restore"),
                *source.args(scratch),
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                str(backup_path),
            ],
            env=source.env(),
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
        seconds = round(time.perf_counter() - started, 2)
        if proc.returncode != 0:
            raise BackupError(f"pg_restore failed: {proc.stderr.strip()[:500]}")
        restored_url = source.sqlalchemy_url(scratch)
        got = table_counts(restored_url)
        revision = schema_revision(restored_url)
        mismatches = {
            t: {"expected": expected.get(t, -1), "restored": got.get(t, -1)}
            for t in sorted(set(expected) | set(got))
            if expected.get(t) != got.get(t)
        }
        if manifest is not None and revision != manifest.get("schema_revision"):
            notes.append(
                f"schema revision {revision} != manifest {manifest.get('schema_revision')}"
            )
        ok = not mismatches and revision is not None and not any("revision" in n for n in notes)
        return DrillResult(
            ok=ok,
            backup_path=str(backup_path),
            scratch_database=scratch,
            restore_seconds=seconds,
            schema_revision=revision,
            tables_compared=len(set(expected) | set(got)),
            mismatches=mismatches,
            notes=notes,
            at=datetime.now(UTC).isoformat(),
        )
    finally:
        if not keep:
            with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        admin.dispose()
