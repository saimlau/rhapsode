"""Worker extraction prefetch: overlap, safety gate, and failure handling."""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


def _worker(backend="modal"):
    lib = server.Library(Path(tempfile.mkdtemp()))
    w = server.Worker(lib, "af_heart", 1.0, 150, tts_cfg={"backend": backend})
    return lib, w


def _add(lib, pid, status="pending"):
    lib.data["papers"][pid] = {"id": pid, "status": status, "progress": 0.0}


def test_prefetch_extracts_next_paper_ahead(monkey=None):
    lib, w = _worker()
    _add(lib, "p2")
    w.q.put("p2")
    calls = []
    real = server.p2a.prepare_units
    server.p2a.prepare_units = lambda path, g, l: (calls.append(path) or
                                                   (["units"], {}, []))
    try:
        w._start_prefetch()
        assert w._prefetch is not None and w._prefetch[0] == "p2"
        assert w._take_prefetch("p2") == (["units"], {}, [])
        assert len(calls) == 1, "prefetch must extract exactly once"
        assert w._prefetch is None, "taking must clear the slot"
    finally:
        server.p2a.prepare_units = real


def test_take_prefetch_returns_none_for_a_different_paper():
    lib, w = _worker()
    _add(lib, "p2")
    w.q.put("p2")
    real = server.p2a.prepare_units
    server.p2a.prepare_units = lambda path, g, l: (["u"], {}, [])
    try:
        w._start_prefetch()
        assert w._take_prefetch("SOMETHING_ELSE") is None
        assert w._prefetch is not None, "a mismatch must not discard the work"
    finally:
        server.p2a.prepare_units = real


def test_local_backend_never_prefetches():
    """Overlapping extraction with LOCAL synthesis would contend for one GPU."""
    lib, w = _worker(backend="local")
    _add(lib, "p2")
    w.q.put("p2")
    w._start_prefetch()
    assert w._prefetch is None
    assert w.q.qsize() == 1, "the paper must stay queued for the normal path"


def test_prefetch_skips_deleted_paper():
    lib, w = _worker()
    w.q.put("ghost")                      # queued but never in the registry
    w._start_prefetch()
    assert w._prefetch is None


def test_prefetch_error_resurfaces_on_take():
    lib, w = _worker()
    _add(lib, "p2")
    w.q.put("p2")
    real = server.p2a.prepare_units

    def boom(path, g, l):
        raise ValueError("scanned/image-only PDF?")
    server.p2a.prepare_units = boom
    try:
        w._start_prefetch()
        try:
            w._take_prefetch("p2")
        except ValueError as e:
            assert "scanned" in str(e)
        else:
            raise AssertionError("a failed prefetch must raise for that paper")
    finally:
        server.p2a.prepare_units = real


def test_prefetch_actually_overlaps():
    """The submit must return immediately, not block on extraction."""
    lib, w = _worker()
    _add(lib, "p2")
    w.q.put("p2")
    real = server.p2a.prepare_units
    server.p2a.prepare_units = lambda path, g, l: (time.sleep(0.4),
                                                   (["u"], {}, []))[1]
    try:
        t0 = time.time()
        w._start_prefetch()
        submit_took = time.time() - t0
        assert submit_took < 0.2, f"_start_prefetch blocked ({submit_took:.2f}s)"
        w._take_prefetch("p2")            # now it blocks, as designed
    finally:
        server.p2a.prepare_units = real




def test_worker_actually_processes_a_prefetched_paper():
    """Integration: drive run(). A paper pulled forward by the prefetch must
    still be generated — the unit tests above never exercised the real loop."""
    lib, w = _worker()
    for pid in ("p1", "p2", "p3"):
        _add(lib, pid)
        w.q.put(pid)
    real_prep, real_gen = server.p2a.prepare_units, server.p2a.generate_readalong
    server.p2a.prepare_units = lambda path, g, l: (["u"], {"title": "t",
                                                           "authors": "a",
                                                           "year": 2024}, [])
    server.p2a.generate_readalong = lambda *a, **k: {
        "duration": 1.0, "warnings": [], "title": "t", "authors": "a",
        "year": 2024, "units": 1}
    try:
        t = threading.Thread(target=w.run, daemon=True)
        t.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            done = [p for p in lib.data["papers"].values()
                    if p.get("status") == "ready"]
            if len(done) == 3:
                break
            time.sleep(0.1)
        statuses = {k: v.get("status") for k, v in lib.data["papers"].items()}
        assert all(s == "ready" for s in statuses.values()), \
            f"a paper was lost by the prefetch: {statuses}"
    finally:
        server.p2a.prepare_units, server.p2a.generate_readalong = real_prep, real_gen


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all prefetch tests passed")
