"""Diagnostic trouble code decoding and descriptions."""

from __future__ import annotations

import csv
import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

LETTERS = "PCBU"

KIND_LABEL = {0x03: "Stored", 0x07: "Pending", 0x0A: "Permanent"}


@dataclass(frozen=True)
class Dtc:
    code: str
    ecu: str = ""
    kind: str = "Stored"

    @property
    def description(self) -> str:
        return describe(self.code)

    @property
    def guide(self) -> Guide:
        return guide(self.code)


def decode_dtc(hi: int, lo: int) -> str:
    return f"{LETTERS[hi >> 6]}{(hi >> 4) & 0x3}{hi & 0xF:X}{lo:02X}"


def parse_dtc_payload(payload: bytes, *, has_count: bool) -> list[str]:
    """``payload`` excludes the response service byte (0x43/0x47/0x4A)."""
    if has_count and len(payload) % 2 == 1:
        payload = payload[1:]
    codes: list[str] = []
    for i in range(0, len(payload) - 1, 2):
        hi, lo = payload[i], payload[i + 1]
        if hi == 0 and lo == 0:
            continue
        code = decode_dtc(hi, lo)
        if code not in codes:
            codes.append(code)
    return codes


# Generic SAE J2012 descriptions for the most commonly seen codes. Anything
# not listed falls back to a category description; you can add your own in
# ~/.config/elm327obd/dtc.csv (two columns: code,description).
GENERIC: dict[str, str] = {
    "P0010": "Intake camshaft position actuator circuit (Bank 1)",
    "P0011": "Intake camshaft timing over-advanced / system performance (Bank 1)",
    "P0012": "Intake camshaft timing over-retarded (Bank 1)",
    "P0013": "Exhaust camshaft position actuator circuit (Bank 1)",
    "P0014": "Exhaust camshaft timing over-advanced / system performance (Bank 1)",
    "P0016": "Crankshaft/camshaft position correlation (Bank 1 Sensor A)",
    "P0017": "Crankshaft/camshaft position correlation (Bank 1 Sensor B)",
    "P0018": "Crankshaft/camshaft position correlation (Bank 2 Sensor A)",
    "P0020": "Intake camshaft position actuator circuit (Bank 2)",
    "P0021": "Intake camshaft timing over-advanced / system performance (Bank 2)",
    "P0030": "HO2S heater control circuit (Bank 1 Sensor 1)",
    "P0036": "HO2S heater control circuit (Bank 1 Sensor 2)",
    "P0068": "MAP/MAF – throttle position correlation",
    "P0087": "Fuel rail/system pressure too low",
    "P0088": "Fuel rail/system pressure too high",
    "P0100": "Mass air flow circuit malfunction",
    "P0101": "Mass air flow circuit range/performance",
    "P0102": "Mass air flow circuit low input",
    "P0103": "Mass air flow circuit high input",
    "P0106": "Manifold absolute pressure circuit range/performance",
    "P0107": "Manifold absolute pressure circuit low input",
    "P0108": "Manifold absolute pressure circuit high input",
    "P0110": "Intake air temperature circuit malfunction",
    "P0112": "Intake air temperature circuit low input",
    "P0113": "Intake air temperature circuit high input",
    "P0115": "Engine coolant temperature circuit malfunction",
    "P0116": "Engine coolant temperature circuit range/performance",
    "P0117": "Engine coolant temperature circuit low input",
    "P0118": "Engine coolant temperature circuit high input",
    "P0120": "Throttle position sensor A circuit malfunction",
    "P0121": "Throttle position sensor A circuit range/performance",
    "P0122": "Throttle position sensor A circuit low input",
    "P0123": "Throttle position sensor A circuit high input",
    "P0125": "Insufficient coolant temperature for closed loop fuel control",
    "P0128": "Coolant thermostat (coolant temp below regulating temperature)",
    "P0130": "O2 sensor circuit (Bank 1 Sensor 1)",
    "P0131": "O2 sensor circuit low voltage (Bank 1 Sensor 1)",
    "P0132": "O2 sensor circuit high voltage (Bank 1 Sensor 1)",
    "P0133": "O2 sensor circuit slow response (Bank 1 Sensor 1)",
    "P0134": "O2 sensor circuit no activity detected (Bank 1 Sensor 1)",
    "P0135": "O2 sensor heater circuit (Bank 1 Sensor 1)",
    "P0136": "O2 sensor circuit (Bank 1 Sensor 2)",
    "P0137": "O2 sensor circuit low voltage (Bank 1 Sensor 2)",
    "P0138": "O2 sensor circuit high voltage (Bank 1 Sensor 2)",
    "P0139": "O2 sensor circuit slow response (Bank 1 Sensor 2)",
    "P0140": "O2 sensor circuit no activity detected (Bank 1 Sensor 2)",
    "P0141": "O2 sensor heater circuit (Bank 1 Sensor 2)",
    "P0150": "O2 sensor circuit (Bank 2 Sensor 1)",
    "P0151": "O2 sensor circuit low voltage (Bank 2 Sensor 1)",
    "P0152": "O2 sensor circuit high voltage (Bank 2 Sensor 1)",
    "P0153": "O2 sensor circuit slow response (Bank 2 Sensor 1)",
    "P0154": "O2 sensor circuit no activity detected (Bank 2 Sensor 1)",
    "P0155": "O2 sensor heater circuit (Bank 2 Sensor 1)",
    "P0156": "O2 sensor circuit (Bank 2 Sensor 2)",
    "P0157": "O2 sensor circuit low voltage (Bank 2 Sensor 2)",
    "P0158": "O2 sensor circuit high voltage (Bank 2 Sensor 2)",
    "P0161": "O2 sensor heater circuit (Bank 2 Sensor 2)",
    "P0171": "System too lean (Bank 1)",
    "P0172": "System too rich (Bank 1)",
    "P0174": "System too lean (Bank 2)",
    "P0175": "System too rich (Bank 2)",
    "P0191": "Fuel rail pressure sensor circuit range/performance",
    "P0192": "Fuel rail pressure sensor circuit low input",
    "P0193": "Fuel rail pressure sensor circuit high input",
    **{f"P020{n}": f"Injector circuit malfunction – cylinder {n}" for n in range(1, 9)},
    "P0217": "Engine overtemperature condition",
    "P0219": "Engine overspeed condition",
    "P0220": "Throttle position sensor B circuit malfunction",
    "P0221": "Throttle position sensor B circuit range/performance",
    "P0222": "Throttle position sensor B circuit low input",
    "P0223": "Throttle position sensor B circuit high input",
    "P0230": "Fuel pump primary circuit malfunction",
    "P0234": "Turbo/supercharger overboost condition",
    "P0299": "Turbo/supercharger underboost condition",
    "P0300": "Random/multiple cylinder misfire detected",
    **{f"P030{n}": f"Cylinder {n} misfire detected" for n in range(1, 9)},
    "P0325": "Knock sensor 1 circuit (Bank 1 or single sensor)",
    "P0327": "Knock sensor 1 circuit low input (Bank 1 or single sensor)",
    "P0328": "Knock sensor 1 circuit high input (Bank 1 or single sensor)",
    "P0330": "Knock sensor 2 circuit (Bank 2)",
    "P0335": "Crankshaft position sensor A circuit",
    "P0336": "Crankshaft position sensor A circuit range/performance",
    "P0340": "Camshaft position sensor A circuit (Bank 1 or single sensor)",
    "P0341": "Camshaft position sensor A circuit range/performance (Bank 1)",
    "P0345": "Camshaft position sensor A circuit (Bank 2)",
    **{f"P035{n}": f"Ignition coil {chr(64 + n)} primary/secondary circuit" for n in range(1, 9)},
    "P0400": "Exhaust gas recirculation flow malfunction",
    "P0401": "Exhaust gas recirculation flow insufficient",
    "P0402": "Exhaust gas recirculation flow excessive",
    "P0403": "Exhaust gas recirculation control circuit",
    "P0404": "Exhaust gas recirculation circuit range/performance",
    "P0410": "Secondary air injection system",
    "P0411": "Secondary air injection system incorrect flow",
    "P0420": "Catalyst system efficiency below threshold (Bank 1)",
    "P0430": "Catalyst system efficiency below threshold (Bank 2)",
    "P0440": "Evaporative emission system malfunction",
    "P0441": "Evaporative emission system incorrect purge flow",
    "P0442": "Evaporative emission system leak detected (small leak)",
    "P0443": "Evaporative emission purge control valve circuit",
    "P0446": "Evaporative emission vent control circuit",
    "P0449": "Evaporative emission vent valve/solenoid circuit",
    "P0451": "Evaporative emission pressure sensor range/performance",
    "P0452": "Evaporative emission pressure sensor low input",
    "P0453": "Evaporative emission pressure sensor high input",
    "P0455": "Evaporative emission system leak detected (large leak)",
    "P0456": "Evaporative emission system leak detected (very small leak)",
    "P0457": "Evaporative emission system leak detected (fuel cap loose/off)",
    "P0461": "Fuel level sensor circuit range/performance",
    "P0462": "Fuel level sensor circuit low input",
    "P0463": "Fuel level sensor circuit high input",
    "P0480": "Cooling fan 1 control circuit",
    "P0500": "Vehicle speed sensor A malfunction",
    "P0501": "Vehicle speed sensor A range/performance",
    "P0505": "Idle air control system malfunction",
    "P0506": "Idle control system RPM lower than expected",
    "P0507": "Idle control system RPM higher than expected",
    "P0520": "Engine oil pressure sensor/switch circuit",
    "P0521": "Engine oil pressure sensor/switch range/performance",
    "P0524": "Engine oil pressure too low",
    "P0530": "A/C refrigerant pressure sensor A circuit",
    "P0562": "System voltage low",
    "P0563": "System voltage high",
    "P0571": "Brake switch A circuit",
    "P0600": "Serial communication link malfunction",
    "P0601": "Internal control module memory checksum error",
    "P0602": "Control module programming error",
    "P0603": "Internal control module keep-alive memory (KAM) error",
    "P0604": "Internal control module RAM error",
    "P0605": "Internal control module ROM error",
    "P0606": "Control module processor fault",
    "P0620": "Generator control circuit",
    "P0622": "Generator field terminal circuit",
    "P0641": "Sensor reference voltage A circuit open",
    "P0650": "Malfunction indicator lamp (MIL) control circuit",
    "P0700": "Transmission control system (MIL request) – check TCM codes",
    "P0705": "Transmission range sensor circuit (PRNDL input)",
    "P0706": "Transmission range sensor circuit range/performance",
    "P0710": "Transmission fluid temperature sensor A circuit",
    "P0711": "Transmission fluid temperature sensor A range/performance",
    "P0712": "Transmission fluid temperature sensor A circuit low",
    "P0713": "Transmission fluid temperature sensor A circuit high",
    "P0715": "Input/turbine speed sensor A circuit",
    "P0717": "Input/turbine speed sensor A circuit no signal",
    "P0720": "Output speed sensor circuit",
    "P0730": "Incorrect gear ratio",
    **{f"P073{n}": f"Gear {n} incorrect ratio" for n in range(1, 7)},
    "P0740": "Torque converter clutch solenoid circuit",
    "P0741": "Torque converter clutch performance or stuck off",
    "P0750": "Shift solenoid A",
    "P0755": "Shift solenoid B",
    "P0760": "Shift solenoid C",
    "P0765": "Shift solenoid D",
    "P0868": "Transmission fluid pressure low",
    "P2096": "Post-catalyst fuel trim system too lean (Bank 1)",
    "P2097": "Post-catalyst fuel trim system too rich (Bank 1)",
    "P2135": "Throttle/pedal position sensor A/B voltage correlation",
    "P2138": "Throttle/pedal position sensor D/E voltage correlation",
    "P2187": "System too lean at idle (Bank 1)",
    "P2188": "System too rich at idle (Bank 1)",
    "P2195": "O2 sensor signal biased/stuck lean (Bank 1 Sensor 1)",
    "P2196": "O2 sensor signal biased/stuck rich (Bank 1 Sensor 1)",
    "P2270": "O2 sensor signal biased/stuck lean (Bank 1 Sensor 2)",
    "P2271": "O2 sensor signal biased/stuck rich (Bank 1 Sensor 2)",
    "U0001": "High speed CAN communication bus",
    "U0100": "Lost communication with ECM/PCM A",
    "U0101": "Lost communication with TCM",
    "U0121": "Lost communication with ABS control module",
    "U0140": "Lost communication with body control module",
    "U0151": "Lost communication with restraints control module",
    "U0155": "Lost communication with instrument panel cluster",
}

