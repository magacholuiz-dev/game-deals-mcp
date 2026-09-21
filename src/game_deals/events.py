"""Known sale and launch events, with how much we trust each date.

The recommendation engine reasons about "a big sale is close", so a wrong date
is worse than a missing one. Every event says where its date comes from:

    rule       computed from a stable rule (Black Friday is the Friday after
               the 4th Thursday of November);
    reported   published by Valve and repeated by several sites, but NOT checked
               against the Valve announcement itself: the announcement page did
               not return its text when this file was written (2026-09-21);
    estimated  the usual timing, no date announced yet. Shown, never trusted.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

SALE, LAUNCH = "sale", "launch"
MAJOR, MINOR = "major", "minor"
RULE, REPORTED, ESTIMATED = "rule", "reported", "estimated"

ALL_PLATFORMS = ("pc", "ps5", "switch", "switch2", "xbox")


@dataclass(frozen=True)
class Event:
    key: str
    name: str                    # Portuguese, shown to the user
    start: dt.date
    end: dt.date
    kind: str                    # sale | launch
    magnitude: str               # major | minor
    platforms: tuple[str, ...]
    certainty: str               # rule | reported | estimated
    note: str = ""

    def contains(self, day: dt.date) -> bool:
        return self.start <= day <= self.end

    def days_until(self, today: dt.date) -> int:
        """0 while it is running, negative once it is over."""
        if self.contains(today):
            return 0
        return (self.start - today).days if today < self.start \
            else -(today - self.end).days


def black_friday(year: int) -> dt.date:
    """Friday after the 4th Thursday of November (the day after Thanksgiving)."""
    first = dt.date(year, 11, 1)
    first_thursday = first + dt.timedelta(days=(3 - first.weekday()) % 7)
    return first_thursday + dt.timedelta(weeks=3, days=1)


def _black_friday_event(year: int) -> Event:
    bf = black_friday(year)
    return Event(f"black_friday_{year}", f"Black Friday {year}", bf,
                 bf + dt.timedelta(days=3),          # through Cyber Monday
                 SALE, MAJOR, ALL_PLATFORMS, RULE,
                 "Sexta após o Dia de Ação de Graças; vai até a Cyber Monday.")


def all_events() -> list[Event]:
    d = dt.date
    return sorted([
        _black_friday_event(2025),
        _black_friday_event(2026),
        _black_friday_event(2027),
        Event("steam_summer_2026", "Steam Summer Sale 2026", d(2026, 6, 25),
              d(2026, 7, 9), SALE, MAJOR, ("pc",), REPORTED),
        Event("steam_autumn_2026", "Steam Autumn Sale 2026", d(2026, 10, 1),
              d(2026, 10, 8), SALE, MINOR, ("pc",), REPORTED,
              "Datas reportadas por PC Gamer, SteamDB e outros; não conferidas "
              "no anúncio da Valve."),
        Event("steam_winter_2026", "Steam Winter Sale 2026", d(2026, 12, 17),
              d(2027, 1, 4), SALE, MAJOR, ("pc",), REPORTED,
              "Datas reportadas; não conferidas no anúncio da Valve."),
        Event("consumer_week_2027", "Semana do Consumidor 2027", d(2027, 3, 9),
              d(2027, 3, 15), SALE, MAJOR, ALL_PLATFORMS, ESTIMATED,
              "Costuma ocorrer em torno de 15 de março; sem data anunciada."),
        Event("steam_summer_2027", "Steam Summer Sale 2027", d(2027, 6, 24),
              d(2027, 7, 8), SALE, MAJOR, ("pc",), ESTIMATED,
              "Estimativa pelo padrão dos anos anteriores; a Valve ainda não anunciou."),
        Event("gta6_launch", "Lançamento de GTA VI", d(2026, 11, 19),
              d(2026, 11, 19), LAUNCH, MAJOR, ("ps5", "xbox"), REPORTED,
              "Data divulgada pela PlayStation. Lançamento não entra em desconto."),
    ], key=lambda e: e.start)


def upcoming(today: dt.date | None = None, within_days: int = 30,
             platform: str | None = None, kind: str = SALE,
             magnitude: str | None = None,
             include_estimated: bool = True) -> list[Event]:
    """Events running now or starting within `within_days`, soonest first."""
    today = today or dt.date.today()
    out = []
    for e in all_events():
        if e.kind != kind:
            continue
        if platform and platform not in e.platforms:
            continue
        if magnitude and e.magnitude != magnitude:
            continue
        if not include_estimated and e.certainty == ESTIMATED:
            continue
        if 0 <= e.days_until(today) <= within_days:
            out.append(e)
    return sorted(out, key=lambda e: (e.days_until(today), e.start))


def active(today: dt.date | None = None) -> list[Event]:
    today = today or dt.date.today()
    return [e for e in all_events() if e.kind == SALE and e.contains(today)]


def near_event(today: dt.date | None = None, days: int = 3) -> bool:
    """True from `days` before a major SALE event until it ends. The scheduler
    collects more often in this window, when prices actually move."""
    return bool(upcoming(today, days, magnitude=MAJOR, include_estimated=False))


def past_events(today: dt.date | None = None, key_prefix: str = "") -> list[Event]:
    today = today or dt.date.today()
    return [e for e in all_events()
            if e.kind == SALE and e.end < today and e.key.startswith(key_prefix)]
