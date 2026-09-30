"""UI regression tests: failures that used to exit the whole app.

``run_test`` re-raises any exception that escaped a worker or handler, so a
test finishing normally means the app survived.
"""

from textual.widgets import DataTable, Input

from elm327obd.emulator import Vehicle
from elm327obd.tui.app import ObdApp

from conftest import start_emulator, wait_until

SIZE = (140, 45)


async def _app_on(port: int, **kwargs) -> ObdApp:
    return ObdApp(host="127.0.0.1", port=port, **kwargs)


async def test_can_error_on_connect_shows_adapter_only():
    _, server, port = await start_emulator(Vehicle(bus_error=True))
    app = await _app_on(port)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_until(pilot, lambda: app.elm is not None)
        assert app.state == "adapter"
        await pilot.press("3")  # codes tab needs a vehicle: must warn, not crash
        await pilot.pause(0.3)
    server.close()


async def test_restore_after_link_drop_does_not_crash():
    _, server, port = await start_emulator()
    app = await _app_on(port)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_until(pilot, lambda: app.elm is not None and app.elm.vehicle_ok)
        await pilot.press("6")
        await app.run_console("ATH0").wait()
        assert app.elm.format_dirty
        old_writer = app.elm.transport._writer
        old_writer.close()  # Wi-Fi drops while on the console tab
        await pilot.press("escape", "1")  # leaving the console triggers the restore
        # The poller notices and reconnects (the emulator is still up).
        assert await wait_until(pilot, lambda: app.elm and app.elm.transport._writer not in (None, old_writer)
                                and app.state == "online", timeout=15)
    server.close()


async def test_adapter_only_link_drop_is_detected():
    _, server, port = await start_emulator(Vehicle(ignition=False))
    app = await _app_on(port)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_until(pilot, lambda: app.elm is not None)
        assert app.state == "adapter"
        old_writer = app.elm.transport._writer
        old_writer.close()
        # voltage() now raises, so the poller reconnects instead of idling forever.
        assert await wait_until(pilot, lambda: app.elm and app.elm.transport._writer not in (None, old_writer),
                                timeout=15)
    server.close()


async def test_extended_tab_formula_error_and_imperial(isolated_config):
    user_dir = isolated_config / "profiles"
    user_dir.mkdir()
    (user_dir / "zz-bad.toml").write_text(
        'name = "Bad formula"\n[[pid]]\nname = "div0"\nheader = "7E0"\ncommand = "221E1C"\n'
        'formula = "1 / (A - A)"\n'
    )
    _, server, port = await start_emulator()
    app = await _app_on(port, imperial=True)
    app.config.profile = "ford"
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_until(pilot, lambda: app.elm is not None and app.elm.vehicle_ok)
        await pilot.press("5")
        assert await wait_until(pilot, lambda: "Transmission fluid temp" in app.ext_values)
        await pilot.pause(0.3)
        table = app.query_one("#ext-table", DataTable)
        assert table.get_cell("Transmission fluid temp", "unit") == "°F"
        assert float(str(table.get_cell("Transmission fluid temp", "value"))) > 120  # ~72 °C
        # Switch to the broken profile: the poller must survive the ZeroDivisionError.
        app.query_one("#profile-select").value = "zz-bad"
        assert await wait_until(pilot, lambda: "div0" in app.ext_values)
        assert str(app.ext_values["div0"][0]).startswith("✖")
        await pilot.press("1")
        assert app.active_tab == "dash"
        assert await wait_until(pilot, lambda: app.store.rate > 0)  # poller still alive
    server.close()


async def test_console_non_ascii_and_bad_did_header():
    _, server, port = await start_emulator()
    app = await _app_on(port)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_until(pilot, lambda: app.elm is not None and app.elm.vehicle_ok)
        await pilot.press("6")
        await app.run_console("ATSH7E0’").wait()
        await app.run_console("[bold]010C").wait()  # markup-looking input must not break the log
        await pilot.pause(0.2)
        log = app.query_one("#console-log")
        text = "\n".join(strip.text for strip in log.lines)
        assert "Non-ASCII" in text
        assert "[bold]010C" in text  # shown literally, not parsed as markup
        await pilot.press("escape", "5")
        app.query_one("#scan-header", Input).value = "7EO"  # letter O, not zero
        await pilot.click("#btn-scan-dids")
        await pilot.pause(0.3)
        assert not app._scanning
        assert app.query_one("#scan-table", DataTable).row_count == 0
    server.close()


async def test_console_buffer_is_bounded():
    app = ObdApp()
    async with app.run_test(size=SIZE) as pilot:
        for i in range(5000):
            app.log_console(f"line {i}")
        assert len(app._console_pending) <= 2000