SYSTEM = {"P": "Powertrain", "C": "Chassis", "B": "Body", "U": "Network"}

P0_SUBSYSTEM = {
    "0": "Fuel/air metering & auxiliary emission controls",
    "1": "Fuel and air metering",
    "2": "Fuel and air metering (injector circuit)",
    "3": "Ignition system or misfire",
    "4": "Auxiliary emission controls",
    "5": "Vehicle speed, idle control and auxiliary inputs",
    "6": "Computer and output circuits",
    "7": "Transmission",
    "8": "Transmission",
    "9": "Transmission",
    "A": "Hybrid propulsion",
    "B": "Hybrid propulsion",
    "C": "Hybrid propulsion",
}

USER_DTC_FILE = Path.home() / ".config" / "elm327obd" / "dtc.csv"
USER_GUIDE_FILE = Path.home() / ".config" / "elm327obd" / "dtc_guide.toml"

# ---- names -------------------------------------------------------------------
# Lookup order: your dtc.csv → built-in GENERIC (curated) → python-OBD's table
# (2,000+ generic P0/P2/P3/U0 names, GPL-2.0, data/python_obd_dtc.tsv) → a
# description derived from the code's structure.

_user_codes: dict[str, str] | None = None
_pyobd_codes: dict[str, str] | None = None


