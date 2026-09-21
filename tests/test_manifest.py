"""Fixtures are evidence. This guards their provenance.

A fixture marked "live" was recorded by scripts/probe.py from a real endpoint.
If someone edits one by hand its hash stops matching and this test fails, so a
comfortable but false fixture cannot slip in as if it were real.
"""
import hashlib
import json
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"
MANIFEST = json.loads((FIX / "MANIFEST.json").read_text())["fixtures"]
HARDENED = ("nintendo/", "playstation/", "promobit/")


def _files():
    return {str(p.relative_to(FIX)) for p in FIX.rglob("*")
            if p.is_file() and p.name != "MANIFEST.json"}


def test_every_fixture_is_listed_and_every_entry_exists():
    assert _files() == set(MANIFEST)


def test_hashes_match_so_nothing_was_edited_by_hand():
    for name, meta in MANIFEST.items():
        # Bytes, not text: text mode would turn CRLF into LF and change the hash.
        body = (FIX / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == meta["sha256"], \
            f"{name} was modified after being recorded"


def test_live_entries_carry_their_provenance():
    for name, meta in MANIFEST.items():
        assert meta["status"] in ("live", "synthetic"), name
        if meta["status"] == "live":
            assert meta["url"].startswith("https://"), name
            assert meta["recorded_at"], name
            assert meta["http_status"] == 200, name
        else:
            assert meta.get("note"), f"{name}: synthetic fixtures must say why"


def test_hardened_sources_use_live_fixtures_only():
    """Provider hardening is worthless if it was checked against invented data."""
    for name, meta in MANIFEST.items():
        if name.startswith(HARDENED):
            assert meta["status"] == "live", f"{name} is not a live recording"
