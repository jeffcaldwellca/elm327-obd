"""Regression tests for issues found in code review (client / backend)."""

import asyncio

import pytest

from elm327obd.elm import ELM327
from elm327obd.emulator import Vehicle
from elm327obd.profiles import FormulaError, evaluate, load_profiles
from elm327obd.protocol import functional_header, parse_dpn
from elm327obd.transport import ElmTransport, TransportError
from elm327obd.tui.app import command_risk

from conftest import connected_client, start_emulator


@pytest.fixture
async def emu():
    vehicle, server, port = await start_emulator()
    yield vehicle, port
    server.close()


@pytest.fixture
async def elm(emu):
    client = await connected_client(emu[1])
    yield client
    await client.close()


def _pid(profile: str, name: str):
    return next(p for p in load_profiles()[profile].pids if p.name == name)


# 1. restoring settings must really reset the adapter's header ---------------

async def test_console_header_is_reset_on_restore(elm):
    await elm.raw("ATSH7E1")  # console: talk to the TCM only
    assert elm.settings_dirty
    assert not elm.format_dirty  # ATSH doesn't change reply format → annotations stay on
    await elm.restore_if_dirty()
    # If the adapter were still on 7E1 only the TCM would answer: no RPM, no ECM codes.
    assert 0x0C in await elm.read_decoded([0x0C])
    assert "7E8" in {d.ecu for d in await elm.read_dtcs()}


async def test_format_commands_mark_format_dirty(elm):
    await elm.raw("ATH0")
    assert elm.settings_dirty and elm.format_dirty
    await elm.raw("ATRV")  # read-only AT command
    await elm.restore_if_dirty()
    assert not elm.settings_dirty and not elm.format_dirty


async def test_console_reset_clears_cached_header(elm):
    await elm.raw("ATSH7E0")
    await elm.raw("ATZ")
    assert elm._header is None and elm._rx is None


# 2. timeout recovery must not duplicate/shift replies ------------------------

async def test_timeout_does_not_shift_later_replies():
    _, server, port = await start_emulator(Vehicle(slow={"010C": 0.6}))
    client = await connected_client(port)
    try:
        with pytest.raises(TransportError, match="Timed out"):
            await client.transport.command("010C", timeout=0.2)
        # A bare-CR resync made the adapter repeat 010C, so these got stale replies.
        assert (await client.transport.command("ATRV", timeout=3))[0].endswith("V")
        assert "ELM327" in (await client.transport.command("ATI", timeout=3))[0]
        assert 0x0D in await client.read_decoded([0x0D])
    finally:
        await client.close()
        server.close()


# 3. re-detection must broadcast, not reuse the last physical header -----------

async def test_redetect_after_physical_request(elm):
    assert await elm.read_ext(_pid("gm", "Transmission fluid temp"))  # leaves ATSH7E2
    await elm.detect_vehicle()
    assert elm.ecus == ["7E8", "7E9"]
    assert {0x0C, 0x0D} <= elm.supported


# 4. adapter error strings during connect don't propagate ---------------------

async def test_connect_survives_can_error():
    _, server, port = await start_emulator(Vehicle(bus_error=True))
    client = ELM327(ElmTransport("127.0.0.1", port))
    try:
        await client.connect()  # used to raise ElmError("CAN ERROR")
        assert not client.vehicle_ok
    finally:
        await client.close()
        server.close()


# 6. formula errors are FormulaError (a ValueError), never raw exceptions ------

@pytest.mark.parametrize("formula", ["A / B", "u16(A)", "2 ** 99999", "1 << 1000", "min()"])
def test_formula_runtime_errors_are_wrapped(formula):
    with pytest.raises(FormulaError):
        evaluate(formula, b"\x05\x00")


# 7. voltage() reports a dead link instead of hiding it -----------------------

async def test_voltage_raises_when_disconnected(elm):
    elm.transport._writer.close()
    with pytest.raises(TransportError):
        await elm.voltage()