def _load_user_codes() -> dict[str, str]:
    global _user_codes
    if _user_codes is None:
        _user_codes = {}
        if USER_DTC_FILE.exists():
            with USER_DTC_FILE.open(newline="", encoding="utf-8") as fh:
                for row in csv.reader(fh):
                    if len(row) >= 2 and row[0].strip():
                        _user_codes[row[0].strip().upper()] = row[1].strip()
    return _user_codes


def _load_pyobd_codes() -> dict[str, str]:
    global _pyobd_codes
    if _pyobd_codes is None:
        _pyobd_codes = {}
        path = resources.files("elm327obd") / "data" / "python_obd_dtc.tsv"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _pyobd_codes  # optional GPL data removed: fall back to built-in names
        for line in text.splitlines():
            if line and not line.startswith("#"):
                code, _, text = line.partition("\t")
                _pyobd_codes[code] = text
    return _pyobd_codes


def is_manufacturer_specific(code: str) -> bool:
    """SAE J2012: P1xxx, P30xx–P33xx, and B/C/U with 1/2 (3 = reserved) are OEM-defined."""
    code = code.upper()
    if code[0] == "P":
        return code[1] == "1" or (code[1] == "3" and code[2] in "0123")
    return code[1] in "123"


def name_source(code: str) -> str:
    code = code.upper()
    if code in _load_user_codes():
        return "custom"
    if code in GENERIC:
        return "built-in"
    if code in _load_pyobd_codes():
        return "python-OBD"
    return "category"


