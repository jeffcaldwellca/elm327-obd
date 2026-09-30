"""Reusable TUI widgets: gauge cards, status bar, history input."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Digits, Input, Sparkline, Static

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
