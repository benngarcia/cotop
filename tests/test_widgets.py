from cotop.widgets import braille_rows, sparkline


def test_braille_area_graph():
    # one row of 4 dot levels, 2 values per cell. Cell 1: full left, half right
    # (0x47 | 0xA0 = U+28E7). Cell 2: a gap (blank) and a zero (one baseline dot, 0x80).
    assert braille_rows([1.0, 0.5, None, 0.0], width=2, height=1) == ["⣧⢀"]
    # two rows: 0.5 fills the bottom row only
    assert braille_rows([0.5, 0.5], width=1, height=2) == [" ", "⣿"]
    # the newest value is the rightmost dot column; a short series pads with gaps
    assert braille_rows([1.0], width=2, height=1) == [" ⢸"]


def test_a_sparkline_gap_is_blank_and_a_zero_is_a_baseline():
    assert sparkline([None, 0.0], 2).plain == " ▁"