def describe(code: str) -> str:
    code = code.upper()
    for table in (_load_user_codes(), GENERIC, _load_pyobd_codes()):
        if code in table:
            return table[code]
    system = SYSTEM.get(code[0], "Unknown")
    if is_manufacturer_specific(code):
        return f"Manufacturer-specific {system.lower()} code – look up for your make"
    if code[0] == "P" and code[1] == "3":
        return "Generic powertrain: cylinder deactivation"
    if code[0] == "P":
        return f"Generic powertrain: {P0_SUBSYSTEM.get(code[2], 'unspecified subsystem')}"
    return f"Generic {system.lower()} code"


# ---- guidance ----------------------------------------------------------------

SEVERITIES = ("stop", "soon", "monitor", "low")
SEVERITY_TEXT = {
    "stop": "Stop driving as soon as it's safe – risk of engine damage or unsafe operation.",
    "soon": "Get it fixed soon – affects how the car runs, or can cause damage if ignored.",
    "monitor": "Fix when convenient – keep an eye on it; usually not urgent.",
    "low": "Low urgency – mainly emissions/inspection; the car normally drives fine.",
}


@dataclass(frozen=True)
class Guide:
    code: str
    name: str
    severity: str
    summary: str
    causes: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    watch: tuple[int, ...] = ()  # Mode 01 PIDs worth reading live
    source: str = "guide"  # "guide", "custom" or "estimated"

    @property
    def rank(self) -> int:
        return SEVERITIES.index(self.severity)


