import datetime as dt

from game_deals import events as ev

D = dt.date


def test_black_friday_is_computed_by_rule():
    assert ev.black_friday(2025) == D(2025, 11, 28)
    assert ev.black_friday(2026) == D(2026, 11, 27)      # the date used everywhere
    assert ev.black_friday(2027) == D(2027, 11, 26)
    assert all(ev.black_friday(y).weekday() == 4 for y in range(2020, 2031))


def test_steam_autumn_is_ten_days_away_on_the_day_this_was_written():
    today = D(2026, 9, 21)
    soon = ev.upcoming(today, 30, platform="pc")
    assert [e.key for e in soon] == ["steam_autumn_2026"]
    assert soon[0].days_until(today) == 10


def test_black_friday_enters_the_thirty_day_window_exactly_on_time():
    assert "black_friday_2026" not in [e.key for e in ev.upcoming(D(2026, 10, 27), 30)]
    assert "black_friday_2026" in [e.key for e in ev.upcoming(D(2026, 10, 28), 30)]


def test_running_event_counts_as_zero_days_and_over_is_negative():
    bf = next(e for e in ev.all_events() if e.key == "black_friday_2026")
    assert bf.days_until(D(2026, 11, 28)) == 0
    assert bf.days_until(D(2026, 12, 3)) == -3      # ended Nov 30
    assert bf.days_until(D(2026, 11, 20)) == 7


def test_launch_is_not_a_sale_and_estimates_can_be_excluded():
    assert "gta6_launch" not in [e.key for e in ev.upcoming(D(2026, 11, 1), 60)]
    launch = ev.upcoming(D(2026, 11, 1), 60, kind=ev.LAUNCH)
    assert [e.key for e in launch] == ["gta6_launch"]
    est = ev.upcoming(D(2027, 3, 1), 30)
    assert "consumer_week_2027" in [e.key for e in est]
    assert "consumer_week_2027" not in [
        e.key for e in ev.upcoming(D(2027, 3, 1), 30, include_estimated=False)]


def test_every_event_declares_how_much_to_trust_it():
    assert {e.certainty for e in ev.all_events()} <= {ev.RULE, ev.REPORTED, ev.ESTIMATED}
    steam = [e for e in ev.all_events() if e.key.startswith("steam_") and
             e.certainty == ev.REPORTED]
    assert steam and all("não conferid" in e.note for e in steam
                         if e.key != "steam_summer_2026")


def test_scheduler_boost_window_ignores_estimates():
    assert ev.near_event(D(2026, 11, 25))            # 2 days before Black Friday
    assert not ev.near_event(D(2026, 11, 10))
    assert not ev.near_event(D(2027, 3, 8))          # only an ESTIMATED event
