"""Reusable TUI widgets: gauge cards, status bar, history input."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Digits, Input, Sparkline, Static, TabbedContent, TabPane

from elm327obd import pids as P

SPARK_CHARS = "▁▂▃▄▅▆▇█"


class LiveStore:
    """Latest values + rolling history for everything the poller reads."""

    def __init__(self, history: int = 120) -> None:
        self.values: dict[int, P.Value] = {}
        self.history: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=history))
        self.lo: dict[int, float] = {}
        self.hi: dict[int, float] = {}
        self.stamp: dict[int, float] = {}
        self._cycles: deque[float] = deque(maxlen=30)

    def update(self, values: dict[int, P.Value]) -> None:
        now = time.monotonic()
        for pid, v in values.items():
            self.values[pid] = v
            self.stamp[pid] = now
            if isinstance(v, (int, float)):
                self.history[pid].append(float(v))
                self.lo[pid] = min(self.lo.get(pid, v), v)
                self.hi[pid] = max(self.hi.get(pid, v), v)

    def cycle(self) -> None:
        self._cycles.append(time.monotonic())

    @property
    def rate(self) -> float:
        if len(self._cycles) < 2:
            return 0.0
        span = self._cycles[-1] - self._cycles[0]
        if time.monotonic() - self._cycles[-1] > 2:
            return 0.0
        return (len(self._cycles) - 1) / span if span > 0 else 0.0

    def reset_minmax(self) -> None:
        self.lo.clear()
        self.hi.clear()


def trend(values: deque[float] | list[float], width: int = 24) -> str:
    data = list(values)[-width:]
    if len(data) < 2:
        return ""
    lo, hi = min(data), max(data)
    span = hi - lo or 1.0
    return "".join(SPARK_CHARS[int((v - lo) / span * (len(SPARK_CHARS) - 1))] for v in data)


def level_color(fraction: float) -> str:
    if fraction >= 0.85:
        return "#ff5f5f"
    if fraction >= 0.65:
        return "#ffaf00"
    return "#5fd7af"


def digits_text(value: P.Value | None, unit: str) -> str:
    if value is None:
        return "--"
    return P.format_value(value, unit).replace(",", "")


class Gauge(Vertical):
    """A dashboard card: big digits, colored level bar and sparkline."""

    def __init__(self, pid: int) -> None:
        super().__init__(classes="gauge")
        self.pid = pid
        self.spec = P.PIDS[pid]
        self._last = ""

    def compose(self) -> ComposeResult:
        with Horizontal(classes="gauge-top"):
            yield Digits("--", classes="gauge-value")
            yield Static(self.spec.unit, classes="gauge-unit")
        yield Static("", classes="gauge-bar")
        yield Sparkline([], classes="gauge-spark", summary_function=max)

    def on_mount(self) -> None:
        self.border_title = self.spec.name

    def refresh_from(self, store: LiveStore, imperial: bool) -> None:
        raw = store.values.get(self.pid)
        value, unit = P.convert(raw, self.spec.unit, imperial)
        text = digits_text(value, unit)
        stale = time.monotonic() - store.stamp.get(self.pid, 0) > 3
        self.set_class(stale, "stale")
        if text != self._last:
            self._last = text
            self.query_one(".gauge-value", Digits).update(text)
        self.query_one(".gauge-unit", Static).update(unit)

        lo, hi = P.convert_range(self.spec.lo, self.spec.hi, self.spec.unit, imperial)
        frac = 0.0
        if isinstance(value, (int, float)) and hi > lo:
            frac = max(0.0, min(1.0, (value - lo) / (hi - lo)))
        width = max(4, self.content_size.width)
        filled = round(frac * width)
        bar = Text()
        bar.append("━" * filled, style=level_color(frac))
        bar.append("━" * (width - filled), style="#3a3a3a")
        self.query_one(".gauge-bar", Static).update(bar)

        hist = store.history.get(self.pid)
        if hist:
            self.query_one(".gauge-spark", Sparkline).data = list(hist)[-max(8, width):]
        if self.pid in store.lo:
            mn, _ = P.convert(store.lo[self.pid], self.spec.unit, imperial)
            mx, _ = P.convert(store.hi[self.pid], self.spec.unit, imperial)
            self.border_subtitle = f"▼{P.format_value(mn, unit)} ▲{P.format_value(mx, unit)}"


class StatusBar(Static):
    """Top status line: connection, adapter, protocol, voltage, rate, recording."""

    def render_status(
        self,
        *,
        state: str,
        address: str = "",
        version: str = "",
        protocol: str = "",
        voltage: float | None = None,
        rate: float = 0.0,
        imperial: bool = False,
        recording: str = "",
        mil: bool | None = None,
    ) -> None:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(" ◉ OBD·TUI ", style="bold #000000 on #5fd7af")
        t.append("  ")
        style, label = {
            "online": ("bold #5fd7af", "● ONLINE"),
            "adapter": ("bold #ffaf00", "● ADAPTER ONLY – vehicle not responding"),
            "reconnecting": ("bold #ffaf00", "◌ RECONNECTING…"),
            "offline": ("bold #ff5f5f", "● OFFLINE"),
        }.get(state, ("", state))
        t.append(label, style=style)
        if address:
            t.append(f"  {address}", style="#8a8a8a")
        if version:
            t.append(f"  {version}", style="#8a8a8a")
        if protocol:
            t.append(f"  {protocol}", style="#8a8a8a")
        if voltage is not None:
            vstyle = "#5fd7af" if voltage >= 12.2 else ("#ffaf00" if voltage >= 11.8 else "#ff5f5f")
            t.append(f"  ⚡{voltage:.1f}V", style=vstyle)
        if rate:
            t.append(f"  ↻ {rate:.1f} Hz", style="#87afff")
        if mil is not None:
            t.append("  MIL ON" if mil else "  MIL off", style="bold #ff5f5f" if mil else "#5f875f")
        t.append(f"  {'Imperial' if imperial else 'Metric'}", style="#8a8a8a")
        if recording:
            t.append(f"  ⏺ REC {recording}", style="bold #ff5f5f")
        self.update(t)


class HistoryInput(Input):
    """Input with shell-style up/down command history."""

    BINDINGS = [
        Binding("up", "history(-1)", "Prev", show=False),
        Binding("down", "history(1)", "Next", show=False),
        Binding("escape", "leave", "Leave input", show=False),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.history: list[str] = []
        self._pos = 0

    def remember(self, value: str) -> None:
        if value and (not self.history or self.history[-1] != value):
            self.history.append(value)
        self._pos = len(self.history)

    def action_leave(self) -> None:
        self.screen.set_focus(None)

    def action_history(self, step: int) -> None:
        if not self.history:
            return
        self._pos = max(0, min(len(self.history), self._pos + step))
        self.value = self.history[self._pos] if self._pos < len(self.history) else ""
        self.cursor_position = len(self.value)


class SteadyTabbedContent(TabbedContent):
    """TabbedContent that only changes tab when the user asks.

    Stock TabbedContent also activates whichever pane contains a newly focused
    widget. Widget.focus() is deferred, so a focus call queued for the previous
    tab can land after the user has already switched and flip them back. Tabs
    here only change via keys/clicks, and Tab-key focus cycling skips hidden
    panes, so focus-driven activation is disabled.
    """

    def _on_tab_pane_focused(self, event: TabPane.Focused) -> None:
        # Textual runs this handler for every class in the MRO; prevent_default
        # stops the base TabbedContent handler from switching the tab anyway.
        event.prevent_default()
        event.stop()


# ---- trouble-code guidance rendering ------------------------------------------

SEVERITY_STYLE = {
    "stop": ("■ STOP", "bold #ff5f5f"),
    "soon": ("▲ SOON", "bold #ffaf00"),
    "monitor": ("● MONITOR", "#d7d75f"),
    "low": ("○ LOW", "#5fd7af"),
}

KIND_NOTE = {
    "Stored": "Stored – a confirmed fault. Emission-related stored codes turn on the check-engine light.",
    "Pending": "Pending – seen on the last drive cycle but not confirmed yet. If the fault doesn't recur, "
               "it clears itself; if it does, it becomes stored.",
    "Permanent": "Permanent – can't be cleared with a scan tool. It clears itself after the repair, once the car "
                 "re-runs the test and passes (can take a few drive cycles).",
}

NAME_SOURCE = {
    "custom": "your dtc.csv",
    "built-in": "built-in list",
    "python-OBD": "python-OBD code list (GPL-2.0)",
    "category": "derived from the code's structure",
}


def _hanging(items: list[tuple[str, str]]):
    """Marker + text rows where wrapped lines stay aligned under the text."""
    from rich.table import Table

    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", no_wrap=True, style="#8a8a8a")
    grid.add_column(ratio=1)
    for marker, text in items:
        grid.add_row(f" {marker}", text)
    return grid


def severity_label(severity: str) -> Text:
    label, style = SEVERITY_STYLE[severity]
    return Text(label, style=style)


def render_guide(dtc, live: dict[int, P.Value] | None, imperial: bool, reading: bool = False):
    """Rich renderable describing one DTC with guidance and live readings."""
    from rich.console import Group

    from elm327obd.dtc import SEVERITY_TEXT, name_source
    from elm327obd.protocol import ecu_name

    g = dtc.guide
    parts: list = []
    head = Text()
    head.append(f"{g.code}  ", style="bold")
    head.append_text(severity_label(g.severity))
    if g.source == "estimated":
        head.append("  (estimated)", style="#8a8a8a")
    parts.append(head)
    parts.append(Text(g.name, style="bold #d0d0d0"))
    parts.append(Text(f"{dtc.kind} · {dtc.ecu} {ecu_name(dtc.ecu)}", style="#8a8a8a"))
    parts.append(Text(""))
    if g.summary:
        parts.append(Text(g.summary))
    parts.append(Text(SEVERITY_TEXT[g.severity], style=SEVERITY_STYLE[g.severity][1].replace("bold ", "")))

    if g.causes:
        parts.append(Text("\nLikely causes (most likely first)", style="bold #87afff"))
        parts.append(_hanging([(f"{i}.", cause) for i, cause in enumerate(g.causes, 1)]))
    if g.checks:
        parts.append(Text("\nWhat to check", style="bold #87afff"))
        parts.append(_hanging([("•", check) for check in g.checks]))
    if g.watch:
        parts.append(Text("\nLive readings", style="bold #87afff"))
        if reading:
            parts.append(Text(" ◌ reading…", style="#8a8a8a"))
        elif live is None:
            parts.append(Text(" (connect to the vehicle to see these)", style="#8a8a8a"))
        for pid in g.watch:
            if live is None or reading:
                break
            spec = P.PIDS[pid]
            value, unit = P.convert(live.get(pid), spec.unit, imperial)
            text = Text(f" {spec.name:<30}", style="#8a8a8a")
            if pid in live:
                text.append(f"{P.format_value(value, unit)} {unit}".rstrip(), style="bold")
            else:
                text.append("not supported", style="#5f5f5f")
            parts.append(text)

    parts.append(Text(f"\n{KIND_NOTE.get(dtc.kind, '')}", style="#8a8a8a"))
    source = "hand-written guide" if g.source == "guide" else (
        "your dtc_guide.toml" if g.source == "custom" else
        "estimated from the code's name/category – look up specifics for your vehicle")
    parts.append(Text(f"Name: {NAME_SOURCE[name_source(g.code)]} · Guidance: {source}", style="#5f5f5f"))
    parts.append(Text("General guidance, not a diagnosis for your specific vehicle.", style="italic #5f5f5f"))
    return Group(*parts)