_guide: dict[str, dict] | None = None
GUIDE_ERRORS: list[str] = []


def expand_codes(spec: str) -> list[str]:
    """"P0301-P0308" → P0301…P0308 (last three characters counted in hex)."""
    spec = spec.strip().upper()
    if "-" not in spec:
        return [spec]
    first, last = (part.strip() for part in spec.split("-", 1))
    if first[:2] != last[:2]:
        raise ValueError(f"range {spec!r} must keep the same first two characters")
    lo, hi = int(first[2:], 16), int(last[2:], 16)
    if hi < lo or hi - lo > 0x100:
        raise ValueError(f"bad range {spec!r}")
    return [f"{first[:2]}{n:03X}" for n in range(lo, hi + 1)]


def _parse_guide(text: str, source: str, into: dict[str, dict]) -> None:
    from elm327obd.pids import PIDS  # local import: pids doesn't depend on dtc

    for i, entry in enumerate(tomllib.loads(text).get("entry", [])):
        where = f"{source} entry {i + 1}"
        severity = entry.get("severity")
        if severity not in SEVERITIES:
            raise ValueError(f"{where}: severity must be one of {', '.join(SEVERITIES)}")
        watch = []
        for pid in entry.get("watch", []):
            num = int(str(pid)[-2:], 16)
            if num not in PIDS:
                raise ValueError(f"{where}: unknown watch PID {pid!r}")
            watch.append(num)
        data = {
            "severity": severity,
            "summary": str(entry.get("summary", "")).strip(),
            "causes": tuple(entry.get("causes", [])),
            "checks": tuple(entry.get("checks", [])),
            "watch": tuple(watch),
            "source": "custom" if source != "built-in" else "guide",
        }
        for spec in entry.get("codes", []):
            for code in expand_codes(spec):
                into[code] = data


def _load_guide() -> dict[str, dict]:
    global _guide
    if _guide is None:
        table: dict[str, dict] = {}
        builtin = resources.files("elm327obd") / "dtc_guide.toml"
        _parse_guide(builtin.read_text(encoding="utf-8"), "built-in", table)
        if USER_GUIDE_FILE.exists():
            try:
                _parse_guide(USER_GUIDE_FILE.read_text(encoding="utf-8"), USER_GUIDE_FILE.name, table)
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError, TypeError) as exc:
                GUIDE_ERRORS.append(f"{USER_GUIDE_FILE}: {exc}")
        _guide = table
    return _guide


