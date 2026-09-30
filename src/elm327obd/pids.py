"""SAE J1979 Mode 01 PID definitions and decoders."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

Value = float | str


@dataclass(frozen=True)
class Pid:
    pid: int
    name: str
    size: int  # data bytes returned
    unit: str
    decode: Callable[[bytes], Value]
    lo: float = 0.0  # gauge range (metric)
    hi: float = 100.0
    category: str = "Engine"

    @property
    def code(self) -> str:
        return f"01{self.pid:02X}"


def _u16(d: bytes) -> int:
    return (d[0] << 8) | d[1]


def _pct(d: bytes) -> float:
    return d[0] * 100 / 255


def _temp(d: bytes) -> float:
    return d[0] - 40


def _trim(d: bytes) -> float:
    return (d[0] - 128) * 100 / 128


def _o2_volts(d: bytes) -> float:
    return d[0] / 200


def _lambda(d: bytes) -> float:
    return _u16(d) * 2 / 65536


FUEL_SYSTEM = {
    0: "Off",
    1: "Open loop (cold)",
    2: "Closed loop",
    4: "Open loop (load/decel)",
    8: "Open loop (fault)",
    16: "Closed loop (fault)",
}

FUEL_TYPE = {
    1: "Gasoline", 2: "Methanol", 3: "Ethanol", 4: "Diesel", 5: "LPG", 6: "CNG",
    7: "Propane", 8: "Electric", 9: "Bifuel gasoline", 10: "Bifuel methanol",
    11: "Bifuel ethanol", 12: "Bifuel LPG", 13: "Bifuel CNG", 14: "Bifuel propane",
    15: "Bifuel electric", 16: "Bifuel electric/combustion", 17: "Hybrid gasoline",
    18: "Hybrid ethanol", 19: "Hybrid diesel", 20: "Hybrid electric",
    21: "Hybrid electric/combustion", 22: "Hybrid regenerative", 23: "Bifuel diesel",
}

OBD_STANDARD = {
    1: "OBD-II (CARB)", 2: "OBD (EPA)", 3: "OBD and OBD-II", 4: "OBD-I", 5: "Not OBD compliant",
    6: "EOBD (Europe)", 7: "EOBD and OBD-II", 8: "EOBD and OBD", 9: "EOBD, OBD and OBD-II",
    10: "JOBD (Japan)", 11: "JOBD and OBD-II", 12: "JOBD and EOBD", 13: "JOBD, EOBD and OBD-II",
    17: "EMD", 18: "EMD+", 19: "HD OBD-C", 20: "HD OBD", 21: "WWH OBD", 23: "HD EOBD-I",
    24: "HD EOBD-I N", 25: "HD EOBD-II", 26: "HD EOBD-II N", 28: "OBDBr-1", 29: "OBDBr-2",
    30: "KOBD (Korea)", 31: "IOBD I", 32: "IOBD II", 33: "HD EOBD-IV",
}


def _fuel_system(d: bytes) -> str:
    return FUEL_SYSTEM.get(d[0], f"0x{d[0]:02X}")


def _enum(table: dict[int, str]) -> Callable[[bytes], str]:
    return lambda d: table.get(d[0], f"Unknown ({d[0]})")


_DEFS: list[Pid] = [
    Pid(0x03, "Fuel system status", 2, "", _fuel_system, category="Fuel"),
    Pid(0x04, "Calculated engine load", 1, "%", _pct),
    Pid(0x05, "Coolant temperature", 1, "°C", _temp, -40, 130),
    Pid(0x06, "Short term fuel trim B1", 1, "%", _trim, -25, 25, "Fuel"),
    Pid(0x07, "Long term fuel trim B1", 1, "%", _trim, -25, 25, "Fuel"),
    Pid(0x08, "Short term fuel trim B2", 1, "%", _trim, -25, 25, "Fuel"),
    Pid(0x09, "Long term fuel trim B2", 1, "%", _trim, -25, 25, "Fuel"),
    Pid(0x0A, "Fuel pressure", 1, "kPa", lambda d: d[0] * 3, 0, 765, "Fuel"),
    Pid(0x0B, "Intake manifold pressure", 1, "kPa", lambda d: d[0], 0, 255, "Air"),
    Pid(0x0C, "Engine RPM", 2, "rpm", lambda d: _u16(d) / 4, 0, 7000),
    Pid(0x0D, "Vehicle speed", 1, "km/h", lambda d: d[0], 0, 200, "Vehicle"),
    Pid(0x0E, "Timing advance", 1, "°", lambda d: d[0] / 2 - 64, -20, 50, "Ignition"),
    Pid(0x0F, "Intake air temperature", 1, "°C", _temp, -40, 80, "Air"),
    Pid(0x10, "MAF air flow rate", 2, "g/s", lambda d: _u16(d) / 100, 0, 250, "Air"),
    Pid(0x11, "Throttle position", 1, "%", _pct, category="Air"),
    Pid(0x13, "O2 sensors present", 1, "", lambda d: f"{d[0]:08b}", category="O2"),
    *[
        Pid(0x14 + i, f"O2 B{1 + i // 4}S{1 + i % 4} voltage", 2, "V", _o2_volts, 0, 1.275, "O2")
        for i in range(8)
    ],
    Pid(0x1C, "OBD standard", 1, "", _enum(OBD_STANDARD), category="Vehicle"),
    Pid(0x1F, "Run time since start", 2, "s", lambda d: _u16(d), 0, 3600, "Vehicle"),
    Pid(0x21, "Distance with MIL on", 2, "km", lambda d: _u16(d), 0, 1000, "Vehicle"),
    Pid(0x22, "Fuel rail pressure (rel.)", 2, "kPa", lambda d: _u16(d) * 0.079, 0, 5000, "Fuel"),
    Pid(0x23, "Fuel rail pressure", 2, "kPa", lambda d: _u16(d) * 10, 0, 30000, "Fuel"),
    *[
        Pid(0x24 + i, f"O2 S{1 + i} wide-band λ", 4, "λ", _lambda, 0.5, 1.5, "O2")
        for i in range(8)
    ],
    Pid(0x2C, "Commanded EGR", 1, "%", _pct, category="Emissions"),
    Pid(0x2D, "EGR error", 1, "%", _trim, -100, 100, "Emissions"),
    Pid(0x2E, "Commanded evap purge", 1, "%", _pct, category="Emissions"),
    Pid(0x2F, "Fuel tank level", 1, "%", _pct, category="Fuel"),
    Pid(0x30, "Warm-ups since codes cleared", 1, "", lambda d: d[0], 0, 255, "Vehicle"),
    Pid(0x31, "Distance since codes cleared", 2, "km", lambda d: _u16(d), 0, 5000, "Vehicle"),
    Pid(0x33, "Barometric pressure", 1, "kPa", lambda d: d[0], 60, 110, "Air"),
    *[
        Pid(0x34 + i, f"O2 S{1 + i} wide-band λ (current)", 4, "λ", _lambda, 0.5, 1.5, "O2")
        for i in range(8)
    ],
    Pid(0x3C, "Catalyst temp B1S1", 2, "°C", lambda d: _u16(d) / 10 - 40, 0, 1000, "Emissions"),
    Pid(0x3D, "Catalyst temp B2S1", 2, "°C", lambda d: _u16(d) / 10 - 40, 0, 1000, "Emissions"),
    Pid(0x3E, "Catalyst temp B1S2", 2, "°C", lambda d: _u16(d) / 10 - 40, 0, 1000, "Emissions"),
    Pid(0x3F, "Catalyst temp B2S2", 2, "°C", lambda d: _u16(d) / 10 - 40, 0, 1000, "Emissions"),
    Pid(0x42, "Control module voltage", 2, "V", lambda d: _u16(d) / 1000, 10, 16, "Electrical"),
    Pid(0x43, "Absolute load", 2, "%", lambda d: _u16(d) * 100 / 255, 0, 200),
    Pid(0x44, "Commanded equivalence ratio", 2, "λ", _lambda, 0.5, 1.5, "Fuel"),
    Pid(0x45, "Relative throttle position", 1, "%", _pct, category="Air"),
    Pid(0x46, "Ambient air temperature", 1, "°C", _temp, -40, 60, "Air"),
    Pid(0x47, "Absolute throttle B", 1, "%", _pct, category="Air"),
    Pid(0x49, "Accelerator pedal D", 1, "%", _pct, category="Air"),
    Pid(0x4A, "Accelerator pedal E", 1, "%", _pct, category="Air"),
    Pid(0x4C, "Commanded throttle actuator", 1, "%", _pct, category="Air"),
    Pid(0x4D, "Time run with MIL on", 2, "min", lambda d: _u16(d), 0, 600, "Vehicle"),
    Pid(0x4E, "Time since codes cleared", 2, "min", lambda d: _u16(d), 0, 6000, "Vehicle"),
    Pid(0x51, "Fuel type", 1, "", _enum(FUEL_TYPE), category="Fuel"),
    Pid(0x52, "Ethanol fuel", 1, "%", _pct, category="Fuel"),
    Pid(0x5A, "Relative accelerator pedal", 1, "%", _pct, category="Air"),
    Pid(0x5B, "Hybrid battery remaining", 1, "%", _pct, category="Electrical"),
    Pid(0x5C, "Engine oil temperature", 1, "°C", _temp, -40, 150),
    Pid(0x5D, "Fuel injection timing", 2, "°", lambda d: _u16(d) / 128 - 210, -30, 30, "Fuel"),
    Pid(0x5E, "Engine fuel rate", 2, "L/h", lambda d: _u16(d) / 20, 0, 40, "Fuel"),
    Pid(0x61, "Driver demand torque", 1, "%", lambda d: d[0] - 125, -125, 130),
    Pid(0x62, "Actual engine torque", 1, "%", lambda d: d[0] - 125, -125, 130),
    Pid(0x63, "Reference engine torque", 2, "Nm", lambda d: _u16(d), 0, 1000),
    Pid(
        0xA6, "Odometer", 4, "km",
        lambda d: ((d[0] << 24) | (d[1] << 16) | (d[2] << 8) | d[3]) / 10, 0, 500000, "Vehicle",
    ),
]

PIDS: dict[int, Pid] = {p.pid: p for p in _DEFS}

# Response sizes for PIDs we don't decode but must skip over in batched replies.
PID_SIZES: dict[int, int] = {p.pid: p.size for p in _DEFS}
PID_SIZES.update({0x00: 4, 0x20: 4, 0x40: 4, 0x60: 4, 0x80: 4, 0xA0: 4, 0xC0: 4, 0x01: 4, 0x02: 2,
                  0x12: 1, 0x1D: 1, 0x1E: 1, 0x41: 4, 0x4F: 4, 0x50: 4})

SUPPORT_PIDS = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0)

DEFAULT_DASHBOARD = [0x0C, 0x0D, 0x05, 0x04, 0x11, 0x0B, 0x0F, 0x06, 0x07, 0x42]


def decode(pid: int, data: bytes) -> Value | None:
    spec = PIDS.get(pid)
    if spec is None or len(data) < spec.size:
        return None
    try:
        return spec.decode(data)
    except (IndexError, ValueError, ZeroDivisionError):
        return None


def decode_support_bitmap(base: int, data: bytes) -> set[int]:
    bits = int.from_bytes(data[:4], "big")
    return {base + i + 1 for i in range(32) if bits & (1 << (31 - i))}


# --- Monitor status (PID 01) --------------------------------------------------

SPARK_MONITORS = [
    "Catalyst", "Heated catalyst", "Evaporative system", "Secondary air system",
    "A/C refrigerant", "Oxygen sensor", "Oxygen sensor heater", "EGR/VVT system",
]
DIESEL_MONITORS = [
    "NMHC catalyst", "NOx/SCR monitor", "Reserved", "Boost pressure",
    "Reserved", "Exhaust gas sensor", "PM filter", "EGR/VVT system",
]


@dataclass
class MonitorStatus:
    mil_on: bool
    dtc_count: int
    diesel: bool
    monitors: list[tuple[str, bool, bool]]  # (name, available, complete)


def decode_monitor_status(d: bytes) -> MonitorStatus:
    a, b, c, dd = d[0], d[1], d[2], d[3]
    diesel = bool(b & 0x08)
    monitors = [
        ("Misfire", bool(b & 0x01), not b & 0x10),
        ("Fuel system", bool(b & 0x02), not b & 0x20),
        ("Components", bool(b & 0x04), not b & 0x40),
    ]
    names = DIESEL_MONITORS if diesel else SPARK_MONITORS
    for bit, name in enumerate(names):
        if name == "Reserved":
            continue
        monitors.append((name, bool(c & (1 << bit)), not dd & (1 << bit)))
    return MonitorStatus(bool(a & 0x80), a & 0x7F, diesel, monitors)


# --- Units ---------------------------------------------------------------------


def convert(value: Value | None, unit: str, imperial: bool) -> tuple[Value | None, str]:
    if not imperial or not isinstance(value, (int, float)):
        return value, unit
    match unit:
        case "°C":
            return value * 9 / 5 + 32, "°F"
        case "km/h":
            return value * 0.621371, "mph"
        case "km":
            return value * 0.621371, "mi"
        case "kPa":
            return value * 0.145038, "psi"
        case "L/h":
            return value * 0.264172, "gal/h"
        case "Nm":
            return value * 0.737562, "lb·ft"
    return value, unit


def convert_range(lo: float, hi: float, unit: str, imperial: bool) -> tuple[float, float]:
    clo, _ = convert(lo, unit, imperial)
    chi, _ = convert(hi, unit, imperial)
    return float(clo), float(chi)  # type: ignore[arg-type]


def format_value(value: Value | None, unit: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    if unit in ("rpm", "s", "min", "") or abs(value) >= 1000:
        text = f"{value:,.0f}"
    elif unit in ("V", "λ"):
        text = f"{value:.3f}" if unit == "λ" else f"{value:.2f}"
    else:
        text = f"{value:.1f}"
    return text
