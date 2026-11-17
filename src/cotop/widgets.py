"""Drawing in the terminal: braille graphs, block meters and row sparklines."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from rich.style import Style
from rich.text import Text
from textual.color import Color, Gradient
from textual.reactive import reactive
from textual.widget import Widget

# Colab's own oranges carry the work being done; slate is the floor.
SLATE = "#5A6B7E"
ORANGE = "#E8710A"
AMBER = "#F9AB00"
RED = "#F85149"
DIM = "#8B949E"
EMPTY = "#2A313B"

WORK = Gradient((0.0, Color.parse(SLATE)), (0.5, Color.parse(ORANGE)), (1.0, Color.parse(AMBER)))
"""Utilization: busier is brighter."""
PRESSURE = Gradient((0.0, Color.parse(SLATE)), (0.6, Color.parse(AMBER)), (1.0, Color.parse(RED)))
"""RAM and disk: fuller is closer to failing."""

# Braille dot bits, bottom row first, for the left and right column of a cell.
_LEFT = (0x40, 0x04, 0x02, 0x01)
_RIGHT = (0x80, 0x20, 0x10, 0x08)


def braille_rows(values: list[float | None], width: int, height: int) -> list[str]:
    """An area graph of values in 0..1, two values per cell, newest at the right.

    A None is a gap: no dots. A zero keeps one baseline dot, so an idle GPU and a
    missed reading look different.
    """
    values = values[-2 * width :]
    values = [None] * (2 * width - len(values)) + values
    dots = 4 * height
    levels = [None if v is None else max(1, round(min(max(v, 0.0), 1.0) * dots)) for v in values]  # zero: a baseline dot
    rows = []
    for row in range(height):
        floor = (height - 1 - row) * 4  # dot level at this row's bottom
        cells = []
        for c in range(width):
            code = 0
            for bits, level in ((_LEFT, levels[2 * c]), (_RIGHT, levels[2 * c + 1])):
                if level is None:
                    continue
                for k in range(4):
                    if level > floor + k:
                        code |= bits[k]
            cells.append(chr(0x2800 + code) if code else " ")
        rows.append("".join(cells))
    return rows


class Graph(Widget):
    """A braille area graph that fills its widget, coloured by height."""

    DEFAULT_CSS = "Graph { width: 1fr; height: 1fr; min-height: 2; }"

    values: reactive[list[float | None]] = reactive(list, always_update=True)

    def __init__(self, gradient: Gradient = WORK, source: Callable[[int], list[float | None]] | None = None, **kwargs):
        """source(n), when given, supplies n values for the graph's width at each draw."""
        super().__init__(**kwargs)
        self.gradient = gradient
        self.source = source

    def render(self) -> Text:
        width, height = self.size.width, self.size.height
        if width <= 0 or height <= 0:
            return Text()
        values = self.source(2 * width) if self.source else self.values
        text = Text()
        for row, line in enumerate(braille_rows(values, width, height)):
            color = self.gradient.get_color((height - row - 0.5) / height)
            text.append(line, Style(color=color.rich_color))
            if row < height - 1:
                text.append("\n")
        return text


def meter(fraction: float, width: int, gradient: Gradient) -> Text:
    """btop's block meter: each filled block takes the colour of its position."""
    filled = round(min(max(fraction, 0.0), 1.0) * width)
    text = Text()
    for i in range(width):
        if i < filled:
            text.append("■", Style(color=gradient.get_color((i + 0.5) / width).rich_color))
        else:
            text.append("■", Style(color=EMPTY))
    return text


_BARS = " ▁▂▃▄▅▆▇█"


def sparkline(values: list[float | None], width: int) -> Text:
    """Eighth-height bars for a table cell; gaps are blank."""
    values = values[-width:]
    text = Text(" " * (width - len(values)))
    for v in values:
        if v is None:
            text.append(" ")
        else:
            v = min(max(v, 0.0), 1.0)
            text.append(_BARS[max(1, round(v * 8))], Style(color=WORK.get_color(v).rich_color))  # zero: a baseline, as in Graph
    return text


class TimeAxis(Widget):
    """'6m ago … now' under a graph, from the real time of its oldest visible reading."""

    DEFAULT_CSS = "TimeAxis { height: 1; color: $text-muted; }"

    def __init__(self, time_of: Callable[[int], datetime | None], **kwargs):
        """time_of(n): when the n-th newest reading was taken."""
        super().__init__(**kwargs)
        self.time_of = time_of

    def render(self) -> Text:
        from cotop.model import fmt_duration, utc_now

        width = self.size.width
        oldest = self.time_of(2 * width)
        if oldest is None:
            return Text()
        span = fmt_duration(utc_now() - oldest) + " ago"
        return Text(span + "now".rjust(max(width - len(span), 4)))
