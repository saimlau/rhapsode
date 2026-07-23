import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


def _lib():
    return server.Library(Path(tempfile.mkdtemp()))


def _paper(lib, pid, owner, billed, dur, status="ready"):
    lib.data["papers"][pid] = {"id": pid, "owner": owner, "billed": billed,
                               "duration": dur, "status": status}
    lib.data["order"].append(pid)


def test_sums_only_operator_billed_ready_papers():
    lib = _lib()
    _paper(lib, "a", "bob", "operator", 3600)
    _paper(lib, "b", "bob", "self", 3600)          # bob pays -> not counted
    _paper(lib, "c", "bob", "operator", 1800, status="pending")  # not ready
    _paper(lib, "d", "carol", "operator", 3600)    # someone else
    lib.save()
    assert server.operator_tts_hours(lib, "bob") == 1.0


def test_absent_billed_counts_as_operator():
    lib = _lib()
    lib.data["papers"]["old"] = {"id": "old", "owner": "bob",
                                 "duration": 7200, "status": "ready"}
    lib.data["order"].append("old")
    lib.save()
    assert server.operator_tts_hours(lib, "bob") == 2.0