# 8. non-ASCII input is rejected cleanly ---------------------------------------

async def test_non_ascii_command_rejected(elm):
    with pytest.raises(ValueError, match="ASCII"):
        await elm.raw("ATSH7E0’")
    with pytest.raises(ValueError):
        await elm.raw("ATI\r010C")  # embedded CR would smuggle a second command
    assert "ELM327" in (await elm.raw("ATI"))[0]  # link still in sync


# 9. write/actuate services need confirmation ----------------------------------

@pytest.mark.parametrize("cmd", ["04", "0801", "3001 07", "3B0112", "8701", "2EF190", "31010203",
                                 "1003", "ATPP 0C SV 23"])
def test_risky_commands_flagged(cmd):
    assert command_risk(cmd)


@pytest.mark.parametrize("cmd", ["010C", "0902", "03", "22F190", "1001", "ATRV", "ATSH7E0", "3E00"])
def test_read_only_commands_not_flagged(cmd):
    assert command_risk(cmd) is None


# 10. functional headers per protocol ------------------------------------------

@pytest.mark.parametrize("protocol,header", [
    ("1", "616AF1"), ("2", "686AF1"), ("3", "686AF1"), ("4", "C133F1"), ("5", "C133F1"),
    ("6", "7DF"), ("7", "18DB33F1"), ("8", "7DF"), ("9", "18DB33F1"),
])
def test_functional_header(protocol, header):
    assert functional_header(protocol) == header


# low-severity findings ----------------------------------------------------------

@pytest.mark.parametrize("reply,protocol", [
    (["A6"], "6"), (["6"], "6"), (["A"], "A"), (["A3"], "3"), ([], "0"), (["?"], "0"), (["AZ"], "0"),
])
def test_parse_dpn(reply, protocol):
    assert parse_dpn(reply) == protocol


async def test_freeze_frame_uses_support_bitmap(elm):
    sent: list[str] = []
    elm.transport.listeners.append(lambda d, t: sent.append(t) if d == "tx" and t.startswith("02") else None)
    trigger, values = await elm.freeze_frame()
    assert trigger == "P0301"
    assert set(values) == {0x04, 0x05, 0x0C, 0x0D, 0x11}
    # 020200 + 020000 bitmap + one request per stored PID — not every Mode 01 PID.
    assert len(sent) == 2 + len(values)


async def test_did_scan_stops_when_link_drops(elm):
    def drop(did: int) -> None:
        if did == 0xF184:
            elm.transport._writer.close()

    with pytest.raises(TransportError):
        await elm.scan_dids("7E0", 0xF180, 0xF19F, on_progress=drop)


async def test_open_timeout_message(monkeypatch):
    async def never(*_a, **_k):
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "open_connection", never)
    with pytest.raises(TransportError, match="timed out"):
        await ElmTransport("192.0.2.1", 35000).open()


def test_malformed_user_profiles_are_skipped(isolated_config):
    user_dir = isolated_config / "profiles"
    user_dir.mkdir()
    (user_dir / "broken.toml").write_text("name = [\n")
    (user_dir / "badhex.toml").write_text('[[pid]]\nname = "x"\ncommand = "22ZZ"\n')
    (user_dir / "nocmd.toml").write_text('[[pid]]\nname = "x"\n')
    (user_dir / "good.toml").write_text('[[pid]]\nname = "ok"\ncommand = "22F190"\nformula = "ascii"\n')
    errors: list[str] = []
    loaded = load_profiles(errors)
    assert {"ford", "gm", "hyundai", "good"} <= set(loaded)
    assert not {"broken", "badhex", "nocmd"} & set(loaded)
    assert len(errors) == 3


@pytest.mark.parametrize("value", ["nonsense:abc", ":35000", "host:0", "host:70000"])
def test_cli_rejects_bad_address(value):
    from elm327obd.cli import _addr

    with pytest.raises(SystemExit, match="Invalid adapter address"):
        _addr(value)


