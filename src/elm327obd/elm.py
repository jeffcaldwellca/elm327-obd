"""High-level async ELM327 client."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from elm327obd import pids as P
from elm327obd.dtc import KIND_LABEL, Dtc, parse_dtc_payload
from elm327obd.profiles import ExtPid, evaluate
from elm327obd.protocol import (
    PROTOCOLS,
    EcuMessage,
    ElmError,
    NegativeResponse,
    NoData,
    VehicleNotResponding,
    check_errors,
    functional_header,
    is_can,
    parse_dpn,
    parse_frames,
)
from elm327obd.transport import ElmTransport, TransportError

Progress = Callable[[str], None]

# Console AT commands that change reply formatting (our parser can't read the
# output any more) vs. ones that change addressing/protocol state. Either kind
# means the live poller needs a settings restore afterwards.
FORMAT_AT = re.compile(r"^AT(?:[EHSL][01]|CAF[01]|D1?|D0|Z|WS|R[01])$")
STATE_AT = re.compile(r"^AT(?:SH|CRA|CF|CM|AR|SP|TP|FC|CP|CEA|TA|SR|AT|ST|PB|BI)")
RESET_AT = re.compile(r"^AT(?:Z|WS|D)$")  # restore default header/filter

INIT_SEQUENCE = ("ATE0", "ATL0", "ATS1", "ATH1", "ATAT1", "ATCAF1")

# Mode 01 PIDs per request. Start at the J1979 maximum and step down when an
# adapter fails a multi-PID request (some clones manage 2 but hang on 6).
BATCH_STEPS = (6, 2, 1)


@dataclass
class VehicleInfo:
    vin: str = ""
    calibration_ids: list[str] = field(default_factory=list)
    cvns: list[str] = field(default_factory=list)
    ecu_names: dict[str, str] = field(default_factory=dict)


class ELM327:
    def __init__(self, transport: ElmTransport) -> None:
        self.transport = transport
        self.version = ""
        self.protocol = "0"
        self.ecus: list[str] = []
        self.supported: set[int] = set()
        self.vehicle_ok = False
        self.batch_size = BATCH_STEPS[0]
        # Header / receive filter currently programmed into the adapter
        # (None = adapter default, i.e. functional broadcast / auto filter).
        self._header: str | None = None
        self._rx: str | None = None
        self.settings_dirty = False  # needs apply_settings() before polling
        self.format_dirty = False  # replies no longer in our header-on format

    # ---- lifecycle ---------------------------------------------------------

    @property
    def protocol_name(self) -> str:
        return PROTOCOLS.get(self.protocol, f"Protocol {self.protocol}")

    async def connect(self, progress: Progress | None = None, protocol: str = "0") -> None:
        say = progress or (lambda _m: None)
        say(f"Opening {self.transport.address}…")
        await self.transport.open()
        say("Resetting adapter…")
        await self._reset()
        await self.apply_settings(protocol)
        say("Searching for vehicle protocol (can take ~10 s)…")
        try:
            await self.detect_vehicle()
            say(f"Vehicle online · {self.protocol_name}")
        except ElmError as exc:  # NO DATA, UNABLE TO CONNECT, CAN ERROR, BUS ERROR…
            self.vehicle_ok = False
            say(f"Adapter OK, vehicle not responding ({exc}). Is the ignition ON?")

    async def close(self) -> None:
        await self.transport.close()

    async def _reset(self) -> None:
        lines: list[str] = []
        for cmd in ("ATZ", "ATWS"):
            try:
                lines = await self.transport.command(cmd, timeout=5)
            except TransportError:
                continue
            if any("ELM" in line.upper() for line in lines):
                break
        await asyncio.sleep(0.3)
        info = await self.transport.command("ATI", timeout=3)
        self.version = next((line for line in info if "ELM" in line.upper()), " ".join(lines) or "?")

    async def apply_settings(self, protocol: str = "0") -> None:
        """Put the adapter into the known state the parser and poller expect.

        ATD restores factory defaults first, which also clears any header
        (ATSH), receive filter (ATCRA) or flow-control settings left over from
        the console or Mode 22 requests, so the cached addressing state below
        is actually true afterwards.
        """
        async with self.transport.session():
            reply = await self.transport.command_unlocked("ATD", timeout=3)
            for cmd in INIT_SEQUENCE:
                await self.transport.command_unlocked(cmd, timeout=3)
            await self.transport.command_unlocked(f"ATSP{protocol}", timeout=3)
            header = None
            if not any("OK" in line.upper() for line in reply):
                # Some clones don't implement ATD: reset addressing explicitly.
                header = functional_header(self.protocol if protocol == "0" else protocol)
                await self.transport.command_unlocked(f"ATSH{header}", timeout=3)
                await self.transport.command_unlocked("ATCRA", timeout=3)
        self._header = header
        self._rx = None
        self.settings_dirty = False
        self.format_dirty = False

    async def detect_vehicle(self) -> None:
        if self.settings_dirty or self._header is not None or self._rx is not None:
            # Re-detection must broadcast, not go to the last physical ECU used.
            await self.apply_settings("0")
        async with self.transport.session():
            raw = await self.transport.command_unlocked("0100", timeout=25)
            dpn = await self.transport.command_unlocked("ATDPN", timeout=3)
        self.protocol = parse_dpn(dpn)
        lines = check_errors(raw)
        msgs = parse_frames(lines, self.protocol)
        self.ecus = sorted({m.ecu for m in msgs if m.service == 0x41})
        if not self.ecus:
            raise NoData("No ECU answered 0100")
        self.vehicle_ok = True
        self.supported = await self._supported_pids(msgs)

    async def restore_if_dirty(self) -> None:
        if self.settings_dirty:
            await self.apply_settings(self.protocol if self.vehicle_ok else "0")

    # ---- low level ---------------------------------------------------------

    async def raw(self, cmd: str, timeout: float = 8.0) -> list[str]:
        """Send a command verbatim (console use)."""
        up = cmd.replace(" ", "").upper()
        lines = await self.transport.command(cmd, timeout=timeout)
        if FORMAT_AT.match(up):
            self.settings_dirty = self.format_dirty = True
        elif STATE_AT.match(up):
            self.settings_dirty = True
        if RESET_AT.match(up):
            self._header = self._rx = None
        elif up.startswith("ATSH"):
            self._header = up[4:] or None
        elif up.startswith("ATCRA"):
            self._rx = up[5:] or None
        return lines

    async def request(
        self,
        cmd: str,
        *,
        header: str | None = None,
        rx: str | None = None,
        timeout: float = 5.0,
    ) -> list[EcuMessage]:
        """Send an OBD/UDS request and return reassembled ECU messages."""
        # Console experiments may have changed formatting or addressing; any
        # app-issued request (poller, code reads, scans…) first restores our state.
        await self.restore_if_dirty()
        want_header = header or functional_header(self.protocol)
        async with self.transport.session():
            if want_header != (self._header or functional_header(self.protocol)):
                await self.transport.command_unlocked(f"ATSH{want_header}", timeout=2)
                self._header = want_header
            if rx != self._rx:
                await self.transport.command_unlocked(f"ATCRA{rx or ''}", timeout=2)
                self._rx = rx
            lines = await self.transport.command_unlocked(cmd, timeout=timeout)
        msgs = parse_frames(check_errors(lines), self.protocol)
        # Drop "response pending" chatter if a real answer followed.
        final = [m for m in msgs if not (m.negative and m.data[2] == 0x78)]
        return final or msgs

    # ---- Mode 01 -----------------------------------------------------------

    async def _supported_pids(self, first: list[EcuMessage] | None = None) -> set[int]:
        supported: set[int] = set()
        msgs = first
        for base in P.SUPPORT_PIDS:
            if msgs is None:
                try:
                    msgs = await self.request(f"01{base:02X}")
                except (NoData, ElmError):
                    break
            got_next = False
            for m in msgs:
                if len(m.data) >= 6 and m.data[0] == 0x41 and m.data[1] == base:
                    bits = P.decode_support_bitmap(base, m.data[2:6])
                    supported |= bits
                    got_next |= (base + 0x20) in bits
            msgs = None
            if not got_next:
                break
        return supported

    async def read_pids(self, pids: list[int]) -> dict[int, bytes]:
        """Read Mode 01 PIDs, batching up to ``batch_size`` per request on CAN."""
        results: dict[int, bytes] = {}
        size = self.batch_size if is_can(self.protocol) else 1
        for i in range(0, len(pids), size):
            chunk = pids[i : i + size]
            try:
                msgs = await self.request("01" + "".join(f"{p:02X}" for p in chunk))
            except NoData:
                continue
            except TransportError:
                # Some clones hang (no reply, no prompt) on multi-PID requests
                # instead of answering; the transport has already resynced.
                if len(chunk) == 1 or not self.transport.connected:
                    raise
                msgs = []
            got = _parse_mode01(msgs)
            if len(chunk) > 1 and not got:
                # Some clones choke on multi-PID requests: use smaller ones for good.
                self.batch_size = next(n for n in BATCH_STEPS if n < len(chunk))
                return results | await self.read_pids(pids[i:])
            results.update(got)
        return results

    async def read_decoded(self, pids: list[int]) -> dict[int, P.Value]:
        raw = await self.read_pids(pids)
        out: dict[int, P.Value] = {}
        for pid, data in raw.items():
            value = P.decode(pid, data)
            if value is not None:
                out[pid] = value
        return out

    async def monitor_status(self) -> P.MonitorStatus | None:
        raw = await self.read_pids([0x01])
        return P.decode_monitor_status(raw[0x01]) if 0x01 in raw else None

    async def voltage(self) -> float | None:
        """Battery voltage at the OBD port. Raises TransportError if the link is down."""
        lines = await self.transport.command("ATRV", timeout=3)
        for line in lines:
            try:
                return float(line.upper().rstrip("V"))
            except ValueError:
                continue
        return None

    # ---- DTCs --------------------------------------------------------------

    async def read_dtcs(self) -> list[Dtc]:
        found: list[Dtc] = []
        for service in (0x03, 0x07, 0x0A):
            try:
                msgs = await self.request(f"{service:02X}", timeout=6)
            except NoData:
                continue
            for m in msgs:
                if m.service != service + 0x40:
                    continue
                for code in parse_dtc_payload(m.data[1:], has_count=is_can(self.protocol)):
                    found.append(Dtc(code, m.ecu, KIND_LABEL[service]))
        return found

    async def clear_dtcs(self) -> list[str]:
        """Mode 04. Returns ECUs that acknowledged. Raises on rejection."""
        msgs = await self.request("04", timeout=10)
        acked = [m.ecu for m in msgs if m.service == 0x44]
        if not acked:
            neg = next((m for m in msgs if m.negative), None)
            if neg:
                raise NegativeResponse(neg.ecu, 0x04, neg.data[2])
        return acked

    async def freeze_frame(self) -> tuple[str | None, dict[int, P.Value]]:
        """Return (DTC that triggered the freeze frame, decoded PIDs)."""
        trigger: str | None = None
        values: dict[int, P.Value] = {}
        try:
            msgs = await self.request("020200")
            for m in msgs:
                if m.service == 0x42 and len(m.data) >= 5:
                    codes = parse_dtc_payload(m.data[3:5], has_count=False)
                    trigger = codes[0] if codes else None
                    break
        except NoData:
            return None, {}
        for pid in sorted(await self._freeze_frame_pids()):
            if pid in (0x01, 0x02, 0x13, 0x1C, 0x51) or pid not in P.PIDS:
                continue
            try:
                msgs = await self.request(f"02{pid:02X}00")
            except NoData:
                continue
            for m in msgs:
                if m.service == 0x42 and len(m.data) > 3 and m.data[1] == pid:
                    v = P.decode(pid, m.data[3:])
                    if v is not None:
                        values[pid] = v
                    break
        return trigger, values

    async def _freeze_frame_pids(self) -> set[int]:
        """PIDs stored in freeze frame 0 (Mode 02 support bitmaps), else Mode 01 set."""
        supported: set[int] = set()
        for base in P.SUPPORT_PIDS:
            try:
                msgs = await self.request(f"02{base:02X}00")
            except (NoData, ElmError):
                break
            bits: set[int] = set()
            for m in msgs:
                # 42 <pid> <frame> <4 bitmap bytes>
                if m.service == 0x42 and len(m.data) >= 7 and m.data[1] == base:
                    bits |= P.decode_support_bitmap(base, m.data[3:7])
            supported |= bits
            if (base + 0x20) not in bits:
                break
        return supported or (self.supported & set(P.PIDS))

    # ---- Mode 09 -----------------------------------------------------------

    async def vehicle_info(self) -> VehicleInfo:
        info = VehicleInfo()
        for pid, handler in ((0x02, "vin"), (0x04, "cal"), (0x06, "cvn"), (0x0A, "name")):
            try:
                msgs = await self.request(f"09{pid:02X}", timeout=6)
            except (NoData, ElmError):
                continue
            for m in msgs:
                if m.service != 0x49 or len(m.data) < 3 or m.data[1] != pid:
                    continue
                body = m.data[3:]
                if handler == "vin" and not info.vin:
                    info.vin = _ascii(body)[-17:]
                elif handler == "cal":
                    info.calibration_ids += [_ascii(body[i : i + 16]) for i in range(0, len(body), 16)]
                elif handler == "cvn":
                    info.cvns += [body[i : i + 4].hex().upper() for i in range(0, len(body), 4)]
                elif handler == "name":
                    info.ecu_names[m.ecu] = _ascii(body).replace("\x00", "")
        return info

    # ---- Mode 22 / extended ------------------------------------------------

    async def read_ext(self, pid: ExtPid) -> P.Value:
        msgs = await self.request(pid.command, header=pid.header, rx=pid.rx, timeout=4)
        echo = bytes.fromhex(pid.command)
        for m in msgs:
            if m.negative:
                raise NegativeResponse(m.ecu, pid.service, m.data[2])
            if m.service == pid.service + 0x40 and m.data[1 : len(echo)] == echo[1:]:
                return evaluate(pid.formula, m.data[len(echo) :])
        raise NoData(f"No reply to {pid.command}")

    async def scan_dids(
        self,
        header: str,
        start: int,
        end: int,
        *,
        rx: str | None = None,
        on_hit: Callable[[int, bytes], None] | None = None,
        on_progress: Callable[[int], None] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> dict[int, bytes]:
        """Probe Service 22 DIDs (read-only) and return the ones that answer."""
        hits: dict[int, bytes] = {}
        timeouts = 0
        for did in range(start, end + 1):
            if cancelled():
                break
            if on_progress:
                on_progress(did)
            try:
                msgs = await self.request(f"22{did:04X}", header=header, rx=rx, timeout=1.5)
            except (NoData, ElmError):
                timeouts = 0  # the adapter answered, so the link is alive
                continue
            except TransportError:
                timeouts += 1
                if not self.transport.connected or timeouts >= 3:
                    raise  # link is gone: stop instead of "finishing" the range
                continue  # a single slow DID timed out
            timeouts = 0
            for m in msgs:
                if m.service == 0x62 and len(m.data) >= 3:
                    hits[did] = m.data[3:]
                    if on_hit:
                        on_hit(did, m.data[3:])
                    break
        return hits


def _parse_mode01(msgs: list[EcuMessage]) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    for m in sorted(msgs, key=lambda m: m.ecu):  # prefer the engine ECU (lowest)
        if m.service != 0x41:
            continue
        i, data = 1, m.data
        while i < len(data):
            pid = data[i]
            size = P.PID_SIZES.get(pid)
            if size is None or i + 1 + size > len(data):
                break
            out.setdefault(pid, data[i + 1 : i + 1 + size])
            i += 1 + size
    return out


def _ascii(data: bytes) -> str:
    return "".join(chr(c) for c in data if 32 <= c < 127).strip()
