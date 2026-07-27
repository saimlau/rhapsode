"""A PDF often emits a table's cells and the paragraph after it as ONE block.
The model labels that block from its head (table cells), so the prose after
them used to be dropped with it. Verbatim regression from Chen et al. 2024."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reflow

# the real block: Table 1's cells run straight into the body paragraph
GLUED = ("Element type Deformable status Fixation plate Tetrahedral Deformable "
         "Grippers Hexahedral Rigid When the gripper for holding engages with and "
         "clamps the corresponding eyelet, it constrains all active structural "
         "degrees of freedom within the region of the clamped eyelet. Encastre "
         "boundary condition is thus applied to that specific region [24]. A "
         "specific angular displacement is applied to the center point of the "
         "cross-section at the end of the deformation region to simulate the "
         "process of the gripper applying the bending input to the plate. Fig. 6 "
         "shows the boundary condition in FE simulation and the point of interest "
         "used to collect data")

REAL_TABLE = ("Density Young's modulus Poisson ratio Yield stress 2.7g/cm3 68.9 "
              "GPa 0.33 276 MPa Element type Tetrahedral Hexahedral Rigid Mesh "
              "size 0.5 1.0 2.0 3.0 Step time 0.1 0.2 0.5 Increment 0.01 0.02")


def _blk(text):
    return {"id": 0, "page": 0, "x0": 0, "y0": 0, "x1": 100, "y1": 10,
            "text": text, "words": []}


def test_table_glued_to_prose_is_rescued():
    assert reflow._rescued(_blk(GLUED), {0: reflow.TABLE}), \
        "the paragraph fused to Table 1's cells must not be dropped"


def test_a_genuine_table_is_still_dropped():
    assert not reflow._rescued(_blk(REAL_TABLE), {0: reflow.TABLE}), \
        "cell/number soup has no sentences and must stay dropped"


def test_short_caption_still_dropped():
    cap = "FIGURE 6: Boundary condition and point for data collection"
    assert not reflow._rescued(_blk(cap), {0: reflow.CAPTION})


def test_references_are_never_rescued():
    refs = ("Smith, J., 2020. A very long paper title here. Journal of Things, "
            "12(3), pp.1-20. Jones, A., 2019. Another long title. Proceedings "
            "of the conference on stuff, pp.30-40. Lee, B., 2021. Third title "
            "about matters. Annual Review of Control, 4, pp.5-9. More text here "
            "to push this comfortably past the rescue word threshold value.")
    assert not reflow._rescued(_blk(refs), {0: reflow.REFERENCE})


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok", n)
