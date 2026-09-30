"""A simulated ELM327 WiFi adapter + vehicle for development without a car.

Emulates an ELM327 v1.5 on ISO 15765-4 CAN 11-bit 500k with an engine ECU
(7E8), a transmission ECU (7E9/7EA) and a BMS (7EC). Honours the formatting
AT commands (echo, headers, spaces), ISO-TP multi-frame replies, multi-PID
Mode 01 requests, DTC read/clear, freeze frame, Mode 09 and some Mode 22.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field

VIN = "1FTEW1EP5NFA12345"


def _dtc_bytes(code: str) -> bytes:
    letter = "PCBU".index(code[0])
    hi = (letter << 6) | (int(code[1]) << 4) | int(code[2], 16)
    return bytes([hi, int(code[3:], 16)])


@dataclass
class Vehicle:
    started: float = field(default_factory=time.monotonic)
    stored: list[str] = field(default_factory=lambda: ["P0301", "P0420"])
    pending: list[str] = field(default_factory=lambda: ["P0171"])
    permanent: list[str] = field(default_factory=lambda: ["P0420"])
    tcm_stored: list[str] = field(default_factory=lambda: ["P0741"])
    distance_since_clear: int = 1234
    ignition: bool = True
    bus_error: bool = False  # answer OBD requests with "CAN ERROR"
    no_atd: bool = False  # clone that answers "?" to ATD (set defaults)
    # Extra reply delay (seconds) per command, e.g. {"010C": 0.5}, to simulate slow ECUs.
    slow: dict[str, float] = field(default_factory=dict)

    @property
    def t(self) -> float:
        return time.monotonic() - self.started

    # --- simulated signals ----------------------------------------------------
    def rpm(self) -> float:
        t = self.t
        drive = max(0.0, math.sin(t / 6))
        return 750 + 3200 * drive**2 + 40 * math.sin(t * 3) + random.uniform(-15, 15)

    def speed(self) -> float:
        return max(0.0, (self.rpm() - 750) / 3200 * 110)

    def coolant(self) -> float:
        return min(92.0, 18 + self.t * 1.2) + random.uniform(-0.4, 0.4)

    def throttle(self) -> float:
        return min(100.0, 14 + (self.rpm() - 750) / 40)

    def load(self) -> float:
        return min(100.0, 18 + (self.rpm() - 750) / 45 + random.uniform(-2, 2))


class EngineEcu:
    """Mode 01 values for the simulated engine."""

    def __init__(self, v: Vehicle) -> None:
        self.v = v

    def pid(self, pid: int) -> bytes | None:
        v = self.v
        match pid:
            case 0x01:
                mil = 0x80 if v.stored else 0x00
                return bytes([mil | len(v.stored), 0x07, 0x65, 0x04 if v.stored else 0x00])
            case 0x03:
                return bytes([0x02 if v.coolant() > 40 else 0x01, 0x00])
            case 0x04:
                return bytes([round(v.load() * 255 / 100)])
            case 0x05:
                return bytes([round(v.coolant() + 40)])
            case 0x06:
                return bytes([128 + random.randint(-6, 6)])
            case 0x07:
                return bytes([128 + 5])
            case 0x0B:
                return bytes([round(30 + v.load() * 0.7)])
            case 0x0C:
                raw = round(v.rpm() * 4)
                return raw.to_bytes(2, "big")
            case 0x0D:
                return bytes([round(v.speed())])
            case 0x0E:
                advance = 12 + 10 * math.sin(v.t)
                return bytes([round((advance + 64) * 2)])
            case 0x0F:
                return bytes([round(24 + 40)])
            case 0x10:
                return round((2.5 + v.load() * 0.9) * 100).to_bytes(2, "big")
            case 0x11:
                return bytes([round(v.throttle() * 255 / 100)])
            case 0x13:
                return bytes([0x03])
            case 0x14:
                return bytes([round((0.45 + 0.4 * math.sin(v.t * 5)) * 200), 0xFF])
            case 0x15:
                return bytes([round(0.68 * 200), 0x80])
            case 0x1C:
                return bytes([0x01])
            case 0x1F:
                return round(v.t).to_bytes(2, "big")
            case 0x21:
                return (42).to_bytes(2, "big")
            case 0x2F:
                return bytes([round(0.63 * 255)])
            case 0x30:
                return bytes([12])
            case 0x31:
                return v.distance_since_clear.to_bytes(2, "big")
            case 0x33:
                return bytes([101])
            case 0x42:
                return round((14.1 + random.uniform(-0.1, 0.1)) * 1000).to_bytes(2, "big")
            case 0x46:
                return bytes([18 + 40])
            case 0x49:
                return bytes([round(v.throttle() * 0.9 * 255 / 100)])
            case 0x4C:
                return bytes([round(v.throttle() * 255 / 100)])
            case 0x51:
                return bytes([0x01])
            case 0x5C:
                return bytes([round(v.coolant() * 1.05 + 40)])
            case 0xA6:
                return (1_234_567).to_bytes(4, "big")
        return None

    SUPPORTED = (
        [0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x13,
         0x14, 0x15, 0x1C, 0x1F, 0x21, 0x2F, 0x30, 0x31, 0x33, 0x42, 0x46, 0x49, 0x4C, 0x51,
         0x5C, 0xA6]
    )


def _bitmap(base: int, supported: list[int]) -> bytes | None:
    bits = 0
    for p in supported:
        if base < p <= base + 32:
            bits |= 1 << (32 - (p - base))
    # Advertise the next support PID if anything beyond this block exists.
    if any(p > base + 32 for p in supported):
        bits |= 1
    return bits.to_bytes(4, "big")


class EmulatedElm:
    def __init__(self, vehicle: Vehicle | None = None) -> None:
        self.vehicle = vehicle or Vehicle()
        self.engine = EngineEcu(self.vehicle)
        self.reset()

    def reset(self) -> None:
        self.echo = True
        self.headers = False
        self.spaces = True
        self.header = "7DF"
        self.auto = True
        self.searched = False
        self.last = ""

    # --- AT commands ----------------------------------------------------------
    def at(self, cmd: str) -> list[str]:
        c = cmd[2:]
        if c == "D" and self.vehicle.no_atd:
            return ["?"]
        if c in ("Z", "WS", "D"):
            self.reset()
            return ["", "ELM327 v1.5"] if c != "D" else ["OK"]
        if c == "I":
            return ["ELM327 v1.5"]
        if c == "@1":
            return ["OBDII to RS232 Interpreter"]
        if c in ("E0", "E1"):
            self.echo = c == "E1"
        elif c in ("H0", "H1"):
            self.headers = c == "H1"
        elif c in ("S0", "S1"):
            self.spaces = c == "S1"
        elif c.startswith("SP") or c.startswith("TP"):
            self.auto = c[2:] in ("0", "A6", "A0", "")
        elif c == "DPN":
            return ["A6" if self.auto else "6"]
        elif c == "DP":
            return [("AUTO, " if self.auto else "") + "ISO 15765-4 (CAN 11/500)"]
        elif c == "RV":
            return [f"{14.1 + random.uniform(-0.2, 0.2) if self.vehicle.ignition else 12.4:.1f}V"]
        elif c.startswith("SH"):
            self.header = c[2:]
        elif c.startswith(("L", "AT", "CAF", "CRA", "ST", "FC", "PC", "M", "AR", "AL", "CF", "CM")):
            pass
        elif c.startswith("PP"):
            return ["OK"]
        else:
            return ["?"]
        return ["OK"]

    # --- OBD requests -----------------------------------------------------------
    def obd(self, hexs: str) -> list[str]:
        if len(hexs) % 2:
            hexs = hexs[:-1]  # trailing "expected responses" digit
        req = bytes.fromhex(hexs)
        prefix: list[str] = []
        if self.auto and not self.searched:
            prefix = ["SEARCHING..."]
            self.searched = True
        if not self.vehicle.ignition:
            return prefix + ["UNABLE TO CONNECT"]
        if self.vehicle.bus_error:
            return prefix + ["CAN ERROR"]
        replies = self.dispatch(req)
        if not replies:
            return prefix + ["NO DATA"]
        lines: list[str] = []
        for ecu, payload in replies:
            lines += self.format(ecu, payload)
        return prefix + lines

    def dispatch(self, req: bytes) -> list[tuple[str, bytes]]:
        header = self.header.upper()
        targets = {"7DF": ["7E8", "7E9"], "7E0": ["7E8"], "7E1": ["7E9"], "7E2": ["7EA"], "7E4": ["7EC"]}
        out: list[tuple[str, bytes]] = []
        for ecu in targets.get(header, [f"{int(header, 16) + 8:03X}"] if len(header) == 3 else []):
            resp = self.ecu_reply(ecu, req, functional=header == "7DF")
            if resp is not None:
                out.append((ecu, resp))
        return out

    def ecu_reply(self, ecu: str, req: bytes, functional: bool) -> bytes | None:
        v = self.vehicle
        svc = req[0]
        if ecu == "7E8":
            if svc == 0x01:
                body = bytearray([0x41])
                for pid in req[1:]:
                    if pid % 0x20 == 0:
                        body += bytes([pid]) + _bitmap(pid, EngineEcu.SUPPORTED)
                    elif (d := self.engine.pid(pid)) is not None:
                        body += bytes([pid]) + d
                return bytes(body) if len(body) > 1 else None
            if svc == 0x02 and len(req) >= 3:
                pid = req[1]
                if pid == 0x02:
                    return bytes([0x42, 0x02, 0x00]) + _dtc_bytes("P0301")
                if pid == 0x00:
                    return bytes([0x42, 0x00, 0x00]) + _bitmap(0, [0x02, 0x04, 0x05, 0x0C, 0x0D, 0x11])
                if pid in (0x04, 0x05, 0x0C, 0x0D, 0x11):
                    snap = {0x04: b"\x66", 0x05: b"\x7c", 0x0C: b"\x0b\xb8", 0x0D: b"\x3c", 0x11: b"\x40"}
                    return bytes([0x42, pid, 0x00]) + snap[pid]
                return None
            if svc in (0x03, 0x07, 0x0A):
                codes = {0x03: v.stored, 0x07: v.pending, 0x0A: v.permanent}[svc]
                return bytes([svc + 0x40, len(codes)]) + b"".join(_dtc_bytes(c) for c in codes)
            if svc == 0x04:
                v.stored.clear()
                v.pending.clear()
                v.distance_since_clear = 0
                return b"\x44"
            if svc == 0x09 and len(req) >= 2:
                return self.mode09(req[1])
            if svc == 0x22 and len(req) >= 3 and not functional:
                did = req[1:3].hex().upper()
                table = {
                    "F190": VIN.encode(),
                    "F188": b"EMU-STRAT-A1B2",
                    "1E1C": round((72 + 6 * math.sin(v.t / 10)) * 16).to_bytes(2, "big", signed=True),
                }
                return (b"\x62" + req[1:3] + table[did]) if did in table else bytes([0x7F, 0x22, 0x31])
            if not functional:
                return bytes([0x7F, svc, 0x11 if svc not in (0x2E, 0x31, 0x2F, 0x34) else 0x33])
            return None
        if ecu == "7E9":
            if svc == 0x01 and len(req) >= 2:
                if req[1] == 0x00:
                    return b"\x41\x00" + _bitmap(0, [0x01, 0x0D])
                if req[1] == 0x0D:
                    return bytes([0x41, 0x0D, round(v.speed())])
                return None
            if svc == 0x03:
                return bytes([0x43, len(v.tcm_stored)]) + b"".join(_dtc_bytes(c) for c in v.tcm_stored)
            if svc in (0x07, 0x0A):
                return bytes([svc + 0x40, 0])
            if svc == 0x04:
                v.tcm_stored.clear()
                return b"\x44"
            return None
        if ecu == "7EA" and svc == 0x22 and req[1:3] == b"\x19\x40":
            return b"\x62\x19\x40" + bytes([round(70 + 5 * math.sin(v.t / 12)) + 40])
        if ecu == "7EC" and svc == 0x22 and req[1:3] == b"\x01\x01":
            return b"\x62\x01\x01" + bytes(random.randint(0, 255) for _ in range(38))
        if svc == 0x22:
            return bytes([0x7F, 0x22, 0x31])
        return None

    def mode09(self, pid: int) -> bytes | None:
        match pid:
            case 0x00:
                return b"\x49\x00" + _bitmap(0, [0x02, 0x04, 0x06, 0x0A])
            case 0x02:
                return b"\x49\x02\x01" + VIN.encode()
            case 0x04:
                return b"\x49\x04\x01" + b"EMU-CAL-0001".ljust(16, b"\x00")
            case 0x06:
                return b"\x49\x06\x01" + bytes.fromhex("1A2B3C4D")
            case 0x0A:
                return b"\x49\x0A\x01" + b"ECM\x00-EngineControl".ljust(20, b"\x00")
        return None

    # --- output formatting --------------------------------------------------------
    def format(self, ecu: str, payload: bytes) -> list[str]:
        frames: list[bytes] = []
        if len(payload) <= 7:
            frames.append(bytes([len(payload)]) + payload)
        else:
            frames.append(bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6])
            rest, seq = payload[6:], 1
            while rest:
                frames.append(bytes([0x20 | (seq & 0xF)]) + rest[:7])
                rest, seq = rest[7:], seq + 1
        frames = [f.ljust(8, b"\x00") if self.headers else f for f in frames]
        sep = " " if self.spaces else ""

        def hx(b: bytes) -> str:
            return sep.join(f"{x:02X}" for x in b)

        if self.headers:
            return [f"{ecu}{sep}{hx(f)}" for f in frames]
        if len(frames) == 1:
            return [hx(frames[0][1 : 1 + len(payload)])]
        lines = [f"{len(payload):03X}", f"0:{sep}{hx(payload[:6])}"]
        rest, seq = payload[6:], 1
        while rest:
            lines.append(f"{seq & 0xF:X}:{sep}{hx(rest[:7])}")
            rest, seq = rest[7:], seq + 1
        return lines

    def handle(self, raw: str) -> str:
        cmd = raw.strip().replace(" ", "").upper()
        if not cmd:
            # Like a real ELM327: an empty line repeats the previous command.
            if not self.last:
                return ">"
            raw = cmd = self.last
        self.last = cmd
        if cmd.startswith("AT"):
            lines = self.at(cmd)
        elif all(c in "0123456789ABCDEF" for c in cmd):
            lines = self.obd(cmd)
        else:
            lines = ["?"]
        echo = [raw.strip()] if self.echo else []
        return "\r".join(echo + lines) + "\r\r>"


async def _client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, vehicle: Vehicle) -> None:
    elm = EmulatedElm(vehicle)
    buf = b""
    try:
        while data := await reader.read(1024):
            buf += data.replace(b"\n", b"")
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                text = line.decode("ascii", "replace")
                cmd = text.strip().replace(" ", "").upper() or elm.last
                is_obd = not cmd.startswith("AT")
                await asyncio.sleep(random.uniform(0.02, 0.05) if is_obd else 0.005)
                await asyncio.sleep(vehicle.slow.get(cmd, 0.0))
                writer.write(elm.handle(text).encode("ascii"))
                await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def serve(host: str = "127.0.0.1", port: int = 35000, vehicle: Vehicle | None = None) -> asyncio.Server:
    v = vehicle or Vehicle()
    return await asyncio.start_server(lambda r, w: _client(r, w, v), host, port)


async def run_forever(host: str = "127.0.0.1", port: int = 35000, ignition: bool = True) -> None:
    server = await serve(host, port, Vehicle(ignition=ignition))
    addr = ", ".join(str(s.getsockname()) for s in server.sockets)
    print(f"ELM327 emulator listening on {addr}  (Ctrl+C to stop)")
    async with server:
        await server.serve_forever()
