"""
picks_db.py — SQLite parse-cache over the xlsx archive.

Every backtest used to re-open and re-parse 200+ archive workbooks with
openpyxl just to rebuild the same pick rows. This module ingests each
archive file ONCE — through the very same backtest._read_* functions the
file-scan path uses, so DB contents are exactly what the loaders would
have produced — and serves them back as a query.

Design properties:
  * The archive stays the source of truth. picks.db is a rebuildable
    cache: delete it (or bump SCHEMA_VERSION when a reader changes) and
    it re-ingests from the xlsx files on the next sync().
  * No second write path. Scans keep writing xlsx only; sync() picks up
    new files incrementally (a nightly backtest ingests ~2 files instead
    of parsing ~200).
  * Rows are stored as the reader's dict, JSON-encoded, keyed by
    (tab, file_date, an_only) — the only dimensions the load_archive_*
    functions filter on. Reader schema changes never require SQL
    migrations, just a version bump.

backtest.py calls this via _load_from_picks_db(), which falls back to
the original file scan on ANY error — the cache can never block a
backtest.
"""
import glob
import json
import logging
import os
import sqlite3
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)

# Bump when any backtest._read_* output shape changes — forces a full
# re-ingest so cached rows always match what the readers would produce.
# v2 (2026-07-25): _read_ev_edges gained cal_ev_pct (shadow calibration).
SCHEMA_VERSION = 2

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", _MODULE_DIR)
DB_PATH = os.path.join(OUTPUT_DIR, "picks.db")

TABS = ("ev", "pt", "promo", "prop")


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(con)
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    (version,) = con.execute("PRAGMA user_version").fetchone()
    if version != SCHEMA_VERSION:
        if version != 0:
            log.info("[picks-db] schema %s -> %s: rebuilding cache",
                     version, SCHEMA_VERSION)
        con.execute("DROP TABLE IF EXISTS picks")
        con.execute("DROP TABLE IF EXISTS ingested_files")
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.execute("""
        CREATE TABLE IF NOT EXISTS ingested_files (
            filename    TEXT PRIMARY KEY,   -- basename, unique per archive
            file_date   TEXT NOT NULL,      -- ISO, parsed from the name
            an_only     INTEGER NOT NULL,
            size        INTEGER,
            ingested_at TEXT NOT NULL,
            n_rows      INTEGER NOT NULL
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            id        INTEGER PRIMARY KEY,
            filename  TEXT NOT NULL,
            tab       TEXT NOT NULL,
            file_date TEXT NOT NULL,
            an_only   INTEGER NOT NULL,
            data      TEXT NOT NULL         -- reader dict, JSON-encoded
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_picks_q "
                "ON picks (tab, file_date, an_only)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_picks_file "
                "ON picks (filename)")
    con.commit()


def _archive_files() -> list[tuple[date, str, bool]]:
    """(file_date, path, an_only) for every parseable archive workbook —
    same discovery surface as backtest._discover_archive_files, minus
    the date-range/mode filters (the DB stores everything; filters apply
    at query time)."""
    import backtest as bt   # lazy — avoids a module-level cycle
    seen: set[str] = set()
    out: list[tuple[date, str, bool]] = []
    for pat in (os.path.join(bt.ARCHIVE_DIR, "ev_edges_*.xlsx"),
                os.path.join(bt.SCRIPT_DIR, "ev_edges_*.xlsx")):
        for f in sorted(glob.glob(pat)):
            b = os.path.basename(f)
            if b in seen:
                continue
            seen.add(b)
            fd = bt._file_date(f)
            if fd is None:
                continue
            out.append((fd, f, bt._is_an_only_file(f)))
    return out


def sync() -> dict:
    """Ingest archive files the cache hasn't seen (or whose size changed
    — an archived workbook is normally immutable, so a size change means
    it was regenerated and must be re-parsed). Returns counters."""
    import backtest as bt   # lazy

    readers = (("ev",    bt._read_ev_edges),
               ("pt",    bt._read_play_these),
               ("promo", bt._read_promo_picks),
               ("prop",  bt._read_props_from_odds_comparison))

    stats = {"new_files": 0, "new_rows": 0, "total_files": 0}
    with _connect() as con:
        known = dict(con.execute(
            "SELECT filename, size FROM ingested_files").fetchall())
        files = _archive_files()
        stats["total_files"] = len(files)

        for fd, path, an_only in files:
            base = os.path.basename(path)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if known.get(base) == size:
                continue
            if base in known:      # regenerated file — replace its rows
                con.execute("DELETE FROM picks WHERE filename = ?", (base,))
                con.execute("DELETE FROM ingested_files WHERE filename = ?",
                            (base,))

            n_rows = 0
            for tab, reader in readers:
                try:
                    rows = reader(path, fd)
                except Exception:
                    log.exception("[picks-db] %s reader failed on %s",
                                  tab, base)
                    rows = []
                if rows:
                    con.executemany(
                        "INSERT INTO picks (filename, tab, file_date, "
                        "an_only, data) VALUES (?, ?, ?, ?, ?)",
                        [(base, tab, fd.isoformat(), int(an_only),
                          json.dumps(r, default=str)) for r in rows])
                    n_rows += len(rows)

            con.execute(
                "INSERT INTO ingested_files (filename, file_date, an_only, "
                "size, ingested_at, n_rows) VALUES (?, ?, ?, ?, ?, ?)",
                (base, fd.isoformat(), int(an_only), size,
                 datetime.now(timezone.utc).isoformat(), n_rows))
            con.commit()
            stats["new_files"] += 1
            stats["new_rows"] += n_rows
    return stats


def load_raw(tab: str, start_dt: date, end_dt: date,
             an_only_mode: str = "exclude") -> list[dict]:
    """Raw reader-shaped rows for one tab across the date range, in the
    same chronological order the file-scan path produces (filename sorts
    chronologically; id preserves within-file row order)."""
    if tab not in TABS:
        raise ValueError(f"unknown tab {tab!r}")
    q = ("SELECT data FROM picks WHERE tab = ? "
         "AND file_date >= ? AND file_date <= ?")
    params: list = [tab, start_dt.isoformat(), end_dt.isoformat()]
    if an_only_mode == "exclude":
        q += " AND an_only = 0"
    elif an_only_mode == "only":
        q += " AND an_only = 1"
    q += " ORDER BY file_date, filename, id"

    rows: list[dict] = []
    with _connect() as con:
        for (data,) in con.execute(q, params):
            p = json.loads(data)
            # json round-trip stringifies the one non-JSON field the
            # readers emit; restore it so dedup/date logic sees a date.
            fd = p.get("file_date")
            if isinstance(fd, str):
                try:
                    p["file_date"] = date.fromisoformat(fd)
                except ValueError:
                    pass
            rows.append(p)
    return rows


def stats() -> dict:
    """Cheap cache introspection for logs / the dashboard."""
    with _connect() as con:
        files = con.execute("SELECT COUNT(*) FROM ingested_files"
                            ).fetchone()[0]
        by_tab = dict(con.execute(
            "SELECT tab, COUNT(*) FROM picks GROUP BY tab").fetchall())
    return {"files": files, "by_tab": by_tab, "db_path": DB_PATH}


if __name__ == "__main__":
    print(f"picks.db at {DB_PATH}")
    print("sync:", sync())
    print("stats:", stats())