async def test_number_keys_switch_from_every_tab():
    _, server, port = await start_emulator()
    app = await _app_on(port)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_until(pilot, lambda: app.elm is not None and app.elm.vehicle_ok)
        tabs = {"1": "dash", "2": "live", "3": "codes", "4": "vehicle", "5": "ext", "6": "console"}
        for src_key, src in tabs.items():
            for dst_key, dst in tabs.items():
                if src == dst:
                    continue
                for key in (src_key, dst_key):
                    if app.active_tab == "console":
                        await pilot.press("escape")  # leave the input so digits aren't typed
                    await pilot.press(key)
                    await pilot.pause(0.05)
                await pilot.pause(0.05)
                assert app.active_tab == dst, f"{src} -> {dst} ended on {app.active_tab}"
    server.close()


async def test_console_buffer_stays_bounded_after_flush():
    from collections import deque

    app = ObdApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        await pilot.press("escape")  # dismiss the connect screen
        app.action_tab("console")
        await pilot.pause(0.3)  # flush happens here
        app.action_tab("dash")
        await pilot.pause(0.1)
        for i in range(5000):
            app.log_console(f"line {i}")
        assert isinstance(app._console_pending, deque)
        assert len(app._console_pending) <= 2000


async def test_screen_notifications_never_parse_markup():
    app = ObdApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        message = "ECU said [/bogus] and [b]"
        app.screen.notify(message)  # screens forward markup=True explicitly
        await pilot.pause(0.1)
        latest = list(app._notifications)[-1]
        assert latest.message == message
        assert latest.markup is False  # shown literally, never parsed


async def test_views_rebuilt_after_auto_reconnect():
    vehicle, server, port = await start_emulator(Vehicle(ignition=False))
    app = await _app_on(port)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_until(pilot, lambda: app.elm is not None)
        assert app.query_one("#live-table", DataTable).row_count == 0
        vehicle.ignition = True  # key turned on…
        app.elm.transport._writer.close()  # …and the Wi-Fi hiccups
        assert await wait_until(pilot, lambda: app.elm and app.elm.vehicle_ok and app.state == "online",
                                timeout=15)
        assert await wait_until(pilot, lambda: app.query_one("#live-table", DataTable).row_count > 10)
        await pilot.press("1")
        assert await wait_until(pilot, lambda: app.store.rate > 0)
    server.close()


async def test_rapid_tab_switching_never_bounces():
    """Keys pressed with no pause at all, so focus calls are still in flight."""
    _, server, port = await start_emulator()
    app = await _app_on(port)
    async with app.run_test(size=SIZE) as pilot:
        assert await wait_until(pilot, lambda: app.elm is not None and app.elm.vehicle_ok)
        sequence = "5251535242541524" * 3
        for key in sequence:
            await pilot.press(key)
        await pilot.pause(0.5)  # let every queued focus/activation message land
        assert app.active_tab == {"1": "dash", "2": "live", "3": "codes", "4": "vehicle", "5": "ext"}[sequence[-1]]
    server.close()


async def test_late_pane_focus_message_does_not_switch_tab():
    """Deterministic version of the race: a focus event for the tab the user
    just left arrives after they've moved on. Stock TabbedContent obeys it."""
    from textual.widgets import TabPane

    app = ObdApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        await pilot.press("escape")  # dismiss the connect screen
        app.action_tab("live")
        await pilot.pause(0.2)
        ext_pane = app.query_one("#ext", TabPane)
        ext_pane.post_message(TabPane.Focused(ext_pane))  # the late, stale message
        await pilot.pause(0.3)
        assert app.active_tab == "live"


async def test_codes_tab_severity_sorting_and_detail_panel():
    _, server, port = await start_emulator()
    app = await _app_on(port)
    async with app.run_test(size=(160, 50)) as pilot:
        assert await wait_until(pilot, lambda: app.elm is not None and app.elm.vehicle_ok)
        await pilot.press("3")
        table = app.query_one("#dtc-table", DataTable)
        assert await wait_until(pilot, lambda: table.row_count == 5)
        # Most urgent first: SOON codes, then the LOW catalyst codes last.
        assert [d.guide.severity for d in app._codes] == ["soon", "soon", "soon", "low", "low"]
        assert app._codes[0].code == "P0301" and app._codes[-1].kind == "Permanent"
        banner = str(app.query_one("#mil-banner").render())
        assert "Most urgent" in banner and "P0301" in banner

        def detail_text() -> str:
            from rich.console import Console

            console = Console(width=60, record=True)
            console.print(app.query_one("#dtc-detail").content)
            return console.export_text()

        assert await wait_until(pilot, lambda: "Engine RPM" in detail_text() and "rpm" in detail_text())
        text = detail_text()
        assert "Likely causes" in text and "ignition coil" in text.lower()
        assert "Stored – a confirmed fault" in text
        # Moving the cursor updates the panel.
        await pilot.press("down")
        assert await wait_until(pilot, lambda: "P0741" in detail_text())
    server.close()
