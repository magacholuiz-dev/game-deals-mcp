"""Backups, exports and restore for deals.db.

The accumulated price history is the most valuable thing in the project: it
cannot be recovered from any API, because the stores only show today's price.
So copies are taken with SQLite's own online backup API (consistent even while
the collector is writing), and a copy is only kept if it passes an integrity
check and holds as many products and price points as the source. A backup that
was never verified is not a backup.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import config, db

# Tables worth carrying between machines. http_cache is disposable and
# schema_version is per-file, so neither is exported.
EXPORT_TABLES = ("products", "aliases", "price_points", "watches", "alerts",
                 "deal_signals", "job_runs", "job_state")


def _dir(dest: str | Path | None) -> Path:
    p = Path(dest or config.BACKUP_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _counts(c: sqlite3.Connection) -> dict[str, int]:
    return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("products", "price_points", "watches")}


def verify(path: Path, expected: dict[str, int] | None = None) -> tuple[bool, str]:
    """Open the copy on its own and check it. (ok, why-not)."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False, "integrity_check failed"
            got = _counts(c)
        finally:
            c.close()
    except sqlite3.DatabaseError as e:
        return False, f"cannot be opened: {e}"
    if expected and any(got[k] < expected[k] for k in expected):
        return False, f"copy is missing rows: {got} vs {expected}"
    return True, ""


def list_backups(dest: str | Path | None = None) -> list[Path]:
    return sorted(_dir(dest).glob("deals-*.db"))


def rotate(dest: str | Path | None = None, keep: int | None = None) -> list[Path]:
    """Delete the oldest copies beyond `keep`. Returns what was removed."""
    keep = config.BACKUP_KEEP if keep is None else keep
    files = list_backups(dest)
    doomed = files[:-keep] if keep > 0 and len(files) > keep else []
    for f in doomed:
        f.unlink()
    return doomed


def run_backup(dest: str | Path | None = None, keep: int | None = None,
               now: dt.datetime | None = None) -> Path | None:
    """One verified copy of the live database, then rotation. None on failure,
    and in that case nothing is rotated: never delete an old good copy to make
    room for a bad one."""
    folder = _dir(dest)
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    final = folder / f"deals-{stamp}.db"
    tmp = folder / f".deals-{stamp}.db.partial"
    src = db.conn()
    expected = _counts(src)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)          # online backup: consistent under writes
        finally:
            dst.close()
    except sqlite3.Error:
        tmp.unlink(missing_ok=True)
        return None
    ok, _why = verify(tmp, expected)
    if not ok:
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(final)               # atomic: a half-written copy is never listed
    rotate(folder, keep)
    return final


# ------------------------------------------------------------- export/import

def export_json(path: str | Path) -> dict[str, int]:
    """Portable dump of the tables above. Returns row counts."""
    payload: dict[str, Any] = {"format": 1, "schema_version": db.LATEST_VERSION,
                               "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
                               "tables": {}}
    counts = {}
    for t in EXPORT_TABLES:
        rows = [dict(r) for r in db.conn().execute(f"SELECT * FROM {t}")]
        payload["tables"][t] = rows
        counts[t] = len(rows)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False))
    return counts


def import_json(path: str | Path, replace: bool = False) -> dict[str, int]:
    """Load an export. By default rows already present are left alone, so
    importing twice, or into a database that kept collecting, is harmless.
    `replace=True` empties the tables first."""
    data = json.loads(Path(path).read_text())
    if data.get("format") != 1:
        raise ValueError(f"unknown export format: {data.get('format')!r}")
    c = db.conn()
    added: dict[str, int] = {}
    with c:
        if replace:
            for t in reversed(EXPORT_TABLES):
                c.execute(f"DELETE FROM {t}")
        for t in EXPORT_TABLES:
            rows = data["tables"].get(t, [])
            n = 0
            for r in rows:
                cols = list(r)
                cur = c.execute(
                    f"INSERT OR IGNORE INTO {t}({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})", [r[k] for k in cols])
                n += cur.rowcount
            added[t] = n
    return added


def restore(backup_path: str | Path, target: str | Path | None = None,
            force: bool = False) -> Path:
    """Copy a verified backup over the database file. Refuses to overwrite an
    existing database unless `force`, and even then keeps a safety copy."""
    src = Path(backup_path)
    ok, why = verify(src)
    if not ok:
        raise ValueError(f"backup {src.name} is not usable: {why}")
    dst = Path(target or config.DB_PATH)
    if dst.exists():
        if not force:
            raise FileExistsError(f"{dst} exists; pass force=True to replace it")
        shutil.copy2(dst, dst.with_name(dst.name + ".before-restore"))
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    for suffix in ("-wal", "-shm"):
        Path(str(dst) + suffix).unlink(missing_ok=True)
    shutil.copy2(src, dst)
    return dst


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="game-deals database backups")
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("backup", help="verified copy + rotation (default)")
    b.add_argument("--dir"), b.add_argument("--keep", type=int)
    sub.add_parser("list")
    e = sub.add_parser("export"); e.add_argument("file")
    i = sub.add_parser("import"); i.add_argument("file")
    i.add_argument("--replace", action="store_true")
    r = sub.add_parser("restore"); r.add_argument("file")
    r.add_argument("--force", action="store_true")
    a = ap.parse_args()

    if a.cmd == "list":
        for f in list_backups():
            print(f"{f.name}  {f.stat().st_size // 1024} KB")
    elif a.cmd == "export":
        print(export_json(a.file))
    elif a.cmd == "import":
        print(import_json(a.file, a.replace))
    elif a.cmd == "restore":
        print("restored to", restore(a.file, force=a.force))
    else:
        path = run_backup(getattr(a, "dir", None), getattr(a, "keep", None))
        if path is None:
            raise SystemExit("backup FAILED verification; nothing was rotated")
        print("backup ok:", path)


if __name__ == "__main__":
    main()
