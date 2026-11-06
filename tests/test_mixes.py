"""Mixes that learn (PLAN 0.14.4): few plays, skips, the hour of day, radio from a song."""

import json
import time

from bakin import music


def songs(n, **kw):
    return [{"id": f"s{i}", "name": f"Artista {i % 5} - Tema {i}.opus", "artist": f"Artista {i % 5}",
             "title": f"Tema {i}", "date": i, "plays": 0, "last_played": 0, "fav": False, **kw} for i in range(n)]


def daily(items, **kw):
    return next(m for m in music.mixes(items, now=kw.pop("now", 1_800_000_000), log=kw.pop("log", []),
                                       skipped=kw.pop("skipped", {})) if m["id"] == "daily")


def test_with_few_plays_the_daily_mix_leans_on_new_and_favourite_songs():
    items = songs(400)
    for i in items[:20]:
        i["fav"] = True
    picked = set(daily(items)["ids"])
    newest = {f"s{i}" for i in range(300, 400)}
    favs = {f"s{i}" for i in range(20)}
    # 120 of 400 songs are new or favourite (30 %): far more than that in the mix
    assert len(picked & (newest | favs)) > 25, len(picked & (newest | favs))


def test_skipped_songs_weigh_less():
    items = songs(200)
    skipped = {f"s{i}": {"n": 5, "last": 0} for i in range(100)}
    picked = daily(items, skipped=skipped)["ids"]
    assert sum(1 for x in picked if int(x[1:]) < 100) < 15


def test_the_mix_spreads_artists():
    ids = daily(songs(300))["ids"]
    by_id = {i["id"]: i for i in songs(300)}
    same = sum(1 for a, b in zip(ids, ids[1:]) if by_id[a]["artist"] == by_id[b]["artist"])
    assert same == 0


def test_what_plays_at_this_hour_weighs_more():
    now = time.mktime((2026, 11, 7, 22, 0, 0, 0, 0, -1))  # night
    items = songs(300)
    for i in items:
        i["plays"] = 1  # 300 plays: the regular mix
    night = [(f"s{i}", now - 86400 * d) for i in range(30) for d in range(1, 4)]
    picked = daily(items, now=now, log=night)["ids"]
    assert sum(1 for x in picked if int(x[1:]) < 30) > 7  # ~3.5 by chance, ~13 expected


def test_plays_and_skips_are_logged_and_added_up():
    music.record_play("a")
    music.record_play("a")
    assert [i for i, _ in music.play_log()] == ["a", "a"]
    pending = music._file("skips-pending.jsonl")
    pending.write_text("\n".join(json.dumps({"id": "b", "at": 5}) for _ in range(3)), encoding="utf-8")
    assert music.skips()["b"]["n"] == 3 and not pending.exists()
    assert music.skips()["b"]["n"] == 3  # added once


def test_radio_starts_with_the_song_then_its_artist_and_who_plays_near_it():
    items = songs(200)
    seed = items[7]  # Artista 2
    log = [("s7", 1000.0), ("s3", 1500.0), ("s8", 1800.0)]  # Artista 3 played close to Artista 2
    out = music.radio(items, "s7", now=2000.0, log=log)
    assert out[0]["id"] == "s7" and len(out) == 50 and len({i["id"] for i in out}) == 50
    artists = [i["artist"] for i in out]
    assert artists.count(seed["artist"]) >= 10
    assert artists.count("Artista 3") >= 10
    assert music.radio(items, "nope") == []
