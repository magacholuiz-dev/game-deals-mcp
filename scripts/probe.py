"""Record live responses as test fixtures, and detect format drift.

    uv run python scripts/probe.py nintendo              # fetch, summarise
    uv run python scripts/probe.py nintendo --record     # also write fixtures
    uv run python scripts/probe.py --list

The test suite never touches the network. Fixtures are the only bridge, and this
script is the only thing allowed to create them. Every fixture is listed in
tests/fixtures/MANIFEST.json with the URL it came from, the date, and a sha256,
and tests/test_manifest.py fails if a file was edited by hand or is unlisted.

Requests go through game_deals.http, so robots.txt, the per-host rate limit and
the honest User-Agent apply here as everywhere else.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from game_deals import http                      # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
MANIFEST = FIX / "MANIFEST.json"


# ------------------------------------------------------------------- trimmers
# Pages are 0.5 to 4 MB. A fixture keeps only what the parser reads, and says so
# in the manifest, so the repository stays small without pretending the trimmed
# file is the whole response.

def ld_json_only(text: str) -> str:
    blocks = re.findall(r'<script[^>]*application/ld\+json[^>]*>.*?</script>',
                        text, re.S)
    return "\n".join(blocks)


def ps_state(text: str) -> str:
    keep = re.findall(r'<script[^>]*id="(?:mfe-jsonld-tags|env:[^"]+)"[^>]*>.*?</script>',
                      text, re.S)
    return "\n".join(keep)


def next_data_only(text: str) -> str:
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>.*?</script>', text, re.S)
    return m.group(0) if m else ""


def sitemap_filtered(keywords: tuple[str, ...]) -> Callable[[str], str]:
    def trim(text: str) -> str:
        urls = re.findall(r"<loc>(.*?)</loc>", text)
        keep = [u for u in urls if any(k in u for k in keywords)]
        body = "\n".join(f"  <url><loc>{u}</loc></url>" for u in keep)
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                f"{body}\n</urlset>\n")
    return trim


def identity(text: str) -> str:
    return text


@dataclass
class Target:
    name: str                       # file name inside tests/fixtures/<source>/
    url: str
    trim: Callable[[str], str] = identity
    params: dict = field(default_factory=dict)
    trimmed: str = ""               # human note stored in the manifest


SITEMAP_KEEP = ("mario-kart-world", "mario-kart-8", "no-mans-sky", "rune-factory",
                "hollow-knight", "donkey-kong-bananza", "the-duskbloods",
                "zelda-breath", "super-mario-odyssey", "hades-", "elden-ring")

PROBES: dict[str, list[Target]] = {
    "nintendo": [
        Target("product_mario_kart_world_switch2.html",
               "https://www.nintendo.com/pt-br/store/products/mario-kart-world-switch-2/",
               ld_json_only, trimmed="JSON-LD blocks only"),
        Target("product_no_mans_sky_switch2_edition.html",
               "https://www.nintendo.com/pt-br/store/products/"
               "no-mans-sky-nintendo-switch-2-edition-switch-2/",
               ld_json_only, trimmed="JSON-LD blocks only"),
        Target("product_zelda_botw_switch1.html",
               "https://www.nintendo.com/pt-br/store/products/"
               "the-legend-of-zelda-breath-of-the-wild-switch/",
               ld_json_only, trimmed="JSON-LD blocks only"),
        Target("sitemap_pt_br_store.xml",
               "https://www.nintendo.com/pt-br/store/sitemap.xml",
               sitemap_filtered(SITEMAP_KEEP),
               trimmed="URLs filtered to the keywords the tests use"),
        Target("price_br_found.json",
               "https://api.ec.nintendo.com/v1/price",
               params={"country": "BR", "lang": "pt",
                       "ids": "70010000095431,70010000099217"}),
        Target("price_br_eu_nsuid_not_found.json",
               "https://api.ec.nintendo.com/v1/price",
               params={"country": "BR", "lang": "pt", "ids": "70010000096802"}),
        Target("price_br_eu_nsuid_nms.json",
               "https://api.ec.nintendo.com/v1/price",
               params={"country": "BR", "lang": "pt", "ids": "70010000099138"}),
        Target("price_br_zelda_switch1.json",
               "https://api.ec.nintendo.com/v1/price",
               params={"country": "BR", "lang": "pt", "ids": "70010000000025"}),
        Target("solr_switch2_native.json",
               "https://search.nintendo-europe.com/en/select",
               params={"q": "mario kart world", "fq": "type:GAME", "wt": "json",
                       "rows": 3}),
        Target("solr_switch2_edition.json",
               "https://search.nintendo-europe.com/en/select",
               params={"q": "no man's sky", "fq": "type:GAME AND system_type:*switch2*",
                       "wt": "json", "rows": 3}),
        Target("robots.txt", "https://www.nintendo.com/robots.txt"),
    ],
    "playstation": [
        Target("concept_gta6_editions.html",
               "https://store.playstation.com/pt-br/concept/10000730",
               ps_state, trimmed="JSON-LD and env:* state scripts only"),
        Target("concept_ronin_on_sale.html",
               "https://store.playstation.com/pt-br/concept/10003386",
               ps_state, trimmed="JSON-LD and env:* state scripts only"),
        Target("product_gta6_standard.html",
               "https://store.playstation.com/pt-br/product/"
               "EP1004-PPSA01547_00-GTAVISTANDARD001",
               ps_state, trimmed="JSON-LD and env:* state scripts only"),
        Target("category_deals_shell.html",
               "https://store.playstation.com/pt-br/category/"
               "3f772501-f6f8-49b7-abac-874a88ca4897/1",
               next_data_only, trimmed="__NEXT_DATA__ only"),
        Target("robots.txt", "https://store.playstation.com/robots.txt"),
    ],
    "promobit": [
        Target("listing_promocoes_games.html",
               "https://www.promobit.com.br/promocoes/games/",
               next_data_only, trimmed="__NEXT_DATA__ only"),
        Target("robots_www.txt", "https://www.promobit.com.br/robots.txt"),
        Target("robots_api.txt", "https://api.promobit.com.br/robots.txt"),
    ],
}


# ------------------------------------------------------------------- manifest

def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text())
    return {"schema": 1, "fixtures": {}}


def save_manifest(m: dict) -> None:
    m["fixtures"] = dict(sorted(m["fixtures"].items()))
    MANIFEST.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")


def fetch(t: Target):
    # robots=False only for the robots.txt files themselves.
    is_robots = t.url.endswith("/robots.txt")
    return http.get(t.url, params=t.params or None, robots=not is_robots)


def run(source: str, record: bool) -> int:
    manifest = load_manifest()
    bad = 0
    for t in PROBES[source]:
        label = f"{source}/{t.name}"
        try:
            r = fetch(t)
        except http.RobotsBlocked as e:
            print(f"  BLOCKED {label}: {e}")
            bad += 1
            continue
        except Exception as e:                       # noqa: BLE001
            print(f"  ERROR   {label}: {type(e).__name__}: {str(e)[:80]}")
            bad += 1
            continue
        body = t.trim(r.text)
        print(f"  {r.status_code}  {label:56} {len(r.content):>9}b -> {len(body):>7}b")
        if r.status_code != 200 or not body.strip():
            bad += 1
            continue
        if record:
            path = FIX / source / t.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body.encode("utf-8"))      # bytes: keep CRLF intact
            manifest["fixtures"][f"{source}/{t.name}"] = {
                "source": source, "url": r.url.__str__(), "status": "live",
                "recorded_at": dt.date.today().isoformat(),
                "http_status": r.status_code,
                "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "trimmed": t.trimmed or None,
            }
    if record:
        save_manifest(manifest)
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", choices=sorted(PROBES))
    ap.add_argument("--record", action="store_true",
                    help="write fixtures and update MANIFEST.json")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list or not a.source:
        for s, ts in PROBES.items():
            print(s)
            for t in ts:
                print("   ", t.name)
        return
    bad = run(a.source, a.record)
    if bad:
        sys.exit(f"{bad} probe(s) failed")


if __name__ == "__main__":
    main()
