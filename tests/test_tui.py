from elm327obd import config
from elm327obd.datalog import DataLogger
from elm327obd.tui.app import ObdApp
from textual.widgets import DataTable


async def test_demo_walkthrough(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    app = ObdApp(demo=True)
    async with app.run_test(size=(140, 45)) as pilot:
        for _ in range(50):
            if app.elm:
                break
            await pilot.pause(0.2)
        assert app.elm and app.elm.vehicle_ok
        app.logger = DataLogger(tmp_path)
        await pilot.pause(1.5)
        assert app.store.values, "dashboard should be polling"
        for key in "234561":
            await pilot.press(key)
            await pilot.pause(0.8)
            if key == "6":
                await pilot.press("escape")
        await pilot.press("3")
        await pilot.pause(1.5)
        assert app.query_one("#dtc-table", DataTable).row_count == 5
