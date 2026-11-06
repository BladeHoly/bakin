"""MusicBrainz lookups and Cover Art Archive covers, with no network:
recorded (hand-written) JSON and a fake session."""

import copy
import json
import threading
from pathlib import Path

import pytest
import requests

from bakin import __version__, musicbrainz as mb

FIX = Path(__file__).parent / "fixtures" / "musicbrainz"
SEARCH = json.loads((FIX / "recording_search.json").read_text(encoding="utf-8"))
EMPTY = json.loads((FIX / "recording_empty.json").read_text(encoding="utf-8"))
RELEASE = "33333333-3333-4333-8333-333333333333"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 1000
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000


class Resp:
    def __init__(self, status=200, data=None, body=b"", headers=None):
        self.status_code = status
        self._data = data
        self.body = body
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses, error=None):
        self.responses = list(responses)
        self.error = error
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if self.error:
            raise self.error
        return self.responses.pop(0)


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


@pytest.fixture(autouse=True)
def fake_limiter(monkeypatch):
    fc = FakeClock()
    monkeypatch.setattr(mb, "_limiter", mb.Limiter(clock=fc.clock, sleep=fc.sleep))
    return fc


# -------------------------------------------------------------------- query

def test_query_plain_with_duration():
    assert mb.build_query("Under Pressure", "Queen", 248.4) == \
        'recording:"Under Pressure" AND artist:"Queen" AND dur:[245400 TO 251400]'


def test_query_without_duration_and_whitespace():
    assert mb.build_query("  Under\tPressure ", "Queen", None) == 'recording:"Under Pressure" AND artist:"Queen"'
    assert "dur:" not in mb.build_query("x", "y", 0)
    assert "dur:" not in mb.build_query("x", "y", "nonsense")


def test_query_low_duration_never_negative():
    assert mb.build_query("x", "y", 1.0).endswith("dur:[0 TO 4000]")


def test_query_escapes_lucene_and_quotes():
    q = mb.build_query('Say "Hi" (Remix) [feat. A/B]', "AC/DC & Co! ~*?:^\\", None)
    assert q == ('recording:"Say \\"Hi\\" \\(Remix\\) \\[feat. A\\/B\\]" AND '
                 'artist:"AC\\/DC \\& Co\\! \\~\\*\\?\\:\\^\\\\"')
    # no unescaped quote can close the phrase early
    inner = q[len('recording:"'):q.index('" AND artist')]
    assert '"' not in inner.replace('\\"', "")


def test_lookup_request_shape():
    s = Session(Resp(data=SEARCH))
    mb.lookup("Under Pressure", "Queen", 248.0, session=s)
    url, kw = s.calls[0]
    assert url == "https://musicbrainz.org/ws/2/recording"
    assert kw["params"] == {"query": 'recording:"Under Pressure" AND artist:"Queen" AND dur:[245000 TO 251000]',
                            "fmt": "json", "limit": 5}
    assert kw["headers"]["User-Agent"] == f"Bakin/{__version__} ( https://github.com/BladeHoly/bakin )"
    assert set(kw["headers"]) <= {"User-Agent", "Accept"}  # nothing else about the user
    assert kw["timeout"]


def test_lookup_skips_request_without_title_or_artist():
    s = Session()
    assert mb.lookup("", "Queen", 200, session=s) is None
    assert mb.lookup("Song", "  ", 200, session=s) is None
    assert s.calls == []


# -------------------------------------------------------------- acceptance

def test_lookup_best_match():
    s = Session(Resp(data=SEARCH))
    assert mb.lookup("Under Pressure", "Queen", 248.0, session=s) == {
        "mbid": "b1a9c0e9-d987-4042-ae91-78d6a3267d69",
        "title": "Under Pressure",
        "artist": "Queen & David Bowie",
        "album": "Hot Space",
        "year": 1982,
        "release_mbid": RELEASE,
    }
    assert s.responses == [] and s.calls


def test_duration_outside_slack_falls_to_next_good_result():
    # 236 s: the studio take (248 s) is 12 s off, the live one (236 s, score 95) fits
    r = mb.lookup("Under Pressure", "Queen", 236.5, session=Session(Resp(data=SEARCH)))
    assert r["mbid"] == "c2c2c2c2-0000-4000-8000-00000000000c"
    assert r["album"] == "Live Magic" and r["artist"] == "Queen"


