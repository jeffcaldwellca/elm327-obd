"""Find an ELM327 WiFi adapter on the local network."""

from __future__ import annotations

import asyncio
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

COMMON = [
    ("192.168.0.10", 35000),
    ("192.168.0.10", 23),
    ("192.168.0.10", 4000),
    ("192.168.1.5", 35000),
    ("192.168.1.10", 35000),
    ("192.168.0.123", 35000),
    ("192.168.4.1", 35000),
    ("10.10.10.1", 35000),
]
PORTS = (35000, 23)


@dataclass
class Found:
    host: str
    port: int
    banner: str


def default_gateway() -> str | None:
    """Router address of the current network (often the adapter itself)."""
    for cmd in (["route", "-n", "get", "default"], ["ip", "-4", "route", "show", "default"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            continue  # macOS has `route -n get`, Linux has `ip route`
        gw = parse_gateway(out)
        if gw:
            return gw
    return None


def parse_gateway(output: str) -> str | None:
    # macOS: "gateway: 192.168.0.10"   Linux: "default via 192.168.0.10 dev wlan0 ..."
    m = re.search(r"gateway:\s*([\d.]+)", output) or re.search(r"default via ([\d.]+)", output)
    return m.group(1) if m else None


def wifi_network() -> str | None:
    """Best-effort current SSID (macOS may hide it without Location permission)."""
    try:
        out = subprocess.run(
            ["ipconfig", "getsummary", "en0"], capture_output=True, text=True, timeout=2
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.MULTILINE)
    return m.group(1).strip() if m else None


def candidates(extra: list[tuple[str, int]] | None = None) -> list[tuple[str, int]]:
    seen: list[tuple[str, int]] = []
    gw = default_gateway()
    pool = list(extra or [])
    if gw:
        pool += [(gw, p) for p in PORTS]
    pool += COMMON
    for c in pool:
        if c not in seen:
            seen.append(c)
    return seen


async def probe(host: str, port: int, timeout: float = 1.5) -> Found | None:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        writer.write(b"ATI\r")
        await writer.drain()
        data = await asyncio.wait_for(reader.readuntil(b">"), 2.5)
        text = data.decode("ascii", "replace").replace("\r", " ").strip(" >")
        banner = next((t for t in text.split("  ") if "ELM" in t.upper()), text).strip()
        return Found(host, port, banner) if "ELM" in text.upper() or "OBD" in text.upper() else None
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None
    finally:
        writer.close()


async def scan(
    extra: list[tuple[str, int]] | None = None,
    on_result: Callable[[str, int, Found | None], None] | None = None,
) -> list[Found]:
    targets = candidates(extra)

    async def one(host: str, port: int) -> Found | None:
        res = await probe(host, port)
        if on_result:
            on_result(host, port, res)
        return res

    results = await asyncio.gather(*(one(h, p) for h, p in targets))
    return [r for r in results if r]
