# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""View kind → class registry, plus context-aware factory binding.

Single place to map the kind identifier (``"trace"``, ``"signal_plot"``,
etc.) to the ``ChildView`` subclass that the MDI shell instantiates and
wraps in a ``QMdiSubWindow``.

``make_factory(ctx)`` returns a closure-bound factory that calls
``view.bind(ctx)`` after construction so each view can subscribe to
``BusReader`` / ``Database`` etc. without reaching for globals.

The legacy ``"message_trace"`` and ``"bus_trace"`` kinds are kept as
aliases that build a :class:`TraceWindow`. Old ``.itp`` files that
record these kinds keep loading; their state shapes round-trip through
``TraceWindow.restore_state``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ithildin.ui._child_base import ChildView
from ithildin.ui.data_view import DataViewWindow
from ithildin.ui.record_window import RecordWindow
from ithildin.ui.replay_window import ReplayWindow
from ithildin.ui.signal_monitor import SignalMonitorWindow
from ithildin.ui.signal_plot import SignalPlotWindow
from ithildin.ui.trace_window import TraceWindow
from ithildin.ui.transmit_window import TransmitWindow

if TYPE_CHECKING:
    from ithildin.app_state.context import AppContext
    from ithildin.ui.window_manager import WindowFactory, WindowManager


# MDI sub-window kinds. Hardware, Settings, About, MCP allowlist live
# as ``QDialog``s opened from the toolbar (handoff §6) and are NOT in
# the factory.
#
# ``message_trace`` and ``bus_trace`` keys are legacy aliases for ``trace``;
# preserved so older ``.itp`` files keep loading without a schema bump.
_REGISTRY: dict[str, type[ChildView]] = {
    TraceWindow.KIND: TraceWindow,
    "message_trace": TraceWindow,
    "bus_trace": TraceWindow,
    DataViewWindow.KIND: DataViewWindow,
    RecordWindow.KIND: RecordWindow,
    ReplayWindow.KIND: ReplayWindow,
    SignalMonitorWindow.KIND: SignalMonitorWindow,
    SignalPlotWindow.KIND: SignalPlotWindow,
    TransmitWindow.KIND: TransmitWindow,
}


def known_kinds() -> list[str]:
    return list(_REGISTRY.keys())


def make_view(
    _manager: WindowManager,
    kind: str,
    instance_id: str,
    params: dict[str, object],
) -> ChildView:
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise KeyError(f"Unknown view kind: {kind!r}")
    return cls(instance_id=instance_id, params=params)


def make_factory(ctx: AppContext) -> WindowFactory:
    """Return a ``WindowFactory`` that calls ``view.bind(ctx)`` after build."""

    def factory(
        manager: WindowManager,
        kind: str,
        instance_id: str,
        params: dict[str, object],
    ) -> ChildView:
        view = make_view(manager, kind, instance_id, params)
        view.bind(ctx)
        return view

    return factory
