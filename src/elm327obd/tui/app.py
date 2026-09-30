"""Main Textual application."""

from __future__ import annotations

import asyncio
import time
from collections import deque

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets.data_table import CellDoesNotExist
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from elm327obd import pids as P
from elm327obd.config import Config
from elm327obd.datalog import DataLogger
from elm327obd.dtc import Dtc
from elm327obd.elm import ELM327, _parse_mode01
from elm327obd.profiles import Profile, load_profiles
from elm327obd.protocol import (
    NRC,
    ElmError,
    NegativeResponse,
    NoData,
    VehicleNotResponding,
    check_errors,
    ecu_name,
    is_can,
    parse_frames,
)
from elm327obd.transport import TransportError
from elm327obd.tui.screens import ConfirmScreen, ConnectScreen, PidPickerScreen
from elm327obd.tui.widgets import Gauge, HistoryInput, LiveStore, StatusBar, trend

DANGEROUS_SERVICES = {
    0x04: "Clear DTCs / freeze frame / readiness (Mode 04)",
    0x08: "Control of on-board system/test/component (Mode 08 – actuates hardware)",
    0x10: "Diagnostic session control",
    0x11: "ECU reset",
    0x14: "UDS clear diagnostic information",
    0x27: "Security access",
    0x28: "Communication control (can silence modules)",
    0x2C: "Dynamically define data identifier",
    0x2E: "Write data by identifier (changes ECU configuration)",
    0x2F: "Input/output control (actuates hardware!)",
    0x30: "KWP2000 input/output control by local identifier (actuates hardware!)",
    0x31: "Routine control",
    0x34: "Request download (ECU flashing)",
    0x35: "Request upload",
    0x36: "Transfer data (ECU flashing)",
    0x37: "Request transfer exit",
    0x3B: "KWP2000 write data by local identifier (changes ECU configuration)",
    0x3D: "Write memory by address",
    0x85: "Control DTC setting",
    0x87: "Link control (changes bus baud rate)",
}


def scan_rx(header: str) -> str | None:
    """Receive filter for a DID scan: 11-bit physical IDs answer on ID + 8
    (7E0→7E8, 726→72E, 760→768). Broadcast 7DF and 29-bit/legacy headers
    use the adapter's automatic filter."""
    if len(header) == 3 and int(header, 16) != 0x7DF:
        return f"{int(header, 16) + 8:03X}"
    return None


def command_risk(compact: str) -> str | None:
    """Why a console command needs confirmation, or None if it's read-only."""
    compact = compact.replace(" ", "").upper()
    if compact.startswith("AT"):
        if compact.startswith("ATPP"):
            return "Programmable parameters are stored in the adapter's EEPROM and persist"
        return None
    try:
        service = int(compact[:2], 16)
    except ValueError:
        return None
    if service == 0x10 and compact[2:4] in ("01", "81"):
        return None  # returning to the default session is harmless
    return DANGEROUS_SERVICES.get(service)

CONSOLE_HELP = """[b]Console[/b] — commands go straight to the adapter.
  [#5fd7af]ATRV[/]  battery voltage    [#5fd7af]ATDP[/]  protocol      [#5fd7af]ATI[/]  adapter version
  [#5fd7af]0100[/]  supported PIDs     [#5fd7af]010C[/]  RPM           [#5fd7af]03[/]   stored codes
  [#5fd7af]0902[/]  VIN                [#5fd7af]ATSH7E0[/] then [#5fd7af]22F190[/]  UDS read by identifier
  [b]App commands[/b]: [#87afff]:help[/]  [#87afff]:clear[/]  [#87afff]:monitor[/] (show all poller traffic)  [#87afff]:reinit[/] (restore settings)
Write/actuate/flash services (04, 08, 10, 11, 27, 28, 2C, 2E, 2F, 30, 31, 34–37, 3B, 3D, 85, 87) ask first."""

CLEAR_WARNING = """This sends [b]Mode 04[/b] to every module and:

  • erases stored and pending trouble codes
  • erases freeze-frame data (the snapshot of when the fault happened)
  • resets all readiness monitors to [b]incomplete[/b] — the vehicle may fail an
    emissions inspection until a full drive cycle completes them again
  • turns off the check-engine light (it returns if the fault is still present)

[#8a8a8a]Permanent codes stay until the car verifies the repair. Best done with the
engine OFF and ignition ON.[/]"""


class ObdApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "OBD·TUI"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "tab('dash')", "Dash", show=False),
        Binding("2", "tab('live')", "Live", show=False),
        Binding("3", "tab('codes')", "Codes", show=False),
        Binding("4", "tab('vehicle')", "Vehicle", show=False),
        Binding("5", "tab('ext')", "Extended", show=False),
        Binding("6", "tab('console')", "Console", show=False),
        Binding("e", "edit_dashboard", "Edit gauges"),
        Binding("r", "read_codes", "Read codes"),
        Binding("x", "clear_codes", "Clear codes"),
        Binding("f", "freeze_frame", "Freeze frame", show=False),
        Binding("u", "toggle_units", "Units"),
        Binding("l", "toggle_log", "Record"),
        Binding("m", "reset_minmax", "Reset min/max", show=False),
        Binding("ctrl+r", "reconnect", "Reconnect"),
    ]

    def __init__(self, *, host: str | None = None, port: int | None = None, demo: bool = False,
                 imperial: bool | None = None) -> None:
        super().__init__()
        self.config = Config.load()
        if imperial is not None:
            self.config.imperial = imperial
        self.start_host, self.start_port, self.start_demo = host, port, demo
        self.elm: ELM327 | None = None
        self.store = LiveStore()
        self.logger = DataLogger(self.config.log_dir)
        self.profile_errors: list[str] = []
        self.profiles: dict[str, Profile] = load_profiles(self.profile_errors)
        # profile PID name -> (raw value or message, style); raw values are
        # converted for display so the units toggle applies to them.
        self.ext_values: dict[str, tuple[P.Value, str]] = {}
        self.voltage: float | None = None
        self.mil: bool | None = None
        self.state = "offline"
        self.demo_server = None
        self._scanning = False
        self._monitor = False
        self._console_busy = False
        self._loaded: set[str] = set()
        self._console_pending: deque[Text] = deque(maxlen=2000)

    def notify(self, message: str, **kwargs) -> None:  # type: ignore[override]
        # Messages often carry adapter/ECU text or exception strings; never
        # parse them as markup. Widgets/screens forward markup=True explicitly,
        # so the flag is forced off here rather than defaulted.
        kwargs["markup"] = False
        super().notify(message, **kwargs)

    # ---- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status")
        with TabbedContent(id="tabs", initial="dash"):
            with TabPane("① Dashboard", id="dash"):
                yield Grid(id="dash-grid")
            with TabPane("② Live data", id="live"):
                yield DataTable(id="live-table", zebra_stripes=True, cursor_type="row")
            with TabPane("③ Trouble codes", id="codes"):
                yield Static("", id="mil-banner")
                with Horizontal(id="codes-body"):
                    yield DataTable(id="dtc-table", zebra_stripes=True, cursor_type="row")
                    yield Static("", id="freeze-panel")
                with Horizontal(classes="actions"):
                    yield Button("Read codes  r", id="btn-read", variant="primary")
                    yield Button("Freeze frame  f", id="btn-ff")
                    yield Button("Clear codes  x", id="btn-clear", variant="error")
            with TabPane("④ Vehicle", id="vehicle"):
                with VerticalScroll():
                    with Horizontal(id="vehicle-body"):
                        yield Static("", id="vehicle-info")
                        yield Static("", id="readiness")
                    with Horizontal(classes="actions"):
                        yield Button("Refresh", id="btn-vehicle", variant="primary")
            with TabPane("⑤ Extended (Mode 22)", id="ext"):
                with Horizontal(id="ext-top"):
                    yield Select(
                        [(p.name, key) for key, p in self.profiles.items()],
                        id="profile-select",
                        allow_blank=False,
                        value=self.config.profile if self.config.profile in self.profiles
                        else next(iter(self.profiles)),
                    )
                    yield Static("", id="profile-desc")
                yield DataTable(id="ext-table", zebra_stripes=True, cursor_type="row")
                with Horizontal(id="did-scan"):
                    yield Label("DID scan (read-only)")
                    yield Input("7E0", id="scan-header", placeholder="header")
                    yield Input("F180", id="scan-start", placeholder="start")
                    yield Input("F19F", id="scan-end", placeholder="end")
                    yield Button("Scan", id="btn-scan-dids", variant="primary")
                    yield ProgressBar(id="scan-progress", show_eta=False)
                yield DataTable(id="scan-table", zebra_stripes=True)
            with TabPane("⑥ Console", id="console"):
                yield RichLog(id="console-log", markup=True, wrap=True, max_lines=5000)
                yield HistoryInput(placeholder="AT / OBD command  (e.g. ATRV · 010C · 0902 · :help)   esc = leave input",
                                   id="console-input")
        yield Footer()

    def on_mount(self) -> None:
        live = self.query_one("#live-table", DataTable)
        live.add_column("PID", key="pid", width=6)
        live.add_column("Parameter", key="name", width=32)
        live.add_column("Value", key="value", width=14)
        live.add_column("Unit", key="unit", width=7)
        live.add_column("Min", key="min", width=10)
        live.add_column("Max", key="max", width=10)
        live.add_column("Trend", key="trend", width=26)

        dtc = self.query_one("#dtc-table", DataTable)
        for label, key, width in (("Code", "code", 7), ("Status", "kind", 10), ("Module", "ecu", 20),
                                  ("Description", "desc", None)):
            dtc.add_column(label, key=key, width=width)

        ext = self.query_one("#ext-table", DataTable)
        for label, key, width in (("Parameter", "name", 38), ("Header", "hdr", 7), ("Request", "cmd", 9),
                                  ("Value", "value", 28), ("Unit", "unit", 6), ("Source", "src", 12)):
            ext.add_column(label, key=key, width=width)
        scan = self.query_one("#scan-table", DataTable)
        for label, key in (("DID", "did"), ("Bytes", "len"), ("Hex", "hex"), ("ASCII", "ascii")):
            scan.add_column(label, key=key)

        self.query_one("#scan-progress", ProgressBar).display = False

        self.log_console(CONSOLE_HELP)
        for err in self.profile_errors:
            self.notify(f"Skipped profile {err}", severity="warning", timeout=10)
        self.set_interval(1 / 8, self.refresh_ui)
        self.show_connect()

    # ---- connection ----------------------------------------------------------

    def show_connect(self) -> None:
        screen = ConnectScreen(self.config, host=self.start_host, port=self.start_port, demo=self.start_demo)
        self.start_host = self.start_port = None
        self.start_demo = False
        self.push_screen(screen, self.attach)

    def attach(self, elm: ELM327 | None) -> None:
        if elm is None:
            return
        self.elm = elm
        self.state = "online" if elm.vehicle_ok else "adapter"
        elm.transport.listeners.append(self._traffic)
        self.store = LiveStore()
        self._loaded.clear()
        self.build_dashboard()
        self.build_live_table()
        self.build_ext_table()
        self.log_console(f"[#5fd7af]Connected to {escape(elm.version)} at {elm.transport.address} · "
                         f"{elm.protocol_name}[/]")
        if not elm.vehicle_ok:
            self.notify("Adapter connected but the vehicle isn't answering. Turn the ignition ON, "
                        "then press Ctrl+R.", severity="warning", timeout=8)
        self.poll()
        self.on_tab_shown(self.active_tab)

    def action_reconnect(self) -> None:
        self.reconnect()

    @work(exclusive=True, group="reconnect")
    async def reconnect(self) -> None:
        if self.elm and self.elm.transport.connected:
            self.notify("Retrying vehicle detection…")
            try:
                await self.elm.detect_vehicle()
            except (VehicleNotResponding, NoData, ElmError, TransportError) as exc:
                self.notify(f"Vehicle still not responding: {exc}", severity="warning")
                return
            self.state = "online"
            self.after_reconnect()
            self.notify(f"Vehicle online · {self.elm.protocol_name}")
            return
        self.show_connect()

    async def on_unmount(self) -> None:
        # Stop the poller and other workers before tearing down the connection,
        # so nothing touches widgets that are already being removed.
        self.workers.cancel_all()
        self.logger.stop()
        if self.elm:
            await self.elm.close()
        if self.demo_server is not None:
            self.demo_server.close()

    # ---- polling ---------------------------------------------------------------

    @property
    def active_tab(self) -> str:
        try:
            return self.query_one("#tabs", TabbedContent).active or "dash"
        except NoMatches:  # during shutdown the DOM is already gone
            return ""

    def wanted_pids(self) -> list[int]:
        assert self.elm
        tab = self.active_tab
        pids: list[int] = []
        if tab == "dash" or self.logger.active:
            pids += [p for p in self.config.dashboard if p in self.elm.supported]
        if tab == "live":
            pids += [p for p in self.live_pids() if p not in pids]
        return pids

    def live_pids(self) -> list[int]:
        if not self.elm:
            return []
        return sorted((self.elm.supported & set(P.PIDS)), key=lambda p: (P.PIDS[p].category, p))

    @work(exclusive=True, group="poll")
    async def poll(self) -> None:
        last_volt = 0.0
        failures = 0  # consecutive adapter/decode errors
        timeouts = 0  # consecutive transport errors (separate so one doesn't escalate the other)
        while self.elm is not None:
            elm = self.elm
            try:
                if time.monotonic() - last_volt > 3:
                    last_volt = time.monotonic()
                    self.voltage = await elm.voltage()
                if not elm.vehicle_ok:
                    await asyncio.sleep(0.5)
                    continue
                if self.active_tab == "console":
                    await asyncio.sleep(0.3)
                    continue
                if self.active_tab == "ext" and not self._scanning:
                    await self.poll_ext()
                pids = self.wanted_pids()
                if pids:
                    values = await elm.read_decoded(pids)
                    self.store.update(values)
                    self.store.cycle()
                    if self.logger.active:
                        self.logger.write(values)
                else:
                    await asyncio.sleep(0.25)
                failures = timeouts = 0
            except TransportError as exc:
                timeouts += 1
                self.log_console(f"[#ff5f5f]{escape(str(exc))}[/]")
                if elm.transport.connected and timeouts < 2:
                    await asyncio.sleep(0.5)  # one slow reply: resynced, keep going
                    continue
                self.state = "reconnecting"
                if not await self._auto_reconnect(elm):
                    self.state = "offline"
                    self.notify("Lost connection to the adapter.", severity="error", timeout=10)
                    self.elm = None
                    self.show_connect()
                    return
                timeouts = 0
                self.after_reconnect()
            except (ElmError, NegativeResponse) as exc:
                failures += 1
                if failures in (3, 20):
                    self.notify(f"Adapter error: {exc}", severity="warning")
                await asyncio.sleep(min(2.0, 0.2 * failures))
            except Exception as exc:  # never let one bad reply kill the whole app
                failures += 1
                self.log_console(f"[#ff5f5f]Poller error: {type(exc).__name__}: {escape(str(exc))}[/]")
                await asyncio.sleep(min(2.0, 0.2 * failures))

    def after_reconnect(self) -> None:
        """The vehicle may have come online (or changed) — rebuild PID views."""
        self._loaded.clear()
        self.build_dashboard()
        self.build_live_table()

    async def _auto_reconnect(self, elm: ELM327) -> bool:
        for attempt in range(1, 4):
            await asyncio.sleep(1.5 * attempt)
            try:
                await elm.close()
                await elm.connect()
                self.state = "online" if elm.vehicle_ok else "adapter"
                self.notify("Reconnected to adapter")
                return True
            except (TransportError, ElmError):
                continue
        return False

    async def poll_ext(self) -> None:
        assert self.elm
        profile = self.current_profile()
        if not profile:
            return
        for pid in profile.pids:
            if self.active_tab != "ext" or self._scanning:
                return
            try:
                self.ext_values[pid.name] = (await self.elm.read_ext(pid), "bold #5fd7af")
            except NegativeResponse as exc:
                self.ext_values[pid.name] = (f"✖ {NRC.get(exc.code, hex(exc.code))}", "#ff8787")
            except NoData:
                self.ext_values[pid.name] = ("no reply", "#8a8a8a")
            except (ElmError, ValueError) as exc:  # ValueError includes FormulaError
                self.ext_values[pid.name] = (f"✖ {exc}", "#ff8787")
        await asyncio.sleep(0.2)

    # ---- UI refresh --------------------------------------------------------------

    def refresh_ui(self) -> None:
        try:
            self._refresh_ui()
        except NoMatches:
            pass  # timer fired while widgets are being torn down on exit

    def _refresh_ui(self) -> None:
        elm = self.elm
        rec = ""
        if self.logger.active:
            rec = f"{self.logger.rows} rows → {self.logger.path.name if self.logger.path else ''}"
        self.query_one(StatusBar).render_status(
            state=self.state if elm else "offline",
            address=elm.transport.address if elm else "",
            version=elm.version if elm else "",
            protocol=elm.protocol_name if elm and elm.vehicle_ok else "",
            voltage=self.voltage,
            rate=self.store.rate,
            imperial=self.config.imperial,
            recording=rec,
            mil=self.mil,
        )
        tab = self.active_tab
        if tab == "dash":
            for gauge in self.query(Gauge):
                gauge.refresh_from(self.store, self.config.imperial)
        elif tab == "live":
            self.refresh_live_table()
        elif tab == "ext":
            self.refresh_ext_table()

    def build_dashboard(self) -> None:
        # Exclusive: a newer rebuild cancels one still in flight.
        self.run_worker(self._rebuild_dashboard(), exclusive=True, group="dashboard")

    async def _rebuild_dashboard(self) -> None:
        grid = self.query_one("#dash-grid", Grid)
        await grid.remove_children()  # must finish before re-mounting the new gauges
        supported = self.elm.supported if self.elm else set()
        pids = [p for p in self.config.dashboard if p in P.PIDS and (p in supported or not supported)]
        if not pids:
            await grid.mount(Static("No dashboard PIDs supported yet — press [b]e[/b] to choose gauges.",
                                    classes="empty"))
            return
        await grid.mount(*[Gauge(p) for p in pids])
        self.resize_grid()

    def on_resize(self) -> None:
        self.resize_grid()

    def resize_grid(self) -> None:
        try:
            grid = self.query_one("#dash-grid", Grid)
        except Exception:
            return
        cols = max(1, min(5, self.size.width // 34))
        grid.styles.grid_size_columns = cols

    def build_live_table(self) -> None:
        table = self.query_one("#live-table", DataTable)
        table.clear()
        for pid in self.live_pids():
            spec = P.PIDS[pid]
            table.add_row(Text(spec.code, style="#8a8a8a"), spec.name, "—", spec.unit, "", "", "",
                          key=str(pid))

    def refresh_live_table(self) -> None:
        table = self.query_one("#live-table", DataTable)
        imp = self.config.imperial
        now = time.monotonic()
        for pid in self.live_pids():
            if pid not in self.store.values:
                continue
            key = str(pid)
            spec = P.PIDS[pid]
            value, unit = P.convert(self.store.values[pid], spec.unit, imp)
            fresh = now - self.store.stamp.get(pid, 0) < 0.6
            try:
                table.update_cell(key, "value", Text(P.format_value(value, unit),
                                                     style="bold #5fd7af" if fresh else "bold"))
                table.update_cell(key, "unit", unit)
                if pid in self.store.lo:
                    table.update_cell(key, "min", P.format_value(P.convert(self.store.lo[pid], spec.unit, imp)[0], unit))
                    table.update_cell(key, "max", P.format_value(P.convert(self.store.hi[pid], spec.unit, imp)[0], unit))
                    table.update_cell(key, "trend", Text(trend(self.store.history[pid]), style="#87afff"))
            except Exception:
                continue

    # ---- tabs --------------------------------------------------------------------------

    def action_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab

    @on(TabbedContent.TabActivated)
    def _tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.on_tab_shown(event.pane.id or "")

    def on_tab_shown(self, tab: str) -> None:
        # Move focus into the newly shown tab. If focus stayed on a widget in
        # the previous (now hidden) pane, TabbedContent would re-activate that
        # pane and the tab switch would silently bounce back.
        focus = {"live": "#live-table", "codes": "#dtc-table", "ext": "#ext-table",
                 "console": "#console-input", "vehicle": "#btn-vehicle"}.get(tab)
        if focus:
            self.query_one(focus).focus()
        else:
            self.set_focus(None)
        if tab == "console":
            self.call_after_refresh(self._flush_console)
        if not self.elm:
            return
        if tab != "console" and self.elm.settings_dirty:
            self.restore_settings()
        if not self.elm.vehicle_ok or tab in self._loaded:
            return
        if tab == "codes":
            self._loaded.add(tab)
            self.action_read_codes()
        elif tab == "vehicle":
            self._loaded.add(tab)
            self.load_vehicle()

    @work(group="restore")
    async def restore_settings(self) -> None:
        if not self.elm:
            return
        try:
            await self.elm.restore_if_dirty()
        except (TransportError, ElmError) as exc:
            # The poller notices a dead link and runs the reconnect flow.
            self.log_console(f"[#ff5f5f]Could not restore adapter settings: {escape(str(exc))}[/]")
            return
        self.log_console("[#8a8a8a]Adapter settings restored for the live poller.[/]")

    # ---- dashboard -----------------------------------------------------------------------

    def action_edit_dashboard(self) -> None:
        available = [p for p in (self.elm.supported if self.elm else set(P.PIDS)) if p in P.PIDS]
        available = [p for p in sorted(available) if P.PIDS[p].unit]  # numeric gauges only

        def done(result: list[int] | None) -> None:
            if result is None:
                return
            self.config.dashboard = result
            self.config.save()
            self.build_dashboard()

        self.push_screen(PidPickerScreen(available, self.config.dashboard), done)

    def action_toggle_units(self) -> None:
        self.config.imperial = not self.config.imperial
        self.config.save()
        self.notify("Imperial units" if self.config.imperial else "Metric units")

    def action_reset_minmax(self) -> None:
        self.store.reset_minmax()
        self.notify("Min/max reset")

    def action_toggle_log(self) -> None:
        if self.logger.active:
            path = self.logger.stop()
            self.notify(f"Saved {self.logger.rows} rows to {path}", title="Recording stopped", timeout=8)
            return
        if not self.elm or not self.elm.vehicle_ok:
            self.notify("Connect to a vehicle first", severity="warning")
            return
        pids = [p for p in self.config.dashboard if p in self.elm.supported]
        path = self.logger.start(pids)
        self.notify(f"Logging dashboard PIDs to {path}", title="Recording")

    # ---- trouble codes -----------------------------------------------------------------

    @on(Button.Pressed, "#btn-read")
    def action_read_codes(self) -> None:
        self.read_codes()

    @work(exclusive=True, group="dtc")
    async def read_codes(self) -> None:
        if not self._require_vehicle():
            return
        assert self.elm
        banner = self.query_one("#mil-banner", Static)
        banner.update("[#87afff]◌ Reading trouble codes from all modules…[/]")
        try:
            status = await self.elm.monitor_status()
            codes = await self.elm.read_dtcs()
        except (TransportError, ElmError) as exc:
            banner.update(f"[#ff5f5f]✖ {exc}[/]")
            return
        self.mil = status.mil_on if status else None
        self.show_codes(codes)
        stored = sum(1 for c in codes if c.kind == "Stored")
        if status and status.mil_on:
            banner.update(Text.from_markup(
                f"[bold #ff5f5f]⚠ CHECK ENGINE LIGHT ON[/]   {status.dtc_count} emission code(s) reported · "
                f"{len(codes)} total across {len({c.ecu for c in codes})} module(s)"))
        elif codes:
            banner.update(Text.from_markup(
                f"[bold #ffaf00]● MIL off[/]   {stored} stored · {len(codes) - stored} pending/permanent"))
        else:
            banner.update(Text.from_markup("[bold #5fd7af]✔ No trouble codes[/]   MIL off"))

    def show_codes(self, codes: list[Dtc]) -> None:
        table = self.query_one("#dtc-table", DataTable)
        table.clear()
        colors = {"Stored": "#ff8787", "Pending": "#ffaf00", "Permanent": "#d787ff"}
        for i, dtc in enumerate(codes):
            table.add_row(
                Text(dtc.code, style="bold"),
                Text(dtc.kind, style=colors.get(dtc.kind, "")),
                f"{dtc.ecu} {ecu_name(dtc.ecu)}",
                dtc.description,
                key=f"{i}",
            )

    @on(Button.Pressed, "#btn-ff")
    def action_freeze_frame(self) -> None:
        self.load_freeze_frame()

    @work(exclusive=True, group="dtc")
    async def load_freeze_frame(self) -> None:
        if not self._require_vehicle():
            return
        assert self.elm
        panel = self.query_one("#freeze-panel", Static)
        panel.update("[#87afff]◌ Reading freeze frame…[/]")
        try:
            trigger, values = await self.elm.freeze_frame()
        except (TransportError, ElmError) as exc:
            panel.update(f"[#ff5f5f]✖ {exc}[/]")
            return
        if not values and not trigger:
            panel.update("[#8a8a8a]No freeze frame stored.[/]")
            return
        table = Table(title=f"Freeze frame · {trigger or '?'}", title_style="bold #ffaf00",
                      box=None, expand=True, show_header=False)
        table.add_column(style="#8a8a8a")
        table.add_column(justify="right", style="bold")
        for pid, raw in values.items():
            spec = P.PIDS[pid]
            v, unit = P.convert(raw, spec.unit, self.config.imperial)
            table.add_row(spec.name, f"{P.format_value(v, unit)} {unit}")
        panel.update(table)

    @on(Button.Pressed, "#btn-clear")
    def action_clear_codes(self) -> None:
        self.clear_codes()

    @work(exclusive=True, group="dtc")
    async def clear_codes(self) -> None:
        if not self._require_vehicle():
            return
        assert self.elm
        ok = await self.push_screen_wait(ConfirmScreen("Clear trouble codes?", CLEAR_WARNING, "Clear codes"))
        if not ok:
            return
        try:
            acked = await self.elm.clear_dtcs()
        except NegativeResponse as exc:
            hint = " Try with the engine OFF (ignition ON)." if exc.code == 0x22 else ""
            self.notify(f"{exc}.{hint}", severity="error", timeout=10)
            return
        except (TransportError, ElmError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"Codes cleared by {', '.join(acked) or 'no modules'}", title="Mode 04")
        self.query_one("#freeze-panel", Static).update("")
        await asyncio.sleep(1.0)
        self.read_codes()

    def _require_vehicle(self) -> bool:
        if not self.elm or not self.elm.vehicle_ok:
            self.notify("No vehicle connection — ignition ON, then Ctrl+R", severity="warning")
            return False
        return True

    # ---- vehicle ---------------------------------------------------------------------------

    @on(Button.Pressed, "#btn-vehicle")
    def _vehicle_btn(self) -> None:
        self.load_vehicle()

    @work(exclusive=True, group="vehicle")
    async def load_vehicle(self) -> None:
        if not self._require_vehicle():
            return
        assert self.elm
        elm = self.elm
        info_panel = self.query_one("#vehicle-info", Static)
        ready_panel = self.query_one("#readiness", Static)
        info_panel.update("[#87afff]◌ Reading vehicle information…[/]")
        try:
            info = await elm.vehicle_info()
            status = await elm.monitor_status()
            extras = await elm.read_decoded([p for p in (0x1C, 0x51, 0xA6, 0x31, 0x4E, 0x21)
                                             if p in elm.supported])
        except (TransportError, ElmError) as exc:
            info_panel.update(f"[#ff5f5f]✖ {exc}[/]")
            return

        t = Table(title="Vehicle", title_style="bold #5fd7af", box=None, show_header=False, expand=True)
        t.add_column(style="#8a8a8a", width=22)
        t.add_column(style="bold")
        t.add_row("VIN", info.vin or "not reported")
        t.add_row("Protocol", elm.protocol_name)
        t.add_row("Adapter", f"{elm.version} @ {elm.transport.address}")
        t.add_row("Batch requests", "yes" if elm.batching else "no (single PID)")
        for pid in (0x1C, 0x51, 0xA6, 0x31, 0x4E, 0x21):
            if pid in extras:
                spec = P.PIDS[pid]
                v, unit = P.convert(extras[pid], spec.unit, self.config.imperial)
                t.add_row(spec.name, f"{P.format_value(v, unit)} {unit}".strip())
        t.add_row("", "")
        for ecu in elm.ecus:
            t.add_row(f"Module {ecu}", f"{ecu_name(ecu)}  {info.ecu_names.get(ecu, '')}".strip())
        for i, cal in enumerate(info.calibration_ids):
            t.add_row(f"Calibration ID {i + 1}", cal)
        for i, cvn in enumerate(info.cvns):
            t.add_row(f"CVN {i + 1}", cvn)
        info_panel.update(t)

        if status is None:
            ready_panel.update("[#8a8a8a]Readiness monitors not reported.[/]")
            return
        self.mil = status.mil_on
        r = Table(title="Readiness monitors", title_style="bold #5fd7af", box=None, expand=True)
        r.add_column("Monitor", style="#d0d0d0")
        r.add_column("Status", justify="right")
        for name, available, complete in status.monitors:
            if not available:
                r.add_row(Text(name, style="#5f5f5f"), Text("n/a", style="#5f5f5f"))
            elif complete:
                r.add_row(name, Text("✔ complete", style="bold #5fd7af"))
            else:
                r.add_row(name, Text("✖ incomplete", style="bold #ffaf00"))
        mil = "[bold #ff5f5f]MIL ON[/]" if status.mil_on else "[#5fd7af]MIL off[/]"
        r.caption = f"{mil} · {status.dtc_count} emission DTC(s) · {'diesel' if status.diesel else 'spark ignition'}"
        ready_panel.update(r)

    # ---- extended / Mode 22 -----------------------------------------------------------------

    def current_profile(self) -> Profile | None:
        return self.profiles.get(self.config.profile)

    @on(Select.Changed, "#profile-select")
    def _profile_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.config.profile = event.value
            self.config.save()
            self.ext_values.clear()
            self.build_ext_table()

    def build_ext_table(self) -> None:
        table = self.query_one("#ext-table", DataTable)
        table.clear()
        profile = self.current_profile()
        desc = self.query_one("#profile-desc", Static)
        if not profile:
            desc.update("")
            return
        desc.update(Text(" ".join(profile.description.split()), style="#8a8a8a"))
        for pid in profile.pids:
            src = Text("verified", style="#5fd7af") if pid.verified else Text("unverified", style="#ffaf00")
            table.add_row(pid.name, pid.header or "7DF", pid.command, "…", pid.unit, src, key=pid.name)

    def refresh_ext_table(self) -> None:
        table = self.query_one("#ext-table", DataTable)
        profile = self.current_profile()
        if not profile:
            return
        for pid in profile.pids:
            if pid.name not in self.ext_values:
                continue
            raw, style = self.ext_values[pid.name]
            value, unit = P.convert(raw, pid.unit, self.config.imperial)
            text = P.format_value(value, unit) if isinstance(value, (int, float)) else str(value)
            try:
                table.update_cell(pid.name, "value", Text(text, style=style))
                table.update_cell(pid.name, "unit", unit)
            except CellDoesNotExist:
                pass  # profile switched between poll and refresh

    @on(Button.Pressed, "#btn-scan-dids")
    def _scan_dids_btn(self) -> None:
        if self._scanning:
            self._scanning = False
            return
        self.scan_dids()

    @work(exclusive=True, group="didscan")
    async def scan_dids(self) -> None:
        if not self._require_vehicle():
            return
        assert self.elm
        header = self.query_one("#scan-header", Input).value.strip().replace(" ", "").upper()
        try:
            if len(header) not in (3, 6, 8):
                raise ValueError
            int(header, 16)
            start = int(self.query_one("#scan-start", Input).value, 16)
            end = int(self.query_one("#scan-end", Input).value, 16)
        except ValueError:
            self.notify("Header must be 3, 6 or 8 hex digits (e.g. 7E0) and the DID range hex (e.g. F180)",
                        severity="error")
            return
        if not 0 <= start <= end <= 0xFFFF or end - start > 0x1000:
            self.notify("Range must be within 0000–FFFF and ≤ 4096 DIDs", severity="error")
            return
        table = self.query_one("#scan-table", DataTable)
        table.clear()
        bar = self.query_one("#scan-progress", ProgressBar)
        bar.display = True
        bar.update(total=end - start + 1, progress=0)
        button = self.query_one("#btn-scan-dids", Button)
        button.label = "Stop"
        button.variant = "error"
        self._scanning = True
        rx = scan_rx(header)

        def hit(did: int, data: bytes) -> None:
            ascii_ = "".join(chr(c) if 32 <= c < 127 else "·" for c in data)
            table.add_row(Text(f"{did:04X}", style="bold #5fd7af"), str(len(data)),
                          data.hex(" ").upper()[:60], ascii_[:30])

        try:
            hits = await self.elm.scan_dids(
                header, start, end, rx=rx, on_hit=hit,
                on_progress=lambda did: bar.update(progress=did - start + 1),
                cancelled=lambda: not self._scanning,
            )
            verb = "finished" if self._scanning else "stopped"
            self.notify(f"DID scan {verb}: {len(hits)} identifier(s) answered on {header}")
        except TransportError as exc:
            self.notify(f"DID scan aborted – {exc}", severity="error")
        finally:
            self._scanning = False
            button.label = "Scan"
            button.variant = "primary"

    # ---- console ----------------------------------------------------------------------------

    def log_console(self, markup: str) -> None:
        # RichLog wraps to its current width; hidden tabs have none, so buffer
        # lines until the console is actually visible.
        self._console_pending.append(Text.from_markup(markup))
        if self.active_tab == "console":
            self._flush_console()

    def _flush_console(self) -> None:
        try:
            log = self.query_one("#console-log", RichLog)
        except NoMatches:
            return
        if not log.size.width:
            self.call_after_refresh(self._flush_console)
            return
        pending = list(self._console_pending)
        self._console_pending.clear()  # keep the same bounded deque
        width = max(40, log.scrollable_content_region.width - 1)
        for line in pending:
            log.write(line, width=width)

    def _traffic(self, direction: str, text: str) -> None:
        if self._monitor and not self._console_busy:
            arrow = "→" if direction == "tx" else "←"
            self._console_pending.append(Text(f"  {arrow} {text}", style="#5f5f5f"))
            if self.active_tab == "console":
                self._flush_console()

    @on(Input.Submitted, "#console-input")
    def _console_submit(self, event: Input.Submitted) -> None:
        box = self.query_one("#console-input", HistoryInput)
        cmd = event.value.strip()
        box.remember(cmd)
        box.value = ""
        if cmd:
            self.run_console(cmd)

    @work(exclusive=False, group="console")
    async def run_console(self, cmd: str) -> None:
        if cmd.startswith(":"):
            self.app_command(cmd[1:].strip().lower())
            return
        if not self.elm or not self.elm.transport.connected:
            self.log_console("[#ff5f5f]Not connected[/]")
            return
        if not cmd.isascii():
            bad = "".join(sorted({c for c in cmd if not c.isascii()}))
            self.log_console(f"[#ff5f5f]Non-ASCII characters {escape(bad)!r} – adapters only accept plain "
                             "ASCII (check for smart quotes or pasted symbols)[/]")
            return
        compact = cmd.replace(" ", "").upper()
        risk = command_risk(compact)
        if risk:
            ok = await self.push_screen_wait(ConfirmScreen(
                "Potentially unsafe command",
                f"[b]{escape(compact)}[/b] → {risk}\n\nThis can change ECU state or configuration. "
                "Only continue if you know exactly what this does for your vehicle.",
                "Send anyway",
            ))
            if not ok:
                self.log_console(f"[#8a8a8a]Cancelled {escape(compact)}[/]")
                return
        self.log_console(f"[bold #5fd7af]›[/] [bold]{escape(cmd)}[/]")
        self._console_busy = True
        started = time.perf_counter()
        try:
            lines = await self.elm.raw(cmd)
        except (TransportError, ValueError) as exc:
            self.log_console(f"[#ff5f5f]{escape(str(exc))}[/]")
            return
        finally:
            self._console_busy = False
        ms = (time.perf_counter() - started) * 1000
        for line in lines:
            self.log_console(f"  {escape(line)}")
        self.log_console(f"  [#5f5f5f]{ms:.0f} ms[/]")
        for note in self.annotate(compact, lines):
            self.log_console(f"  [#87afff]↳ {escape(note)}[/]")

    def annotate(self, compact: str, lines: list[str]) -> list[str]:
        if not self.elm or self.elm.format_dirty or compact.startswith("AT"):
            return []
        try:
            msgs = parse_frames(check_errors(lines), self.elm.protocol)
        except (ElmError, ValueError):
            return []
        notes: list[str] = []
        for m in msgs:
            if m.negative:
                notes.append(f"{m.ecu}: negative response – {NRC.get(m.data[2], hex(m.data[2]))}")
            elif m.service == 0x41:
                for pid, data in _parse_mode01([m]).items():
                    spec = P.PIDS.get(pid)
                    if spec:
                        v, unit = P.convert(P.decode(pid, data), spec.unit, self.config.imperial)
                        notes.append(f"{m.ecu} {spec.name} = {P.format_value(v, unit)} {unit}".rstrip())
                    elif pid % 0x20 == 0:
                        got = P.decode_support_bitmap(pid, data)
                        notes.append(f"{m.ecu} supports PIDs {', '.join(f'{p:02X}' for p in sorted(got))}")
            elif m.service in (0x43, 0x47, 0x4A):
                from elm327obd.dtc import describe, parse_dtc_payload

                codes = parse_dtc_payload(m.data[1:], has_count=is_can(self.elm.protocol))
                notes += [f"{m.ecu} {c}: {describe(c)}" for c in codes] or [f"{m.ecu}: no codes"]
            elif m.service in (0x49, 0x62):
                text = "".join(chr(c) for c in m.data[3:] if 32 <= c < 127)
                if len(text) >= 4:
                    notes.append(f"{m.ecu} \"{text}\"")
            elif m.service == 0x44:
                notes.append(f"{m.ecu}: codes cleared")
        return notes

    def app_command(self, cmd: str) -> None:
        log = self.query_one("#console-log", RichLog)
        if cmd in ("help", "h", "?"):
            self.log_console(CONSOLE_HELP)
        elif cmd in ("clear", "cls"):
            log.clear()
        elif cmd.startswith("monitor"):
            self._monitor = not self._monitor
            self.log_console(f"Traffic monitor {'on' if self._monitor else 'off'}")
        elif cmd == "reinit":
            if self.elm:
                self.elm.settings_dirty = True
                self.restore_settings()
        else:
            self.log_console(f"[#ff5f5f]Unknown command :{escape(cmd)}[/] – try :help")


def run(**kwargs) -> None:
    ObdApp(**kwargs).run()
