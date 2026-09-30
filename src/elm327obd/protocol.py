"""OBD-II / ISO 15765 frame parsing independent of the transport."""

from __future__ import annotations

import string
from dataclasses import dataclass

PROTOCOLS = {
    "0": "Automatic",
    "1": "SAE J1850 PWM (41.6 kbaud)",
    "2": "SAE J1850 VPW (10.4 kbaud)",
    "3": "ISO 9141-2 (5 baud init)",
    "4": "ISO 14230-4 KWP (5 baud init)",
    "5": "ISO 14230-4 KWP (fast init)",
    "6": "ISO 15765-4 CAN (11 bit, 500 kbaud)",
    "7": "ISO 15765-4 CAN (29 bit, 500 kbaud)",
    "8": "ISO 15765-4 CAN (11 bit, 250 kbaud)",
    "9": "ISO 15765-4 CAN (29 bit, 250 kbaud)",
    "A": "SAE J1939 CAN (29 bit, 250 kbaud)",
    "B": "User1 CAN",
    "C": "User2 CAN",
}

CAN_11BIT = {"6", "8", "B"}
CAN_29BIT = {"7", "9", "A", "C"}

# Default functional (broadcast) request headers per protocol family.
FUNCTIONAL_HEADER_11 = "7DF"
FUNCTIONAL_HEADER_29 = "18DB33F1"
FUNCTIONAL_HEADER_PWM = "616AF1"
FUNCTIONAL_HEADER_LEGACY = "686AF1"  # SAE J1850 VPW and ISO 9141-2
FUNCTIONAL_HEADER_KWP = "C133F1"  # ISO 14230-4 (KWP2000)

ECU_NAMES = {
    "7E8": "Engine (ECM/PCM)",
    "7E9": "Transmission (TCM)",
    "7EA": "ECU #3",
    "7EB": "ECU #4",
    "7EC": "ECU #5 (often hybrid/BMS)",
    "7ED": "ECU #6",
    "7EE": "ECU #7",
    "7EF": "ECU #8",
    "10": "Engine (ECM/PCM)",
    "18": "Transmission (TCM)",
}

ELM_ERRORS = (
    "NO DATA",
    "CAN ERROR",
    "UNABLE TO CONNECT",
    "BUS ERROR",
    "BUS BUSY",
    "FB ERROR",
    "DATA ERROR",
    "BUFFER FULL",
    "STOPPED",
    "ACT ALERT",
    "LV RESET",
    "LP ALERT",
    "RX ERROR",
    "<RX ERROR",
    "ERR",
)

NRC = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceededNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x72: "generalProgrammingFailure",
    0x78: "responsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


class ElmError(Exception):
    """The adapter reported an error string instead of data."""


class NoData(ElmError):
    """No ECU answered the request."""


class VehicleNotResponding(ElmError):
    """Adapter is fine but the vehicle bus is silent (ignition off?)."""


class NegativeResponse(Exception):
    def __init__(self, ecu: str, service: int, code: int) -> None:
        self.ecu = ecu
        self.service = service
        self.code = code
        super().__init__(
            f"ECU {ecu} rejected service {service:02X}: {NRC.get(code, 'unknown')} (0x{code:02X})"
        )


@dataclass(frozen=True)
class EcuMessage:
    ecu: str
    data: bytes

    @property
    def negative(self) -> bool:
        return len(self.data) >= 3 and self.data[0] == 0x7F

    @property
    def service(self) -> int:
        return self.data[0] if self.data else -1

    def hex(self) -> str:
        return self.data.hex(" ").upper()


def ecu_name(header: str) -> str:
    return ECU_NAMES.get(header.upper(), ECU_NAMES.get(header[-2:].upper(), "ECU"))


def is_can(protocol: str) -> bool:
    return protocol in CAN_11BIT or protocol in CAN_29BIT


def functional_header(protocol: str) -> str:
    if protocol in CAN_29BIT:
        return FUNCTIONAL_HEADER_29
    if protocol == "1":
        return FUNCTIONAL_HEADER_PWM
    if protocol in ("4", "5"):
        return FUNCTIONAL_HEADER_KWP
    if protocol in CAN_11BIT or protocol == "0":
        return FUNCTIONAL_HEADER_11
    return FUNCTIONAL_HEADER_LEGACY


