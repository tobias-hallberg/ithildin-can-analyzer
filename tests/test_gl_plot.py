# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui._gl_plot.GLPlotWidget``'s segment buffer (no GL context needed)."""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtGui import QColor

from ithildin.ui._gl_plot import _FLOATS_PER_SEGMENT, GLPlotWidget

_PATH = "Msg.Sig"


def _plot(qtbot, limit: int = 0) -> GLPlotWidget:  # type: ignore[no-untyped-def]
    plot = GLPlotWidget()
    qtbot.addWidget(plot)
    plot.set_segment_limit(limit)
    plot.add_curve(_PATH, QColor("red"))
    return plot


def _segments(plot: GLPlotWidget) -> np.ndarray:
    curve = plot._curves[_PATH]
    return curve.data[: curve.segment_count * _FLOATS_PER_SEGMENT]


def test_set_points_draws_what_append_point_draws(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    xs = [0.0, 0.5, 1.0, 1.0, 2.0, 2.5, 2.5, 3.0]
    ys = [1.0, 2.0, 3.0, math.nan, 4.0, 6.0, math.nan, 5.0]
    one_by_one = _plot(qtbot)
    for x, y in zip(xs, ys, strict=True):
        one_by_one.append_point(_PATH, x, y)
    at_once = _plot(qtbot)

    at_once.set_points(_PATH, xs, ys)

    assert at_once._curves[_PATH].segment_count == 3
    assert np.array_equal(_segments(at_once), _segments(one_by_one))
    curve = at_once._curves[_PATH]
    assert (curve.has_last, curve.last_x, curve.last_y) == (True, 3.0, 5.0)
    # The next point joins the last one, as it would have.
    at_once.append_point(_PATH, 3.5, 7.0)
    one_by_one.append_point(_PATH, 3.5, 7.0)
    assert np.array_equal(_segments(at_once), _segments(one_by_one))


def test_set_points_ending_in_a_break_leaves_the_line_broken(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    plot = _plot(qtbot)
    plot.set_points(_PATH, [0.0, 1.0], [1.0, math.nan])
    plot.append_point(_PATH, 2.0, 3.0)

    assert plot._curves[_PATH].segment_count == 0


def test_segment_limit_keeps_the_newest_segments(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    limited = _plot(qtbot, limit=300)
    reference = _plot(qtbot)
    for i in range(5000):
        for plot in (limited, reference):
            plot.append_point(_PATH, i * 0.01, math.sin(i))

    curve = limited._curves[_PATH]
    assert 300 <= curve.segment_count <= 375
    # The buffer stops growing once it holds the limit plus its slack.
    assert curve.segment_capacity == 512
    kept = _segments(limited)
    assert np.array_equal(kept, _segments(reference)[-len(kept) :])


def test_set_points_keeps_the_newest_segments_within_the_limit(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    xs = [float(i) for i in range(100)]
    limited = _plot(qtbot, limit=10)
    reference = _plot(qtbot)

    limited.set_points(_PATH, xs, xs)
    reference.set_points(_PATH, xs, xs)

    assert limited._curves[_PATH].segment_count == 10
    assert np.array_equal(_segments(limited), _segments(reference)[-10 * _FLOATS_PER_SEGMENT :])