def test_duration_exactly_at_slack_accepted_and_beyond_rejected():
    data = copy.deepcopy(SEARCH)
    data["recordings"] = data["recordings"][:1]
    assert mb.lookup("Under Pressure", "Queen", 251.0, session=Session(Resp(data=data)))
    assert mb.lookup("Under Pressure", "Queen", 251.001, session=Session(Resp(data=data))) is None


def test_no_duration_takes_top_score():
    r = mb.lookup("Under Pressure", "Queen", None, session=Session(Resp(data=SEARCH)))
    assert r["mbid"] == "b1a9c0e9-d987-4042-ae91-78d6a3267d69"


def test_low_score_rejected():
    data = copy.deepcopy(SEARCH)
    data["recordings"] = [data["recordings"][2]]  # score 62
    assert mb.lookup("Under Pressure", "Queen", None, session=Session(Resp(data=data))) is None
    data["recordings"][0]["score"] = 89
    assert mb.lookup("Under Pressure", "Queen", None, session=Session(Resp(data=data))) is None
    data["recordings"][0]["score"] = "90"  # older servers send it as a string
    assert mb.lookup("Under Pressure", "Queen", None, session=Session(Resp(data=data)))["mbid"]


def test_missing_length_rejected_when_duration_known():
    data = copy.deepcopy(SEARCH)
    data["recordings"] = data["recordings"][:1]
    del data["recordings"][0]["length"]
    assert mb.lookup("Under Pressure", "Queen", 248, session=Session(Resp(data=data))) is None
    assert mb.lookup("Under Pressure", "Queen", None, session=Session(Resp(data=data)))


def test_no_results_is_none():
    assert mb.lookup("Nada", "Nadie", 100, session=Session(Resp(data=EMPTY))) is None


def test_recording_without_releases():
    data = {"recordings": [{"id": "x-1", "score": 100, "title": "T", "length": 100000,
                            "first-release-date": "2004-02",
                            "artist-credit": [{"name": "A", "joinphrase": ""}]}]}
    assert mb.lookup("T", "A", 100, session=Session(Resp(data=data))) == {
        "mbid": "x-1", "title": "T", "artist": "A", "album": None, "year": 2004, "release_mbid": None}


# ----------------------------------------------------------------- releases

def test_best_release_order():
    rels = SEARCH["recordings"][0]["releases"]
    assert mb.best_release(rels)["id"] == RELEASE
    # without the plain album: single beats compilation
    assert mb.best_release([r for r in rels if r["title"] != "Hot Space"])["id"] == \
        "22222222-2222-4222-8222-222222222222"
    # an official release beats a bootleg, whatever its type
    assert mb.best_release([rels[0], rels[3]])["id"] == "11111111-1111-4111-8111-111111111111"
    # an undated release goes after dated ones
    a = {"id": "a", "title": "A", "status": "Official", "release-group": {"primary-type": "Album"}}
    b = {"id": "b", "title": "B", "status": "Official", "release-group": {"primary-type": "Album"}, "date": "2001"}
    assert mb.best_release([a, b])["id"] == "b"
    assert mb.best_release([]) is None


def test_artist_credit_joining():
    assert mb.join_credit([{"name": "A", "joinphrase": " feat. "}, {"name": "B", "joinphrase": " & "},
                           {"name": "C"}]) == "A feat. B & C"
    # the credited name wins over the artist's own name; falls back to it
    assert mb.join_credit([{"name": "Ye", "artist": {"name": "Kanye West"}}]) == "Ye"
    assert mb.join_credit([{"artist": {"name": "Björk"}}]) == "Björk"
    assert mb.join_credit(None) == ""


# ------------------------------------------------------------------ limiter

def test_limiter_spaces_requests(fake_limiter):
    s = Session(*[Resp(data=EMPTY) for _ in range(3)])
    for _ in range(3):
        mb.lookup("a", "b", None, session=s)
    assert fake_limiter.slept == [1.0, 1.0]


def test_limiter_no_wait_after_gap(fake_limiter):
    lim = mb._limiter
    lim.wait()
    fake_limiter.now += 5
    lim.wait()
    fake_limiter.now += 0.4
    lim.wait()
    assert fake_limiter.slept == [pytest.approx(0.6)]


def test_limiter_shared_across_threads():
    clock_lock = threading.Lock()
    now = [0.0]
    stamps = []

    def clock():
        with clock_lock:
            return now[0]

    def sleep(sec):
        with clock_lock:
            now[0] += sec

    lim = mb.Limiter(clock=clock, sleep=sleep)

    def worker():
        lim.wait()
        stamps.append(clock())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    assert all(b - a >= 1.0 - 1e-9 for a, b in zip(stamps, stamps[1:]))