def test_cli_accepts_address():
    from elm327obd.cli import _addr

    assert _addr("192.168.0.10") == ("192.168.0.10", 35000)
    assert _addr("192.168.0.10:23") == ("192.168.0.10", 23)


# ---- second-pass review fixes ----------------------------------------------------

async def test_atfe_does_not_reset_cached_header(elm):
    await elm.raw("ATSH7E0")
    await elm.raw("ATFE")  # "forget events" – header is untouched
    assert elm._header == "7E0"


async def test_request_restores_before_sending(elm):
    await elm.raw("ATH0")  # console turned headers off; parser can't read replies
    # No explicit restore: the next app request must restore first.
    assert 0x0C in await elm.read_decoded([0x0C])
    assert not elm.settings_dirty


async def test_restore_without_atd_support():
    _, server, port = await start_emulator(Vehicle(no_atd=True))
    client = await connected_client(port)
    try:
        await client.raw("ATSH7E1")
        await client.restore_if_dirty()
        assert client._header == "7DF"  # explicitly reset, not assumed
        assert "7E8" in {d.ecu for d in await client.read_dtcs()}
    finally:
        await client.close()
        server.close()


async def test_did_scan_timeouts_must_be_consecutive(elm, monkeypatch):
    from elm327obd.protocol import NoData

    def fake_with(errors):
        seq = iter(errors)

        async def fake(cmd, **_kw):
            exc = next(seq, None)
            if exc:
                raise exc
            return []

        return fake

    t = TransportError("timed out")
    monkeypatch.setattr(elm, "request", fake_with([t, NoData(), t, NoData(), t, NoData(), t]))
    await elm.scan_dids("7E0", 0, 7)  # interleaved timeouts: keep scanning
    monkeypatch.setattr(elm, "request", fake_with([NoData(), t, t, t]))
    with pytest.raises(TransportError):
        await elm.scan_dids("7E0", 0, 7)


def test_nested_power_is_bounded():
    import time

    start = time.perf_counter()
    with pytest.raises(FormulaError):
        evaluate("((9 ** 64) ** 64) ** 64", b"")
    assert time.perf_counter() - start < 0.5
    assert evaluate("2 ** 16 + (A << 8)", b"\x01") == 65536 + 256


@pytest.mark.parametrize("header,rx", [
    ("7E0", "7E8"), ("7E2", "7EA"), ("726", "72E"), ("760", "768"), ("7DF", None), ("18DA10F1", None),
])
def test_scan_rx(header, rx):
    from elm327obd.tui.app import scan_rx

    assert scan_rx(header) == rx


@pytest.mark.parametrize("protocol,lines,codes", [
    ("6", ["7E8 06 43 02 03 01 04 20"], ["P0301", "P0420"]),
    ("3", ["48 6B 10 43 01 33 04 20 00 00 C4"], ["P0133", "P0420"]),
])
def test_console_dtc_annotation_by_protocol(protocol, lines, codes):
    from types import SimpleNamespace

    from elm327obd.tui.app import ObdApp

    app = ObdApp()
    app.elm = SimpleNamespace(format_dirty=False, protocol=protocol)
    notes = app.annotate("03", lines)
    assert [n.split()[1].rstrip(":") for n in notes] == codes


@pytest.mark.parametrize("output,gw", [
    ("   route to: default\\ndestination: default\\n    gateway: 192.168.0.10\\n  interface: en0\\n", "192.168.0.10"),
    ("default via 192.168.0.10 dev wlan0 proto dhcp src 192.168.0.11 metric 600\\n", "192.168.0.10"),
    ("", None),
])
def test_parse_gateway_macos_and_linux(output, gw):
    from elm327obd.scanner import parse_gateway

    assert parse_gateway(output) == gw
