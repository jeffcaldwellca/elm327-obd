"""Command-line entry point: ``obd`` (TUI) plus a few headless helpers."""

from __future__ import annotations

import argparse
import asyncio
import sys

from elm327obd import __version__
from elm327obd.protocol import ElmError
from elm327obd.transport import TransportError


def _addr(value: str | None) -> tuple[str | None, int | None]:
    if not value:
        return None, None
    host, _, port = value.partition(":")
    try:
        port_num = int(port or 35000)
    except ValueError:
        port_num = -1
    if not host or not 0 < port_num < 65536:
        sys.exit(f"Invalid adapter address {value!r} – expected HOST:PORT, e.g. 192.168.0.10:35000")
    return host, port_num


async def _open(addr: str | None):
    from elm327obd import scanner
    from elm327obd.config import Config
    from elm327obd.elm import ELM327
    from elm327obd.transport import ElmTransport

    host, port = _addr(addr)
    if host is None:
        cfg = Config.load()
        host, port = cfg.host, cfg.port
    if host is None:
        print("Scanning for adapter…", file=sys.stderr)
        found = await scanner.scan()
        if not found:
            sys.exit("No ELM327 adapter found. Join its Wi-Fi or pass --adapter HOST:PORT.")
        host, port = found[0].host, found[0].port
    elm = ELM327(ElmTransport(host, port or 35000))
    await elm.connect(progress=lambda m: print(f"  {m}", file=sys.stderr))
    return elm


async def _cmd_scan(_args) -> None:
    from elm327obd import scanner

    print(f"Wi-Fi: {scanner.wifi_network() or 'unknown'}   gateway: {scanner.default_gateway() or 'none'}")

    def show(host: str, port: int, found) -> None:
        mark = f"✔ {found.banner}" if found else "✖"
        print(f"  {host}:{port:<6} {mark}")

    found = await scanner.scan(on_result=show)
    print(f"\n{len(found)} adapter(s) found.")


async def _cmd_codes(args) -> None:
    elm = await _open(args.adapter)
    try:
        if not elm.vehicle_ok:
            sys.exit("Vehicle not responding (ignition ON?)")
        codes = sorted(await elm.read_dtcs(), key=lambda d: (d.guide.rank, d.code))
        if not codes:
            print("No trouble codes.")
        for c in codes:
            print(f"{c.guide.severity.upper():<8} {c.code}  {c.kind:<9}  {c.ecu:<4}  {c.description}")
        if codes:
            print("\nRun `obd explain CODE` for likely causes and what to check.")
        if args.clear:
            if input("Clear all codes, freeze frame and readiness monitors? [y/N] ").lower() == "y":
                print("Cleared by:", ", ".join(await elm.clear_dtcs()) or "no modules")
    finally:
        await elm.close()


def _cmd_explain(args) -> None:
    import re
    import textwrap

    from elm327obd.dtc import SEVERITY_TEXT, guide, name_source

    def item(prefix: str, text: str) -> str:
        return textwrap.fill(text, width=88, initial_indent=prefix, subsequent_indent=" " * len(prefix))

    for raw in args.codes:
        code = raw.strip().upper()
        if not re.fullmatch(r"[PCBU][0-3][0-9A-F]{3}", code):
            print(f"{raw}: not a trouble code (expected e.g. P0420, U0100)\n")
            continue
        g = guide(code)
        estimated = g.source == "estimated"
        print(f"{code}  [{g.severity.upper()}{' – estimated' if estimated else ''}]  {g.name}")
        if g.summary:
            print(item("     ", g.summary))
        print(item("     ", SEVERITY_TEXT[g.severity]))
        if g.causes:
            print("   Likely causes:")
            for i, cause in enumerate(g.causes, 1):
                print(item(f"     {i}. ", cause))
        if g.checks:
            print("   What to check:")
            for check in g.checks:
                print(item("     - ", check))
        note = "estimated from the code's name/category" if estimated else "hand-written guide"
        print(f"   (name: {name_source(code)}; guidance: {note})\n")


async def _cmd_info(args) -> None:
    from elm327obd.protocol import ecu_name

    elm = await _open(args.adapter)
    try:
        print(f"Adapter : {elm.version} @ {elm.transport.address}")
        print(f"Voltage : {await elm.voltage()} V")
        if not elm.vehicle_ok:
            sys.exit("Vehicle not responding (ignition ON?)")
        info = await elm.vehicle_info()
        print(f"Protocol: {elm.protocol_name}")
        print(f"VIN     : {info.vin or 'n/a'}")
        for ecu in elm.ecus:
            print(f"Module  : {ecu} {ecu_name(ecu)} {info.ecu_names.get(ecu, '')}")
        for cal in info.calibration_ids:
            print(f"CAL ID  : {cal}")
        status = await elm.monitor_status()
        if status:
            print(f"MIL     : {'ON' if status.mil_on else 'off'}  ({status.dtc_count} DTC)")
            for name, avail, done in status.monitors:
                if avail:
                    print(f"  {'✔' if done else '✖'} {name}")
    finally:
        await elm.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="obd", description="ELM327 WiFi OBD-II diagnostics")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-a", "--adapter", metavar="HOST:PORT", help="connect directly (skip scan)")
    parser.add_argument("--demo", action="store_true", help="run against the built-in emulator")
    parser.add_argument("--imperial", action="store_true", default=None, help="use °F / mph / psi")
    sub = parser.add_subparsers(dest="command")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-a", "--adapter", metavar="HOST:PORT", default=argparse.SUPPRESS,
                        help="adapter address (default: last used, else scan)")

    sub.add_parser("scan", help="find adapters on the current network")
    p_codes = sub.add_parser("codes", parents=[common], help="print trouble codes (headless)")
    p_codes.add_argument("--clear", action="store_true", help="clear codes after printing (asks first)")
    sub.add_parser("info", parents=[common], help="print vehicle info and readiness (headless)")
    p_explain = sub.add_parser("explain", help="explain trouble codes offline (no car needed)")
    p_explain.add_argument("codes", nargs="+", metavar="CODE", help="e.g. P0420 P0171")
    p_emu = sub.add_parser("emulator", help="run a fake ELM327 + vehicle for testing")
    p_emu.add_argument("--host", default="127.0.0.1")
    p_emu.add_argument("--port", type=int, default=35000)
    p_emu.add_argument("--ignition-off", action="store_true", help="simulate an unresponsive vehicle")

    args = parser.parse_args(argv)
    try:
        match args.command:
            case "scan":
                asyncio.run(_cmd_scan(args))
            case "codes":
                asyncio.run(_cmd_codes(args))
            case "info":
                asyncio.run(_cmd_info(args))
            case "explain":
                _cmd_explain(args)
            case "emulator":
                from elm327obd.emulator import run_forever

                asyncio.run(run_forever(args.host, args.port, ignition=not args.ignition_off))
            case _:
                from elm327obd.tui.app import run

                host, port = _addr(args.adapter)
                run(host=host, port=port, demo=args.demo, imperial=args.imperial)
    except KeyboardInterrupt:
        pass
    except (TransportError, ElmError) as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
