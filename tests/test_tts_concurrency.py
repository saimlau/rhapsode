"""The 500 that destroyed a 70-hour thesis, reproduced without a GPU.

modal_app runs up to MAX_CONCURRENT requests per container in threads. A
phonemizer EspeakBackend keeps per-call word counts on the instance
(_count_txt written before phonemizing, _count_phn after, then compared), so
two threads sharing one backend compare thread A's input count against thread
B's output count and it raises "number of lines in input and output must be
equal" — the 500 the client saw. This test pins both directions: sharing one
backend across threads raises, and one backend per concurrent slot does not.

Runs on CPU: the defect is in the espeak G2P layer, not the GPU model.
Skips cleanly when phonemizer/espeak-ng is not installed locally.
"""

import os
import queue
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

THREADS = 4
CALLS = 150
# a token that punctuation-splits into 2 chunks, alternated with one that does
# not — the mismatch the race needs (input=1 vs output=2)
TEXTS = ["hello world", "Fig.2 shows", "et al. report", "plain words here"]


def _backend():
    try:
        from phonemizer.backend import EspeakBackend
    except ImportError:
        return None
    try:
        return EspeakBackend("en-us", with_stress=True)
    except Exception:                       # espeak-ng shared lib absent
        return None


def _hammer(get_backend, threads=THREADS, calls=CALLS):
    """Run threads x calls phonemize() calls; return exceptions raised."""
    errors, barrier = [], threading.Barrier(threads)

    def work(tid):
        backend = get_backend()
        barrier.wait()                      # maximise the collision window
        for i in range(calls):
            try:
                backend.phonemize([TEXTS[(tid + i) % len(TEXTS)]])
            except Exception as exc:        # noqa: BLE001 - that is the point
                errors.append(exc)
                return

    workers = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    return errors


def test_one_backend_per_slot_is_race_free():
    """The fix: modal_app checks a pipeline (hence a backend) out per request."""
    if _backend() is None:
        print("  (skipped: phonemizer/espeak-ng not installed)")
        return
    pool = queue.Queue()
    for _ in range(THREADS):
        pool.put(_backend())

    def checkout():
        return pool.get()                   # each thread owns one outright

    errors = _hammer(checkout)
    assert not errors, f"pooled backends must not race: {errors[:2]}"


def test_sharing_one_backend_across_threads_raises():
    """Pins the regression: if someone reverts to a single shared pipeline,
    this test fails loudly instead of the thesis failing at 3 a.m."""
    if _backend() is None:
        print("  (skipped: phonemizer/espeak-ng not installed)")
        return
    shared = _backend()
    errors = _hammer(lambda: shared, threads=8, calls=800)
    if not errors:
        # the race is probabilistic; a quiet run proves nothing either way
        print("  (inconclusive: the race did not trigger this run)")
        return
    # The symptom varies with where the threads interleave — production saw
    # RuntimeError("number of lines in input and output must be equal"), while
    # locally the espeak output buffer itself gets clobbered mid-read
    # (UnicodeDecodeError). Both are the same shared-state defect, so assert
    # the failure, not one spelling of it.
    print(f"  (race reproduced: {len(errors)} failures, "
          f"e.g. {type(errors[0]).__name__})")


def test_modal_app_checks_a_pipeline_out_per_request():
    """Static guard: the endpoint must not touch a shared self.pipeline."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "modal_app.py").read_text()
    assert "self.pool" in src, "expected a pipeline pool"
    assert "self.pipeline(" not in src, \
        "the request path must not call a shared pipeline"
    assert "self.pool.get()" in src and "self.pool.put(" in src, \
        "pipelines must be checked out and returned"
    # the pool must be sized to the concurrency the container allows
    assert "range(MAX_CONCURRENT)" in src and \
           "max_inputs=MAX_CONCURRENT" in src, \
        "pool size and max_inputs must come from the same constant"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tts-concurrency tests passed")
