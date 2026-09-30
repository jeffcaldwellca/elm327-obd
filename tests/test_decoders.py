import pytest

from elm327obd import pids as P
from elm327obd.dtc import decode_dtc, describe, parse_dtc_payload
from elm327obd.profiles import FormulaError, evaluate, load_profiles


def test_rpm_speed_temp():
    assert P.decode(0x0C, bytes.fromhex("1AF8")) == 1726.0
    assert P.decode(0x0D, b"\x3c") == 60
    assert P.decode(0x05, b"\x7b") == 83
    assert P.decode(0x06, b"\x80") == 0
    assert P.decode(0x0C, b"\x1a") is None  # short data


def test_support_bitmap():
    assert P.decode_support_bitmap(0, bytes.fromhex("BE1FA813")) >= {0x01, 0x03, 0x04, 0x05, 0x0C, 0x0D, 0x20}


def test_monitor_status():
    st = P.decode_monitor_status(bytes.fromhex("82076504"))
    assert st.mil_on and st.dtc_count == 2 and not st.diesel
    evap = next(m for m in st.monitors if m[0] == "Evaporative system")
    assert evap == ("Evaporative system", True, False)


def test_units():
    assert P.convert(100.0, "°C", True) == (212.0, "°F")
    assert P.convert(100.0, "km/h", True)[1] == "mph"
    assert P.convert("Gasoline", "", True) == ("Gasoline", "")


def test_dtc_decode():
    assert decode_dtc(0x03, 0x01) == "P0301"
    assert decode_dtc(0xC1, 0x00) == "U0100"
    assert decode_dtc(0x41, 0x23) == "C0123"
    assert parse_dtc_payload(bytes.fromhex("02030104 20"), has_count=True) == ["P0301", "P0420"]
    assert parse_dtc_payload(bytes.fromhex("030104200000"), has_count=False) == ["P0301", "P0420"]


def test_describe_fallbacks():
    assert "misfire" in describe("P0301").lower()
    assert "Manufacturer-specific" in describe("P1234")
    assert "Transmission" in describe("P070A")  # not in any name list → category fallback


@pytest.mark.parametrize("formula,payload,expected", [
    ("A - 40", b"\x6e", 70),
    ("s16(A, B) / 16", bytes.fromhex("FFF0"), -1.0),
    ("u16(A,B) * 0.1", bytes.fromhex("0100"), 25.6),
    ("bit(A, 7)", b"\x80", 1),
    ("b[2] + 1", b"\x00\x00\x05", 6),
    ("ascii", b"ABC\x00", "ABC"),
    ("hex", b"\x01\xab", "01 AB"),
])
def test_formulas(formula, payload, expected):
    assert evaluate(formula, payload) == pytest.approx(expected) if not isinstance(expected, str) \
        else evaluate(formula, payload) == expected


@pytest.mark.parametrize("formula", ["__import__('os')", "A.__class__", "open('x')", "C + 1"])
def test_formula_rejects_unsafe_or_missing(formula):
    with pytest.raises(FormulaError):
        evaluate(formula, b"\x01\x02")


def test_builtin_profiles_load():
    profiles = load_profiles()
    assert {"ford", "gm", "hyundai"} <= set(profiles)
    assert all(p.pids for p in profiles.values())
