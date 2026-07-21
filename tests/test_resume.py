"""Retry and resume: a long paper must survive transient TTS failures.

Before this, one 5xx anywhere in thousands of requests discarded every
completed unit — a thesis could never finish. These tests pin both halves:
transport errors are retried, and audio already narrated is resumed from
disk rather than re-synthesized.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import requests

import rhapsode as rh


def _units(n, pause=0.0):
    return [{"text": f"Sentence number {i} of the paper.", "kind": "body",
             "rects": [], "para_end": False, "pause": pause} for i in range(n)]


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} Server Error")
            err.response = self
            raise err

    def json(self):
        return self._payload


class _Session:
    """Fake Modal endpoint. `script` lists what each POST does, in order:
    an int status (>=400 raises), an exception instance, or None for OK."""

    def __init__(self, script=()):
        self.script = list(script)
        self.posts = 0
        self.texts_seen = []

    def post(self, endpoint, headers=None, timeout=None, json=None):
        outcome = self.script[self.posts] if self.posts < len(self.script) else None
        self.posts += 1
        texts = json["texts"]
        self.texts_seen.append(list(texts))
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            return _Resp({}, status=outcome)
        results = []
        for t in texts:
            # one sample per character: audio length identifies the text
            pcm = np.full(max(1, len(t)), 1000, dtype="<i2").tobytes()
            results.append({"pcm_b64": base64.b64encode(pcm).decode(),
                            "words": [{"w": t.split()[0], "t0": 0.0,
                                       "t1": 0.01}]})
        return _Resp({"sample_rate": 24000, "results": results})

    def close(self):
        pass


def _run(units, session, **kw):
    real_session, real_sleep = requests.Session, rh.time.sleep
    requests.Session = lambda: session
    rh.time.sleep = lambda s: None                 # no real backoff waits
    try:
        return list(rh._modal_unit_audio(
            units, "af_heart", 1.0,
            {"modal_endpoint": "https://fake", "backend": "modal"}, **kw))
    finally:
        requests.Session, rh.time.sleep = real_session, real_sleep


def test_transient_5xx_is_retried_and_output_is_identical():
    units = _units(6)
    clean = _run(units, _Session(), batch=2, lookahead=1)
    flaky = _Session([None, 500, None, None])      # 2nd request fails once
    got = _run(units, flaky, batch=2, lookahead=1)

    assert len(got) == len(clean) == 6
    for (u1, a1), (u2, a2) in zip(clean, got):
        assert u1["text"] == u2["text"]
        assert np.array_equal(a1[0][0], a2[0][0]), "retried audio must match"
    assert flaky.posts == 4, f"3 batches + 1 retry, got {flaky.posts}"


def test_connection_and_timeout_errors_are_retried():
    for exc in (requests.ConnectionError("reset"), requests.Timeout("slow")):
        s = _Session([exc, None])
        out = _run(_units(2), s, batch=2, lookahead=1)
        assert len(out) == 2, f"{type(exc).__name__} must be retried"
        assert s.posts == 2


def test_retries_are_bounded_and_then_raise():
    s = _Session([500] * 20)
    try:
        _run(_units(2), s, batch=2, lookahead=1)
        raise AssertionError("a permanently failing endpoint must raise")
    except requests.HTTPError:
        pass
    assert s.posts == rh.TTS_ATTEMPTS, \
        f"expected {rh.TTS_ATTEMPTS} attempts, got {s.posts}"


def test_client_errors_are_not_retried():
    """A 4xx is our bug — retrying just burns GPU credits."""
    s = _Session([400] * 5)
    try:
        _run(_units(2), s, batch=2, lookahead=1)
        raise AssertionError("4xx must propagate")
    except requests.HTTPError:
        pass
    assert s.posts == 1, f"4xx must not be retried, saw {s.posts} posts"


def test_deterministic_endpoint_error_is_not_retried():
    class Deterministic(_Session):
        def post(self, *a, **kw):
            self.posts += 1
            return _Resp({"error": "texts [3] exceed 2000 chars"})

    s = Deterministic()
    try:
        _run(_units(2), s, batch=2, lookahead=1)
        raise AssertionError("endpoint-reported error must propagate")
    except RuntimeError as e:
        assert "exceed 2000" in str(e)
    assert s.posts == 1, "a 200 + {'error'} is deterministic; retrying wastes GPU"


# ----------------------------------------------------------------- resume

def _synth(units, out, session, fail_after=None):
    """Run synthesize against the fake endpoint, optionally dying partway."""
    real_session = requests.Session
    real_sleep = rh.time.sleep
    requests.Session = lambda: session
    rh.time.sleep = lambda s: None
    seen = {"n": 0}

    def progress(i, n, text):
        seen["n"] = i
        if fail_after and i >= fail_after:
            raise RuntimeError("simulated crash mid-paper")

    try:
        return rh.synthesize(units, out, "af_heart", 1.0, progress=progress,
                             tts_cfg={"modal_endpoint": "https://fake",
                                      "backend": "modal"})
    finally:
        requests.Session, rh.time.sleep = real_session, real_sleep


def test_resume_reuses_narrated_audio_and_matches_a_clean_run():
    if not shutil.which("ffmpeg"):
        print("  (skipped: ffmpeg not installed)")
        return
    tmp = Path(tempfile.mkdtemp())
    units_a, units_b = _units(60), _units(60)

    # reference: one clean run
    ref = tmp / "clean.m4a"
    dur_ref = _synth(units_a, ref, _Session())
    assert ref.exists()

    # interrupted run: dies after unit 40
    out = tmp / "paper.m4a"
    s1 = _Session()
    try:
        _synth(units_b, out, s1, fail_after=40)
        raise AssertionError("the simulated crash must propagate")
    except RuntimeError:
        pass

    pcm, ckpt = tmp / "paper.m4a.pcm", tmp / "paper.m4a.ckpt"
    assert pcm.exists(), "narrated audio must survive a crash"
    assert ckpt.exists(), "a checkpoint must survive a crash"
    assert not out.exists(), "no output until the paper completes"
    ck = json.loads(ckpt.read_text())
    assert ck["done"] == 25, f"checkpoint on a 25-unit boundary, got {ck['done']}"
    posts_before = s1.posts

    # resume: only the remaining units may be requested
    s2 = _Session()
    dur = _synth(units_b, out, s2)
    assert out.exists(), "resumed run must produce the output"
    assert not pcm.exists() and not ckpt.exists(), \
        "scaffolding must be cleaned up on success"

    requested = sum(len(t) for t in s2.texts_seen)
    assert requested == 35, \
        f"only units 26-60 should be re-requested, got {requested}"
    assert s2.posts < posts_before, "resume must do strictly less work"
    assert abs(dur - dur_ref) < 0.05, \
        f"resumed duration {dur:.2f}s must match clean {dur_ref:.2f}s"
    # timings must be continuous across the seam, not restarted at zero
    t1s = [u["t1"] for u in units_b]
    assert t1s == sorted(t1s), "unit timings must increase monotonically"
    assert units_b[30]["t0"] > units_b[20]["t1"], "seam must not rewind"


def test_checkpoint_is_ignored_when_the_text_changed():
    """Re-extracting a paper must start clean, never splice two narrations."""
    if not shutil.which("ffmpeg"):
        print("  (skipped: ffmpeg not installed)")
        return
    tmp = Path(tempfile.mkdtemp())
    out = tmp / "p.m4a"
    try:
        _synth(_units(60), out, _Session(), fail_after=40)
    except RuntimeError:
        pass
    assert (tmp / "p.m4a.ckpt").exists()

    changed = _units(60)
    for u in changed:
        u["text"] = u["text"].replace("Sentence", "Rewritten sentence")
    s = _Session()
    _synth(changed, out, s)
    requested = sum(len(t) for t in s.texts_seen)
    assert requested == 60, \
        f"changed text must re-narrate everything, got {requested}"


def test_truncated_sidecar_is_not_trusted():
    """A crash mid-write can leave a partial tail; splicing it would shift
    every later word timing."""
    tmp = Path(tempfile.mkdtemp())
    pcm, ckpt = tmp / "a.pcm", tmp / "a.ckpt"
    units = _units(10)
    sig = rh._units_sig(units, "af_heart", 1.0)
    ckpt.write_text(json.dumps({"sig": sig, "done": 5, "samples": 1000,
                                "units": []}))

    pcm.write_bytes(b"\x00" * 1000)                      # short: 500 samples
    assert rh._load_checkpoint(ckpt, pcm, sig, units) == (0, 0), \
        "a sidecar shorter than the checkpoint must be distrusted"

    pcm.write_bytes(b"\x00" * 5000)                      # long: partial tail
    done, samples = rh._load_checkpoint(ckpt, pcm, sig, units)
    assert (done, samples) == (5, 1000)
    assert pcm.stat().st_size == 2000, "tail past the checkpoint must be cut"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all resume/retry tests passed")