def test_cover_uses_the_same_limiter(fake_limiter):
    s = Session(Resp(data=EMPTY), Resp(body=JPEG, headers={"Content-Type": "image/jpeg"}))
    mb.lookup("a", "b", None, session=s)
    mb.cover(RELEASE, session=s)
    assert fake_limiter.slept == [1.0]


# ------------------------------------------------------------------- errors

@pytest.mark.parametrize("status", [503, 429])
def test_busy(status):
    r = Resp(status=status)
    with pytest.raises(mb.Busy, match="MusicBrainz pidió esperar"):
        mb.lookup("a", "b", None, session=Session(r))
    assert r.closed
    with pytest.raises(mb.Busy):
        mb.cover(RELEASE, session=Session(Resp(status=status)))
    assert issubclass(mb.Busy, mb.MBError)


def test_busy_is_not_retried():
    s = Session(Resp(status=503), Resp(data=SEARCH))
    with pytest.raises(mb.Busy):
        mb.lookup("Under Pressure", "Queen", None, session=s)
    assert len(s.calls) == 1


def test_network_errors():
    with pytest.raises(mb.MBError, match="No se pudo conectar"):
        mb.lookup("a", "b", None, session=Session(error=requests.ConnectionError("boom")))
    with pytest.raises(mb.MBError, match="tardó"):
        mb.lookup("a", "b", None, session=Session(error=requests.ReadTimeout("slow")))
    with pytest.raises(mb.MBError, match="No se pudo conectar"):
        mb.cover(RELEASE, session=Session(error=OSError("down")))


def test_bad_status_and_bad_json():
    with pytest.raises(mb.MBError, match="500"):
        mb.lookup("a", "b", None, session=Session(Resp(status=500)))
    with pytest.raises(mb.MBError, match="no se entiende"):
        mb.lookup("a", "b", None, session=Session(Resp(data=None)))


# -------------------------------------------------------------------- cover

def test_cover_ok():
    s = Session(Resp(body=JPEG, headers={"Content-Type": "image/jpeg", "Content-Length": str(len(JPEG))}))
    assert mb.cover(RELEASE, session=s) == JPEG
    url, kw = s.calls[0]
    assert url == f"https://coverartarchive.org/release/{RELEASE}/front-500"
    assert kw["allow_redirects"] is True and kw["stream"] is True
    assert kw["headers"]["User-Agent"].startswith("Bakin/")
    assert mb.cover(RELEASE.upper(), session=Session(Resp(body=PNG, headers={"Content-Type": "image/png; x=1"}))) == PNG


def test_cover_404_is_none():
    r = Resp(status=404)
    assert mb.cover(RELEASE, session=Session(r)) is None
    assert r.closed


def test_cover_other_error():
    with pytest.raises(mb.MBError, match="500"):
        mb.cover(RELEASE, session=Session(Resp(status=500)))


def test_cover_rejects_wrong_type():
    assert mb.cover(RELEASE, session=Session(Resp(body=JPEG, headers={"Content-Type": "image/gif"}))) is None
    assert mb.cover(RELEASE, session=Session(Resp(body=JPEG, headers={}))) is None
    # says JPEG but isn't one
    assert mb.cover(RELEASE, session=Session(Resp(body=b"<html>", headers={"Content-Type": "image/jpeg"}))) is None


def test_cover_size_cap():
    big = b"\xff\xd8\xff" + b"\x00" * mb.COVER_MAX
    # declared too big: not even read
    r = Resp(body=big, headers={"Content-Type": "image/jpeg", "Content-Length": str(len(big))})
    assert mb.cover(RELEASE, session=Session(r)) is None and r.closed
    # undeclared, grows past the cap while streaming
    assert mb.cover(RELEASE, session=Session(Resp(body=big, headers={"Content-Type": "image/jpeg"}))) is None
    exact = b"\xff\xd8\xff" + b"\x00" * (mb.COVER_MAX - 3)
    assert mb.cover(RELEASE, session=Session(Resp(body=exact, headers={"Content-Type": "image/jpeg"}))) == exact


def test_cover_invalid_mbid_sends_nothing():
    s = Session()
    for bad in ("", None, "../../etc", "33333333-3333-4333-8333-33333333333g"):
        assert mb.cover(bad, session=s) is None
    assert s.calls == []
