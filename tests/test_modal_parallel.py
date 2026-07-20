"""Parallel TTS batching must be indistinguishable from serial.

Audio is streamed into ffmpeg in order, so out-of-order *completion* must never
become out-of-order *output*. The fake endpoint below answers later batches
FASTEST, which reliably exposes any code that yields on completion order.
"""

import base64
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import rhapsode


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """Returns a distinct tone per unit; later batches return sooner."""

    def __init__(self, order_log, delay_for=None):
        self.order_log = order_log
        self.delay_for = delay_for or (lambda texts: 0.0)
        self.lock = threading.Lock()
        self.max_inflight = 0
        self._inflight = 0
        self.closed = False

    def close(self):
        self.closed = True

    def post(self, endpoint, headers=None, timeout=None, json=None):
        with self.lock:
            self._inflight += 1
            self.max_inflight = max(self.max_inflight, self._inflight)
        texts = json["texts"]
        time.sleep(self.delay_for(texts))
        with self.lock:
            self._inflight -= 1
            self.order_log.append(texts[0])
        results = []
        for t in texts:
            # unit "N" -> N samples of value N, so any mis-ordering shows up
            n = int(t)
            pcm = np.full(n, n, dtype="<i2").tobytes()
            results.append({"pcm_b64": base64.b64encode(pcm).decode(),
                            "words": [{"w": t, "t0": 0.0, "t1": 1.0}]})
        return _FakeResp({"sample_rate": 24000, "results": results})


def _units(n):
    return [{"text": str(i), "kind": "body", "rects": [], "para_end": False,
             "pause": 0.0} for i in range(1, n + 1)]


def _collect(units, session, **kw):
    import requests
    real = requests.Session
    requests.Session = lambda: session
    try:
        cfg = {"modal_endpoint": "https://fake", "backend": "modal"}
        return [(u["text"], w[0][0].copy())
                for u, w in rhapsode._modal_unit_audio(units, "af_heart", 1.0,
                                                       cfg, **kw)]
    finally:
        requests.Session = real


def test_parallel_output_matches_serial_exactly():
    units = _units(24)                      # 3 batches of 8
    serial = _collect(units, _FakeSession([]), batch=8, lookahead=1)
    # later batches finish FIRST — completion order is reversed
    log = []
    fast_last = _FakeSession(log, delay_for=lambda t: 0.30 / int(t[0]))
    parallel = _collect(units, fast_last, batch=8, lookahead=4)

    assert [t for t, _ in serial] == [t for t, _ in parallel]
    for (ts, ws), (tp, wp) in zip(serial, parallel):
        assert ts == tp
        assert np.array_equal(ws, wp), f"audio differs for unit {ts}"
    # the fake really did complete out of order (otherwise the test proves little)
    assert log[0] != "1", f"expected a later batch to finish first, got {log}"


def test_requests_actually_overlap():
    units = _units(32)                      # 4 batches
    s = _FakeSession([], delay_for=lambda t: 0.15)
    _collect(units, s, batch=8, lookahead=4)
    assert s.max_inflight > 1, "batches were not sent concurrently"


def test_serial_when_lookahead_is_one():
    units = _units(16)
    s = _FakeSession([], delay_for=lambda t: 0.05)
    _collect(units, s, batch=8, lookahead=1)
    assert s.max_inflight == 1, "lookahead=1 must not overlap requests"


def test_error_in_any_batch_propagates():
    class Boom(_FakeSession):
        def post(self, endpoint, headers=None, timeout=None, json=None):
            if json["texts"][0] == "9":     # second batch
                return _FakeResp({"error": "texts too long", "results": []})
            return super().post(endpoint, headers=headers, timeout=timeout,
                                json=json)
    try:
        _collect(_units(24), Boom([]), batch=8, lookahead=4)
    except RuntimeError as e:
        assert "texts too long" in str(e)
    else:
        raise AssertionError("a failing batch must raise")



def test_abandoning_the_generator_does_not_block():
    """A consumer that stops early (ffmpeg died, error) must not wait on the
    in-flight batches — shutdown(wait=True) would stall the whole queue."""
    units = _units(40)
    s = _FakeSession([], delay_for=lambda t: 1.5)
    import requests, time as _t
    real = requests.Session
    requests.Session = lambda: s
    try:
        gen = rhapsode._modal_unit_audio(units, "af_heart", 1.0,
                                         {"modal_endpoint": "https://fake",
                                          "backend": "modal"},
                                         batch=8, lookahead=4)
        next(gen)                     # first batch delivered; others in flight
        t0 = _t.time()
        gen.close()                   # abandon
        took = _t.time() - t0
        assert took < 0.5, f"teardown blocked on in-flight batches ({took:.2f}s)"
        assert s.closed, "the session must be closed on teardown"
    finally:
        requests.Session = real



def test_oversized_unit_is_chunked_and_reassembled():
    """A unit over the endpoint's 2000-char cap (e.g. a thesis TOC classified
    as a heading) must be split into pieces, never exceed per-request limits,
    and come back as ONE unit with concatenated audio and shifted timings."""
    import rhapsode as rh
    monster = " ".join(f"tok{i:03d}" for i in range(700))     # ~4900 chars
    units = ([{"text": "small one.", "kind": "body", "rects": [],
               "para_end": False, "pause": 0.0}]
             + [{"text": monster, "kind": "heading", "rects": [],
                 "para_end": False, "pause": 0.0}]
             + [{"text": "small two.", "kind": "body", "rects": [],
                 "para_end": False, "pause": 0.0}])

    seen = {"max_texts": 0, "max_len": 0}
    class Chunky(_FakeSession):
        def post(self, endpoint, headers=None, timeout=None, json=None):
            texts = json["texts"]
            seen["max_texts"] = max(seen["max_texts"], len(texts))
            seen["max_len"] = max(seen["max_len"], max(map(len, texts)))
            results = []
            for t in texts:
                n = len(t)                      # samples == chars of the piece
                pcm = np.full(n, 7, dtype="<i2").tobytes()
                results.append({"pcm_b64": base64.b64encode(pcm).decode(),
                                "words": [{"w": t[:8], "t0": 0.0,
                                           "t1": n / 24000}]})
            return _FakeResp({"sample_rate": 24000, "results": results})

    import requests
    real = requests.Session
    requests.Session = lambda: Chunky([])
    try:
        out = list(rh._modal_unit_audio(units, "af_heart", 1.0,
                                        {"modal_endpoint": "https://fake",
                                         "backend": "modal"},
                                        batch=4, lookahead=3))
    finally:
        requests.Session = real

    assert seen["max_texts"] <= 4, "per-request text cap violated"
    assert seen["max_len"] <= 1900, "a piece exceeded the endpoint char cap"
    assert [u["text"][:8] for u, _ in out] == ["small on", " ".join(
        f"tok{i:03d}" for i in range(2))[:8], "small tw"]
    # the monster unit: one entry, audio length == total chars of its pieces
    mu, mw = out[1]
    wave, words = mw[0]
    assert len(wave) > 4000, "merged audio must cover all pieces"
    assert len(words) >= 3, "one word per piece expected"
    t0s = [w["t0"] for w in words]
    assert t0s == sorted(t0s) and t0s[1] > 0, "timings must shift cumulatively"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all modal-parallel tests passed")
