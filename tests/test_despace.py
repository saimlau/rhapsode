"""Letter-tracked text (banners, running headers) must not be narrated one
letter at a time."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extraction import _despace, clean_text


def test_strips_a_letter_tracked_banner():
    # the Science-Robotics "HUMAN-ROBOT INTERACTION" banner fused to the title
    assert _despace("HU MA N-ROB OT IN TER AC TI ON The role of collaborative robotics") \
        == "The role of collaborative robotics"
    assert _despace("SC I EN C E RO B OT ICS FOCUS") == ""


def test_collapses_single_letter_spacing():
    assert _despace("H U M A N") == "HUMAN"
    assert _despace("a b s t r a c t") == "abstract"


def test_leaves_real_text_alone():
    # a genuine all-caps phrase (longer words) is not a tracked banner
    assert _despace("HUMAN ROBOT TASK FORCE for control") == "HUMAN ROBOT TASK FORCE for control"
    assert _despace("The goal of robotics is to improve human life.") \
        == "The goal of robotics is to improve human life."
    assert _despace("Effective Human-Robot Collaboration Through Wearable Sensors") \
        == "Effective Human-Robot Collaboration Through Wearable Sensors"


def test_clean_text_applies_despacing():
    assert clean_text("HU MA N-ROB OT IN TER AC TI ON The role of X") == "The role of X"


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok", n)
