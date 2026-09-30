"""Pure drag-vs-click slop — mirrors renderer/src/lib/pointerDrag.ts."""

DRAG_CLICK_SLOP_PX = 5


def is_click_not_drag(start: dict, end: dict, slop_px: int = DRAG_CLICK_SLOP_PX) -> bool:
    return abs(end["x"] - start["x"]) < slop_px and abs(end["y"] - start["y"]) < slop_px


def test_stationary_pointer_is_a_click():
    start = {"x": 100, "y": 200}
    assert is_click_not_drag(start, {"x": 100, "y": 200}) is True


def test_jitter_inside_slop_is_a_click():
    start = {"x": 100, "y": 200}
    assert is_click_not_drag(start, {"x": 104, "y": 201}) is True


def test_move_past_slop_is_a_drag():
    start = {"x": 100, "y": 200}
    assert is_click_not_drag(start, {"x": 106, "y": 200}) is False
    assert is_click_not_drag(start, {"x": 100, "y": 208}) is False
