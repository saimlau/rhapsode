"""An ffmpeg death that left a resume checkpoint is a shutdown interruption,
not a bad paper: it must be resumable, not errored."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server


def _worker():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    return server.Worker(lib, "af_heart", 1.0, 150), lib


def test_ffmpeg_interrupt_needs_a_checkpoint_to_be_resumable():
    w, lib = _worker()
    pid = "p1"
    vd = lib.view_dir(pid); vd.mkdir(parents=True, exist_ok=True)
    died = RuntimeError("ffmpeg died during encode (rc=255)")
    failed = RuntimeError("ffmpeg encode failed (rc=255)")
    # no checkpoint -> a genuine ffmpeg failure -> errors (not resumable)
    assert w._resumable_ffmpeg_interrupt(pid, died) is False
    # with a checkpoint -> both ffmpeg messages are resumable
    (vd / "narration.m4a.ckpt").write_bytes(b"x")
    assert w._resumable_ffmpeg_interrupt(pid, died) is True
    assert w._resumable_ffmpeg_interrupt(pid, failed) is True


def test_non_ffmpeg_errors_are_never_resumable():
    w, lib = _worker()
    pid = "p2"
    vd = lib.view_dir(pid); vd.mkdir(parents=True, exist_ok=True)
    (vd / "narration.m4a.ckpt").write_bytes(b"x")   # even with a checkpoint
    assert w._resumable_ffmpeg_interrupt(pid, RuntimeError("CUDA out of memory")) is False
    assert w._resumable_ffmpeg_interrupt(pid, ValueError("no usable text")) is False


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok", n)