def parse_dpn(lines: list[str]) -> str:
    """Parse an ATDPN reply: "A6" (auto-detected 6) -> "6"; "A" (J1939) stays "A"."""
    value = (lines[0] if lines else "").strip().upper()
    if len(value) == 2 and value[0] == "A":
        value = value[1]
    return value if value in PROTOCOLS else "0"


def check_errors(lines: list[str]) -> list[str]:
    """Strip status noise and raise if the adapter reported an error."""
    clean: list[str] = []
    for line in lines:
        upper = line.upper()
        if upper.startswith("SEARCHING"):
            continue
        if upper.startswith("BUS INIT"):
            if "ERROR" in upper:
                raise VehicleNotResponding(line)
            continue
        clean.append(line)
    if not clean:
        raise NoData("NO DATA")
    joined = " ".join(clean).upper()
    if "UNABLE TO CONNECT" in joined:
        raise VehicleNotResponding("UNABLE TO CONNECT")
    data_lines = [c for c in clean if _is_hex(c.replace(" ", ""))]
    if not data_lines:
        if "NO DATA" in joined:
            raise NoData("NO DATA")
        for err in ELM_ERRORS:
            if err in joined:
                raise ElmError(clean[0])
        if clean == ["?"]:
            raise ElmError("Adapter did not understand the command (?)")
    return data_lines


def _is_hex(text: str) -> bool:
    return bool(text) and all(c in string.hexdigits for c in text)


def parse_frames(lines: list[str], protocol: str) -> list[EcuMessage]:
    """Turn raw header-on adapter lines into complete per-ECU messages.

    Expects ATH1 (headers on). Handles ISO-TP single/first/consecutive frames
    for CAN; legacy protocols yield one message per line.
    """
    messages: list[EcuMessage] = []
    pending: dict[str, tuple[int, bytearray]] = {}

    for line in lines:
        hexs = line.replace(" ", "").upper()
        if not _is_hex(hexs):
            continue
        if is_can(protocol):
            hlen = 8 if (protocol in CAN_29BIT and len(hexs) > 8 and not _looks_11bit(line)) else 3
            if len(hexs) <= hlen or (len(hexs) - hlen) % 2:
                continue
            ecu = hexs[:hlen]
            body = bytes.fromhex(hexs[hlen:])
            kind = body[0] >> 4
            if kind == 0:
                length = body[0] & 0x0F
                if length:
                    messages.append(EcuMessage(ecu, body[1 : 1 + length]))
            elif kind == 1 and len(body) >= 2:
                length = ((body[0] & 0x0F) << 8) | body[1]
                pending[ecu] = (length, bytearray(body[2:]))
            elif kind == 2 and ecu in pending:
                length, buf = pending[ecu]
                buf.extend(body[1:])
                if len(buf) >= length:
                    messages.append(EcuMessage(ecu, bytes(buf[:length])))
                    del pending[ecu]
            # kind 3 = flow control: ignore
        else:
            if len(hexs) < 10 or len(hexs) % 2:
                continue
            ecu = hexs[4:6]
            messages.append(EcuMessage(ecu, bytes.fromhex(hexs[6:-2])))  # drop checksum

    # Emit truncated multi-frame messages rather than silently losing them.
    for ecu, (_length, buf) in pending.items():
        messages.append(EcuMessage(ecu, bytes(buf)))

    return _merge_legacy(messages) if not is_can(protocol) else messages


def _looks_11bit(line: str) -> bool:
    first = line.split(" ", 1)[0]
    return " " in line and len(first) == 3


def _merge_legacy(messages: list[EcuMessage]) -> list[EcuMessage]:
    """Legacy buses split long replies into several same-service lines."""
    merged: dict[tuple[str, int], bytearray] = {}
    order: list[tuple[str, int]] = []
    for msg in messages:
        if not msg.data:
            continue
        key = (msg.ecu, msg.data[0])
        if key not in merged:
            merged[key] = bytearray(msg.data)
            order.append(key)
        elif msg.data[0] in (0x43, 0x47, 0x4A):
            merged[key].extend(msg.data[1:])
        elif msg.data[0] == 0x49 and len(msg.data) > 3:
            merged[key].extend(msg.data[3:])  # strip "49 PID seq"
        else:
            merged[key] = bytearray(msg.data)
    return [EcuMessage(ecu, bytes(merged[(ecu, svc)])) for ecu, svc in order]
