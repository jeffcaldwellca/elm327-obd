import pytest

from elm327obd.protocol import (
    ElmError, NoData, VehicleNotResponding, check_errors, parse_frames,
)


def test_single_frame_11bit():
    msgs = parse_frames(["7E8 06 41 00 BE 3F A8 13 00"], "6")
    assert len(msgs) == 1
    assert msgs[0].ecu == "7E8"
    assert msgs[0].data == bytes.fromhex("4100BE3FA813")


def test_single_frame_no_spaces():
    msgs = parse_frames(["7E803410C1AF8"], "6")
    assert msgs[0].data == bytes.fromhex("410C1AF8")[:3]  # PCI says 3 bytes


def test_multi_frame_vin():
    lines = [
        "7E8 10 14 49 02 01 31 46 54",
        "7E8 21 45 57 31 45 50 35 4E",
        "7E8 22 46 41 31 32 33 34 35",
    ]
    (msg,) = parse_frames(lines, "6")
    assert msg.data[:3] == b"\x49\x02\x01"
    assert msg.data[3:].decode() == "1FTEW1EP5NFA12345"


def test_interleaved_ecus():
    lines = ["7E9 03 41 0D 20 00 00 00 00", "7E8 04 41 0C 1A F8 00 00 00"]
    msgs = {m.ecu: m.data for m in parse_frames(lines, "6")}
    assert msgs == {"7E9": bytes.fromhex("410D20"), "7E8": bytes.fromhex("410C1AF8")}


def test_29bit():
    (msg,) = parse_frames(["18 DA F1 10 04 41 0C 1A F8 00 00 00"], "7")
    assert msg.ecu == "18DAF110"
    assert msg.data == bytes.fromhex("410C1AF8")


def test_legacy_merges_dtc_lines():
    lines = ["48 6B 10 43 01 33 04 20 00 00 C4", "48 6B 10 43 01 71 00 00 00 00 AA"]
    (msg,) = parse_frames(lines, "3")
    assert msg.ecu == "10"
    assert msg.data == bytes.fromhex("43013304200000" + "017100000000")


@pytest.mark.parametrize("lines,exc", [
    (["NO DATA"], NoData),
    (["SEARCHING...", "UNABLE TO CONNECT"], VehicleNotResponding),
    (["CAN ERROR"], ElmError),
    (["?"], ElmError),
    (["BUS INIT: ...ERROR"], VehicleNotResponding),
])
def test_errors(lines, exc):
    with pytest.raises(exc):
        check_errors(lines)


def test_searching_stripped():
    assert check_errors(["SEARCHING...", "7E8 03 41 0D 00"]) == ["7E8 03 41 0D 00"]
