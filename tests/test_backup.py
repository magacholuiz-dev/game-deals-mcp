import sqlite3
import threading

import pytest

from game_deals import backup, db


@pytest.fixture()
def populated(scratch_db):
    db.upsert_product("p1", "Jogo Um")
    db.upsert_product("p2", "Jogo Dois")
    db.add_alias("p1", "nintendo", "slug-1")
    for i in range(50):
        db.record("p1", "nintendo", "Nintendo eShop", 10000 + i, None, "BRL", True, "", ts=1000 + i)
    db.add_watch("p1", "any_drop", None)
    return scratch_db


def test_backup_is_a_verified_consistent_copy(populated, tmp_path):
    path = backup.run_backup(tmp_path / "bk")
    assert path and path.exists() and path.name.startswith("deals-")
    c = sqlite3.connect(path)
    assert c.execute("SELECT COUNT(*) FROM price_points").fetchone()[0] == 50
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == db.LATEST_VERSION
    assert not list((tmp_path / "bk").glob("*.partial"))       # no half-written file left


def test_backup_is_consistent_while_another_thread_writes(populated, tmp_path):
    stop = threading.Event()

    def writer():
        c = sqlite3.connect(populated.config.DB_PATH if hasattr(populated, "config") else db.config.DB_PATH)
        i = 0
        while not stop.is_set():
            c.execute("INSERT OR IGNORE INTO price_points VALUES ('p1','x','S',?,NULL,'BRL',1,'',?)",
                      (999, 50_000 + i))
            c.commit()
            i += 1
        c.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        path = backup.run_backup(tmp_path / "bk")
    finally:
        stop.set()
        t.join()
    assert path is not None
    assert backup.verify(path)[0]


def test_rotation_keeps_the_newest_n(populated, tmp_path):
    import datetime as dt
    d = tmp_path / "bk"
    for day in range(1, 21):
        backup.run_backup(d, keep=14, now=dt.datetime(2026, 8, day, 3, 0, 0))
    files = [f.name for f in backup.list_backups(d)]
    assert len(files) == 14
    assert files[0] == "deals-20260807-030000.db" and files[-1] == "deals-20260820-030000.db"


def test_a_failed_verification_keeps_old_backups_and_lists_nothing_new(populated, tmp_path, monkeypatch):
    d = tmp_path / "bk"
    import datetime as dt
    good = backup.run_backup(d, keep=1, now=dt.datetime(2026, 8, 1, 3, 0, 0))
    monkeypatch.setattr(backup, "verify", lambda p, e=None: (False, "corrupt"))
    assert backup.run_backup(d, keep=1, now=dt.datetime(2026, 8, 2, 3, 0, 0)) is None
    assert backup.list_backups(d) == [good]           # the good one was NOT rotated away
    assert not list(d.glob("*.partial"))


def test_export_import_round_trip(populated, tmp_path):
    counts = backup.export_json(tmp_path / "x.json")
    assert counts["price_points"] == 50 and counts["products"] == 2
    # fresh empty database
    db._conn.close()
    db._conn = None
    db.config.DB_PATH = str(tmp_path / "other.db")
    added = backup.import_json(tmp_path / "x.json")
    assert added["price_points"] == 50 and added["products"] == 2
    assert db.conn().execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 1
    # importing again changes nothing
    assert backup.import_json(tmp_path / "x.json")["price_points"] == 0


def test_restore_refuses_to_overwrite_without_force_and_keeps_a_safety_copy(populated, tmp_path):
    bk = backup.run_backup(tmp_path / "bk")
    target = tmp_path / "restore.db"
    target.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        backup.restore(bk, target)
    backup.restore(bk, target, force=True)
    assert (tmp_path / "restore.db.before-restore").read_bytes() == b"precious"
    assert backup.verify(target)[0]


def test_restore_rejects_a_corrupt_backup(tmp_path):
    bad = tmp_path / "deals-bad.db"
    bad.write_bytes(b"not a database")
    with pytest.raises(ValueError, match="not usable"):
        backup.restore(bad, tmp_path / "t.db")