# Keyword rules for codes without a hand-written entry, checked in order
# against the code's name. They give a sensible first estimate, not a diagnosis.
_RULES: list[tuple[str, str, str]] = [
    (r"over ?temperature|overheat", "stop",
     "Something (coolant, oil or transmission fluid) is too hot – pull over and let it cool."),
    (r"oil pressure too low|low oil pressure", "stop", "Engine oil pressure may be too low."),
    (r"oil pressure", "soon", "Oil pressure sensing fault – check the oil level now and watch the oil light."),
    (r"misfire", "soon", "A misfire was detected. If the check-engine light is flashing, stop driving."),
    # Network codes name the module they lost; check these before the per-system rules below.
    (r"(lost communication|invalid data).*(abs|anti-?lock|brake|restraint|airbag|occupant|steering)", "soon",
     "Communication problem with a safety-related module – ABS, stability, airbag or steering functions "
     "may be disabled."),
    (r"lost communication|invalid data|communication bus|\bcan\b", "monitor",
     "Module communication – often low battery voltage, connectors or a module offline."),
    (r"(ho2s|o2 sensor|oxygen sensor|a/f sensor).*heater|heater.*(ho2s|o2 sensor)", "low",
     "An oxygen sensor heater circuit fault – mainly affects emissions after cold starts."),
    (r"(ho2s|o2 sensor|oxygen sensor|a/f sensor).*sensor [23]", "low",
     "A downstream oxygen sensor issue – mainly emissions monitoring."),
    (r"ho2s|o2 sensor|oxygen sensor|a/f sensor|air.?fuel ratio", "monitor",
     "An oxygen/air-fuel sensor issue – can affect fuel control and economy."),
    (r"catalyst", "low", "Catalytic converter system – mainly emissions."),
    (r"evap|evaporative|purge|fuel tank pressure|fuel cap|canister", "low",
     "Evaporative emissions (fuel vapour) system – no effect on driving."),
    (r"\begr\b|exhaust gas recirculation", "low", "Exhaust gas recirculation system – mainly emissions."),
    (r"secondary air", "low", "Secondary air injection – mainly emissions on cold starts."),
    (r"particulate|\bdpf\b|\bscr\b|\bdef\b|reductant|nox", "monitor",
     "Diesel/advanced emissions after-treatment – may lead to reduced power if ignored."),
    (r"knock", "monitor", "Knock sensing – the engine may reduce timing, lowering power."),
    (r"injector|fuel pump|fuel rail|fuel pressure|fuel volume|fuel shutoff", "soon",
     "Fuel delivery – can cause hesitation, stalling or no-start."),
    (r"ignition coil|spark", "soon", "Ignition – likely to cause misfires."),
    (r"crankshaft|camshaft|timing", "soon", "Engine timing/position sensing – can cause stalling or no-start."),
    (r"throttle|pedal", "soon", "Throttle/pedal control – may cause reduced-power (limp) mode."),
    (r"turbo|supercharger|boost|wastegate", "soon", "Boost control – expect reduced power."),
    (r"transmission|gear|shift|torque converter|clutch|range sensor", "soon",
     "Transmission – may cause harsh shifts, slipping or limp mode."),
    (r"brake", "soon", "Brake-related – brake lights, ABS or cruise functions may be affected."),
    (r"airbag|restraint|occupant|seat ?belt|pretension", "soon", "Safety restraint system – airbags may not deploy."),
    (r"\babs\b|anti-?lock|stability|traction|yaw|wheel speed", "soon",
     "Braking/stability systems – ABS or traction control may be disabled."),
    (r"steering", "soon", "Steering system – assist or related features may be affected."),
    (r"coolant|cooling fan|thermostat|radiator", "soon", "Cooling system – watch engine temperature."),
    (r"battery|charging|generator|alternator|system voltage", "soon",
     "Charging/electrical supply – can leave you stranded."),
    (r"hybrid|high voltage|drive motor|inverter", "soon", "Hybrid/electric drive system – have it checked promptly."),
    (r"a/c|air condition|hvac|climate|blower|refrigerant", "low", "Climate control – comfort only."),
    (r"lamp|light|indicator|mirror|radio|window|door|wiper|horn|seat", "low", "Body/convenience feature."),
]

_P_SUBSYSTEM_SEVERITY = {"0": "monitor", "1": "monitor", "2": "monitor", "3": "soon", "4": "low",
                         "5": "monitor", "6": "monitor", "7": "soon", "8": "soon", "9": "soon",
                         "A": "soon", "B": "soon", "C": "soon"}


def _estimate(code: str, name: str) -> tuple[str, str]:
    lower = name.lower()
    for pattern, severity, summary in _RULES:
        if re.search(pattern, lower):
            return severity, summary
    if is_manufacturer_specific(code):
        return "monitor", "Manufacturer-specific code: look up its exact meaning for your make before deciding."
    if code[0] == "P" and code[1] == "3":
        return "monitor", "Cylinder deactivation system – may cause rough running or reduced economy."
    if code[0] == "P":
        return _P_SUBSYSTEM_SEVERITY.get(code[2], "monitor"), (
            f"Generic powertrain code ({P0_SUBSYSTEM.get(code[2], 'unspecified subsystem').lower()}).")
    if code[0] == "C":
        return "soon", "Chassis code – brakes, steering or suspension may be affected."
    if code[0] == "U":
        return "monitor", "Network code – often low battery voltage, connectors or a module offline."
    return "monitor", "Body code – usually a convenience or comfort feature."


def guide(code: str) -> Guide:
    code = code.upper()
    name = describe(code)
    entry = _load_guide().get(code)
    if entry:
        return Guide(code, name, entry["severity"], entry["summary"], entry["causes"], entry["checks"],
                     entry["watch"], entry["source"])
    severity, summary = _estimate(code, name)
    return Guide(code, name, severity, summary, source="estimated")
