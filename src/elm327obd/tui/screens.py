"""Connect screen and modal dialogs."""

from __future__ import annotations

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, RichLog, SelectionList, Static
from textual.widgets.selection_list import Selection

from elm327obd import pids as P
from elm327obd import scanner
from elm327obd.config import Config
from elm327obd.elm import ELM327
from elm327obd.protocol import ElmError
from elm327obd.transport import ElmTransport, TransportError

LOGO = r"""
 ___  ___  ___      _____ _   _ ___
/ _ \| _ )|   \ ___|_   _| | | |_ _|
| (_) | _ \| |) |___| | | | |_| || |
\___/|___/|___/      |_|  \___/|___|
"""


def _logo() -> Text:
    text = Text(justify="center")
    colors = ["#5fd7af", "#5fd7d7", "#5fafff", "#8787ff"]
    for i, line in enumerate(LOGO.strip("\n").splitlines()):
        text.append(line + "\n", style=f"bold {colors[i % len(colors)]}")
    text.append("ELM327 WiFi diagnostics for macOS", style="italic #8a8a8a")
    return text


class ConnectScreen(Screen[ELM327]):
    BINDINGS = [
        Binding("s", "scan", "Rescan"),
        Binding("d", "demo", "Demo mode"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self, config: Config, *, host: str | None = None, port: int | None = None,
                 demo: bool = False) -> None:
        super().__init__()
        self.config = config
        self.host = host
        self.port = port
        self.demo = demo
        self._rows: dict[tuple[str, int], str] = {}

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="connect-box"):
                yield Static(_logo(), id="logo")
                yield Static(
                    "[b]1.[/b] Plug the adapter into the OBD port and turn the ignition [b]ON[/b]\n"
                    "[b]2.[/b] Join the adapter's Wi-Fi on this Mac (often [b]WiFi_OBDII[/b], "
                    "[b]V-LINK[/b] or [b]OBDII[/b])\n"
                    "[b]3.[/b] Pick the adapter below, or press [b]d[/b] to explore with the built-in emulator",
                    id="steps",
                )
                yield Static("", id="wifi")
                yield DataTable(id="candidates", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="connect-actions"):
                    yield Input(placeholder="host:port  e.g. 192.168.0.10:35000", id="manual")
                    yield Button("Connect", variant="primary", id="btn-connect")
                    yield Button("Scan", id="btn-scan")
                    yield Button("Demo", variant="warning", id="btn-demo")
                yield RichLog(id="connect-log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#candidates", DataTable)
        table.add_column("Adapter", key="addr", width=24)
        table.add_column("Status", key="status")
        ssid = scanner.wifi_network()
        gw = scanner.default_gateway()
        wifi = self.query_one("#wifi", Static)
        wifi.update(
            f"[#8a8a8a]Wi-Fi:[/] [b]{ssid or 'unknown (macOS hides SSID without Location access)'}[/b]"
            f"   [#8a8a8a]Gateway:[/] [b]{gw or 'none'}[/b]"
        )
        if self.config.host and self.config.port:
            self.query_one("#manual", Input).value = f"{self.config.host}:{self.config.port}"
        if self.demo:
            self.action_demo()
        elif self.host:
            self.connect(self.host, self.port or 35000)
        else:
            self.action_scan()

    def log_line(self, markup: str) -> None:
        self.query_one("#connect-log", RichLog).write(markup)

    # ---- scanning ----------------------------------------------------------

    def _set_row(self, host: str, port: int, status: str) -> None:
        table = self.query_one("#candidates", DataTable)
        key = f"{host}:{port}"
        if (host, port) not in self._rows:
            table.add_row(key, status, key=key)
        else:
            table.update_cell(key, "status", status)
        self._rows[(host, port)] = status

    def action_scan(self) -> None:
        self.scan()

    @work(exclusive=True, group="scan")
    async def scan(self) -> None:
        self.log_line("[#87afff]Scanning for ELM327 adapters…[/]")
        extra = [(self.config.host, self.config.port)] if self.config.host and self.config.port else []
        for host, port in scanner.candidates(extra):
            self._set_row(host, port, Text("◌ probing…", style="#8a8a8a"))

        def result(host: str, port: int, found: scanner.Found | None) -> None:
            if found:
                self._set_row(host, port, Text(f"✔ {found.banner}", style="bold #5fd7af"))
            else:
                self._set_row(host, port, Text("✖ no adapter", style="#5f5f5f"))

        found = await scanner.scan(extra, on_result=result)
        if not found:
            self.log_line(
                "[#ffaf00]No adapter found.[/] Check you're joined to the adapter's Wi-Fi, "
                "then press [b]s[/b] to rescan or type an address."
            )
            return
        first = found[0]
        self.query_one("#manual", Input).value = f"{first.host}:{first.port}"
        self.log_line(f"[#5fd7af]Found {escape(first.banner)} at {first.host}:{first.port}[/]")
        if len(found) == 1:
            self.connect(first.host, first.port)

    # ---- connecting --------------------------------------------------------

    @on(DataTable.RowSelected, "#candidates")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        host, port = str(event.row_key.value).rsplit(":", 1)
        self.connect(host, int(port))

    @on(Button.Pressed, "#btn-connect")
    @on(Input.Submitted, "#manual")
    def _manual(self) -> None:
        value = self.query_one("#manual", Input).value.strip()
        if not value:
            self.notify("Enter host:port or pick an adapter", severity="warning")
            return
        host, _, port = value.partition(":")
        try:
            port_num = int(port or 35000)
            if not host or not 0 < port_num < 65536:
                raise ValueError
        except ValueError:
            self.notify("Address must look like 192.168.0.10:35000", severity="error")
            return
        self.connect(host, port_num)

    @on(Button.Pressed, "#btn-scan")
    def _scan_btn(self) -> None:
        self.action_scan()

    @on(Button.Pressed, "#btn-demo")
    def _demo_btn(self) -> None:
        self.action_demo()

    @work(exclusive=True, group="connect")
    async def action_demo(self) -> None:
        from elm327obd.emulator import serve

        if getattr(self.app, "demo_server", None) is None:
            server = await serve("127.0.0.1", 0)
            self.app.demo_server = server  # type: ignore[attr-defined]
        port = self.app.demo_server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
        self.log_line(f"[#ffaf00]Demo mode:[/] emulated vehicle on 127.0.0.1:{port}")
        await self._connect("127.0.0.1", port, remember=False)

    @work(exclusive=True, group="connect")
    async def connect(self, host: str, port: int) -> None:
        await self._connect(host, port, remember=True)

    async def _connect(self, host: str, port: int, *, remember: bool) -> None:
        self.query_one("#btn-connect", Button).disabled = True
        elm = ELM327(ElmTransport(host, port))
        try:
            await elm.connect(progress=lambda m: self.log_line(f"[#8a8a8a]›[/] {escape(m)}"))
        except (TransportError, ElmError, ValueError) as exc:
            # ElmError: adapter answered setup commands with an error (odd clones).
            self.log_line(f"[#ff5f5f]✖ {escape(str(exc))}[/]")
            await elm.close()
            return
        finally:
            self.query_one("#btn-connect", Button).disabled = False
        if remember:
            self.config.host, self.config.port = host, port
            self.config.save()
        self.dismiss(elm)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, body: str, confirm: str = "Confirm", danger: bool = True) -> None:
        super().__init__()
        self.title_text = title
        self.body = body
        self.confirm = confirm
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="danger" if self.danger else ""):
            yield Label(self.title_text, id="dialog-title")
            yield Static(self.body, id="dialog-body")
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", id="no")
                yield Button(self.confirm, variant="error" if self.danger else "primary", id="yes")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class PidPickerScreen(ModalScreen[list[int] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "save", "Save")]
    MAX = 16

    def __init__(self, available: list[int], current: list[int]) -> None:
        super().__init__()
        self.available = available
        self.current = current

    def compose(self) -> ComposeResult:
        ordered = [p for p in self.current if p in self.available]
        ordered += [p for p in self.available if p not in ordered]
        with Vertical(id="picker"):
            yield Label(f"Dashboard gauges  [dim](space to toggle · up to {self.MAX})[/]", id="dialog-title")
            yield SelectionList[int](
                *[
                    Selection(f"{P.PIDS[p].name}  [dim]{P.PIDS[p].code} · {P.PIDS[p].unit}[/]", p,
                              p in self.current)
                    for p in ordered
                ],
                id="picker-list",
            )
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", id="no")
                yield Button("Save", variant="primary", id="yes")

    @on(Button.Pressed, "#yes")
    def action_save(self) -> None:
        chosen = self.query_one("#picker-list", SelectionList).selected
        keep = [p for p in self.current if p in chosen]
        keep += [p for p in chosen if p not in keep]
        if len(keep) > self.MAX:
            self.notify(f"Pick at most {self.MAX} gauges", severity="warning")
            return
        self.dismiss(keep)

    @on(Button.Pressed, "#no")
    def action_cancel(self) -> None:
        self.dismiss(None)
