import asyncio

import pytest

from elm327obd import config, dtc, profiles
from elm327obd.elm import ELM327
from elm327obd.emulator import Vehicle, serve
from elm327obd.transport import ElmTransport


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Never read or write the real ~/.config/elm327obd during tests."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(profiles, "USER_PROFILE_DIR", tmp_path / "profiles")
    monkeypatch.setattr(dtc, "USER_DTC_FILE", tmp_path / "cfg" / "dtc.csv")
    monkeypatch.setattr(dtc, "USER_GUIDE_FILE", tmp_path / "cfg" / "dtc_guide.toml")
    monkeypatch.setattr(dtc, "_user_codes", None)  # force reload from the patched paths
    monkeypatch.setattr(dtc, "_guide", None)
    monkeypatch.setattr(dtc, "GUIDE_ERRORS", [])
    return tmp_path


async def start_emulator(vehicle: Vehicle | None = None):
    vehicle = vehicle or Vehicle()
    server = await serve("127.0.0.1", 0, vehicle)
    return vehicle, server, server.sockets[0].getsockname()[1]


async def connected_client(port: int) -> ELM327:
    client = ELM327(ElmTransport("127.0.0.1", port))
    await client.connect()
    return client


async def wait_until(pilot, condition, timeout: float = 10.0, step: float = 0.1) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return True
        await pilot.pause(step)
    return condition()
