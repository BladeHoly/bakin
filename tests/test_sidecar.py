"""Engine sidecar protocol (bakin.sidecar)."""

import io
import json
import time

from bakin import sidecar


def run(lines, timeout=5.0):
    """Feed request lines to the sidecar; return the protocol lines it wrote."""
    out = io.StringIO()
    sidecar.serve(io.StringIO("".join(json.dumps(l) + "\n" if not isinstance(l, str) else l for l in lines)), out)
    deadline = time.time() + timeout
    want = sum(1 for l in lines if isinstance(l, dict) and "id" in l)
    while time.time() < deadline:
        got = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
        if sum(1 for g in got if "id" in g) >= want:
            return got
        time.sleep(0.02)
    return [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]


def by_id(lines):
    return {l["id"]: l for l in lines if "id" in l}


def test_reply_not_lost_when_input_closes_right_after():
    out = io.StringIO()
    sidecar.serve(io.StringIO(json.dumps({"id": 9, "method": "get_state"}) + "\n"), out)
    replies = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
    assert any(r.get("id") == 9 and "result" in r for r in replies)


def test_ready_first_then_answers():
    lines = run([{"id": 1, "method": "job_running"}])
    assert lines[0].get("ready") is True
    assert by_id(lines)[1] == {"id": 1, "result": False}


def test_only_whitelisted_methods():
    lines = by_id(run([{"id": 1, "method": "_run"}, {"id": 2, "method": "__init__"},
                       {"id": 3, "method": "get_state", "params": "nope"}]))
    assert all("error" in lines[i] for i in (1, 2, 3))


def test_bad_arguments_and_garbage_lines():
    lines = by_id(run(["not json\n", "[1,2]\n", {"id": 4, "method": "start", "params": {"zzz": 1}}]))
    assert lines[4]["error"].startswith("bad arguments")


def test_start_validation_over_the_wire():
    lines = by_id(run([{"id": 5, "method": "start",
                        "params": {"mode": "download", "url": "esto no es un enlace"}}]))
    assert lines[5]["result"]["ok"] is False and lines[5]["result"]["field"] == "url"


def test_a_failing_method_answers_with_its_error_class():
    class Boom:
        def get_state(self):
            raise RuntimeError("disk on fire")
    out = io.StringIO()
    sidecar.handle(Boom(), sidecar.Channel(out), {"id": 8, "method": "get_state"})
    assert json.loads(out.getvalue()) == {"id": 8, "error": "RuntimeError: disk on fire"}


def test_requests_without_params_or_id():
    class Svc:
        def job_running(self):
            return True
    out = io.StringIO()
    sidecar.handle(Svc(), sidecar.Channel(out), {"method": "job_running", "params": None})
    assert json.loads(out.getvalue()) == {"id": None, "result": True}
