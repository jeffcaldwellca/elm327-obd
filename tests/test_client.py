import pytest

from elm327obd.elm import ELM327
from elm327obd.emulator import VIN, Vehicle, serve
from elm327obd.profiles import load_profiles
from elm327obd.protocol import NegativeResponse
from elm327obd.transport import ElmTransport, TransportError


@pytest.fixture
async def emu():
    vehicle = Vehicle()
    server = await serve("127.0.0.1", 0, vehicle)
    port = server.sockets[0].getsockname()[1]
    yield vehicle, port, server
    server.close()


@pytest.fixture
async def elm(emu):
    _, port, _ = emu
    client = ELM327(ElmTransport("127.0.0.1", port))
    await client.connect()
    yield client
    await client.close()


async def test_connect(elm):
    assert elm.vehicle_ok
    assert elm.protocol == "6"
    assert elm.ecus == ["7E8", "7E9"]
    assert {0x0C, 0x0D, 0x05, 0xA6} <= elm.supported
    assert "ELM327" in elm.version


async def test_batched_read(elm):
    values = await elm.read_decoded([0x0C, 0x0D, 0x05, 0x04, 0x11, 0x0B, 0x0F])
    assert set(values) == {0x0C, 0x0D, 0x05, 0x04, 0x11, 0x0B, 0x0F}
    assert 600 < values[0x0C] < 4500
    assert elm.batching


async def test_dtcs_and_clear(elm, emu):
    codes = {(d.code, d.kind, d.ecu) for d in await elm.read_dtcs()}
    assert ("P0301", "Stored", "7E8") in codes
    assert ("P0741", "Stored", "7E9") in codes
    assert ("P0171", "Pending", "7E8") in codes
    assert ("P0420", "Permanent", "7E8") in codes
    assert sorted(await elm.clear_dtcs()) == ["7E8", "7E9"]
    assert [(d.code, d.kind) for d in await elm.read_dtcs()] == [("P0420", "Permanent")]


async def test_vehicle_info_and_freeze_frame(elm):
    info = await elm.vehicle_info()
    assert info.vin == VIN
    assert info.calibration_ids == ["EMU-CAL-0001"]
    trigger, values = await elm.freeze_frame()
    assert trigger == "P0301"
    assert values[0x0C] == 750.0


async def test_extended_profiles(elm):
    ford = load_profiles()["ford"]
    by_name = {p.name: p for p in ford.pids}
    assert await elm.read_ext(by_name["PCM VIN (UDS F190)"]) == VIN
    assert 60 < await elm.read_ext(by_name["Transmission fluid temp"]) < 90
    gm = {p.name: p for p in load_profiles()["gm"].pids}
    assert 60 < await elm.read_ext(gm["Transmission fluid temp"]) < 90
    # header switched back to functional for normal OBD requests
    assert 0x0D in await elm.read_decoded([0x0D])


async def test_negative_response(elm):
    hy = {p.name: p for p in load_profiles()["hyundai"].pids}
    with pytest.raises(NegativeResponse) as exc:
        await elm.read_ext(hy["ECU software version (UDS F195)"])
    assert exc.value.code == 0x31


async def test_console_format_change_is_restored(elm):
    await elm.raw("ATH0")
    assert elm.settings_dirty
    await elm.restore_if_dirty()
    assert not elm.settings_dirty
    assert 0x0C in await elm.read_decoded([0x0C])


async def test_did_scan(elm):
    hits = await elm.scan_dids("7E0", 0xF185, 0xF191)
    assert set(hits) == {0xF188, 0xF190}


async def test_ignition_off():
    server = await serve("127.0.0.1", 0, Vehicle(ignition=False))
    port = server.sockets[0].getsockname()[1]
    client = ELM327(ElmTransport("127.0.0.1", port))
    await client.connect()
    assert not client.vehicle_ok
    assert await client.voltage() == 12.4
    await client.close()
    server.close()


async def test_no_adapter():
    client = ELM327(ElmTransport("127.0.0.1", 1, connect_timeout=1))
    with pytest.raises(TransportError):
        await client.connect()


async def test_connection_drop(elm):
    elm.transport._writer.close()
    with pytest.raises(TransportError):
        await elm.read_pids([0x0C])
