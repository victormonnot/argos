"""Offline reception timelines, rendered as standalone SVG or PNG figures.

The plot uses validated journals. Green means a recent valid LOCAL RECEPTION,
not a fresh physical measurement. Rejected payloads never restart an age curve.
Records from other sources and unsupported telemetry types remain in the journal
but are not plotted as samples of the selected component.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO

from argos.backends.mavlink.recording import Recording
from argos.backends.mavlink.telemetry import (
    BootProgress, TelemetryCache, TelemetryLimits, UpdateStatus,
)


@dataclass(frozen=True)
class ReceptionSeries:
    name: str
    limit: float
    valid: tuple[float, ...]
    rejected: tuple[float, ...]
    repeated: tuple[float, ...]
    decreased: tuple[float, ...]

    def spans(self, start: float, end: float):
        """(start, duration, state) intervals on the original run clock."""
        if not self.valid:
            return ((start, end - start, "absent"),)
        result = [(start, self.valid[0] - start, "absent")]
        for index, at in enumerate(self.valid):
            following = self.valid[index + 1] if index + 1 < len(self.valid) else end
            expiry = min(following, at + self.limit)
            result.append((at, expiry - at, "recent"))
            result.append((expiry, following - expiry, "stale"))
        return tuple(span for span in result if span[1] > 0)

    def ages(self, end: float):
        """Vertices include the pre-reset age at every new valid reception."""
        if not self.valid:
            return (), ()
        x, y = [self.valid[0]], [0.]
        previous = self.valid[0]
        for at in self.valid[1:]:
            x.extend((at, at))
            y.extend((at - previous, 0.))
            previous = at
        x.append(end)
        y.append(end - previous)
        return tuple(x), tuple(y)


def reception_series(recording: Recording, *, system: int, component: int,
                     limits: TelemetryLimits) -> tuple[ReceptionSeries, ...]:
    """Derive plot data through the same admission boundary as textual inspection."""
    cache = TelemetryCache(system=system, component=component, limits=limits,
                           started_at=recording.started_at)
    names = {0: "heartbeat", 30: "attitude", 32: "local_position_ned"}
    groups = {name: {key: [] for key in ("valid", "rejected", "repeated", "decreased")}
              for name in names.values()}
    for event in recording.events:
        result = cache.update(event, event.received_at)
        name = names.get(event.message_id)
        if name is None or (event.system, event.component) != (system, component):
            continue
        if result.status is UpdateStatus.REJECTED:
            groups[name]["rejected"].append(event.received_at)
        elif result.accepted:
            groups[name]["valid"].append(event.received_at)
            view = getattr(cache.snapshot(event.received_at), name)
            if view.boot_progress is BootProgress.REPEATED:
                groups[name]["repeated"].append(event.received_at)
            elif view.boot_progress is BootProgress.DECREASED:
                groups[name]["decreased"].append(event.received_at)
    return tuple(ReceptionSeries(name, getattr(limits, name),
                                 **{key: tuple(value) for key, value in groups[name].items()})
                 for name in names.values())


def plot_recording(recording: Recording, stream: BinaryIO, *, system: int,
                   component: int, limits: TelemetryLimits, format: str = "svg") -> None:
    """Write a headless figure to a borrowed stream; optional dependency is lazy."""
    if format not in ("svg", "png"):
        raise ValueError("plot format must be svg or png")
    series = reception_series(recording, system=system, component=component, limits=limits)
    try:
        from matplotlib import rc_context
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from matplotlib.lines import Line2D
        from matplotlib.patches import Patch
    except ImportError as exc:
        raise ImportError('install plotting support: pip install -e ".[plot]"') from exc

    start, end = recording.started_at, recording.ended_at
    duration = end - start
    colors = {"absent": "#e2e8f0", "recent": "#a7dfc2", "stale": "#f7ce85"}
    curve_colors = ("#2563eb", "#9333ea", "#087e8b")
    with rc_context({"font.family": "DejaVu Sans", "font.size": 10,
                     "svg.fonttype": "none", "svg.hashsalt": "argos-receptions-v1"}):
        fig = Figure(figsize=(11, 6.5), facecolor="#ffffff")
        FigureCanvasAgg(fig)
        timeline, age = fig.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [1, 1.3]})
        fig.suptitle(f"MAVLink reception journal — source {system}/{component}",
                     x=.09, ha="left", fontsize=17, weight="bold")
        for row, (item, curve_color) in enumerate(zip(series, curve_colors)):
            spans = item.spans(start, end)
            for state, color in colors.items():
                ranges = [(at - start, length) for at, length, kind in spans if kind == state]
                timeline.broken_barh(ranges, (row - .3, .6),
                                     facecolors=color, edgecolors="none")
            timeline.scatter([at - start for at in item.valid], [row] * len(item.valid),
                             s=10, color=curve_color, zorder=3)
            for points, marker, color in ((item.rejected, "x", "#b91c1c"),
                                          (item.repeated, "D", "#334155"),
                                          (item.decreased, "v", "#0f172a")):
                timeline.scatter([at - start for at in points], [row] * len(points),
                                 marker=marker, color=color, s=32, zorder=4)
            x, y = item.ages(end)
            if x:
                age.plot([at - start for at in x], y, color=curve_color,
                         linewidth=1.8, label=item.name.upper())
                age.axhline(item.limit, color=curve_color, linestyle=":", linewidth=1,
                            alpha=.7)
        timeline.set_yticks(range(3), [item.name.upper() for item in series])
        timeline.set_ylim(2.6, -.6)
        timeline.set_title("Last valid reception: availability and events", loc="left", fontsize=11)
        age.set_ylabel("Age since valid reception (s)")
        age.set_xlabel(f"Seconds since journal start (run time {start:g} s)")
        age.set_ylim(bottom=0)
        age.grid(axis="both", color="#e2e8f0", linewidth=.7)
        age.set_axisbelow(True)
        if any(item.valid for item in series):
            age.legend(loc="upper left", fontsize=8)
        else:
            age.text(.5, .5, "No valid telemetry from this source", transform=age.transAxes,
                     ha="center", color="#64748b")
        age.set_xlim(0, duration if duration > 0 else 1.)
        legend = [Patch(facecolor=color, label=state.capitalize()) for state, color in colors.items()]
        legend += [Line2D([], [], linestyle="none", marker=marker, color=color, label=label)
                   for marker, color, label in (("x", "#b91c1c", "Rejected payload"),
                                                ("D", "#334155", "Boot repeated"),
                                                ("v", "#0f172a", "Boot decreased"))]
        fig.legend(handles=legend, loc="upper center", bbox_to_anchor=(.53, .91),
                   ncol=3, frameon=False, fontsize=9)
        note = ("Reception age only • dotted lines: chosen age limits • no physical freshness or RF-loss claim")
        if duration == 0:
            note += "\nZero-duration journal; horizontal axis padded for display."
        fig.text(.09, .025, note, fontsize=8, color="#64748b")
        fig.tight_layout(rect=(0, .055, 1, .84))
        metadata = {"Creator": "ARGOS"}
        if format == "svg":
            metadata["Date"] = None
        fig.savefig(stream, format=format, dpi=160, metadata=metadata)
