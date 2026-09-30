"""Manufacturer-specific (Mode 22 / UDS ReadDataByIdentifier) PID profiles.

Profiles are TOML files. Built-ins ship in ``elm327obd/profiles/`` and you can
add or override your own in ``~/.config/elm327obd/profiles/``. Example entry::

    [[pid]]
    name    = "Transmission fluid temp"
    header  = "7E1"          # physical request address (ATSH)
    rx      = "7E9"          # optional CAN receive filter (ATCRA)
    command = "221940"       # service 22 + DID
    formula = "A - 40"       # A, B, C... = bytes after the DID echo
    unit    = "°C"
    min     = -40
    max     = 150
    verified = false

Formula language: + - * / // % ** << >> & | ~, parentheses, bytes A..Z,
``b[i]`` for any byte index, and helpers ``signed(x)`` (8-bit),
``s16(hi, lo)``, ``u16(hi, lo)``, ``bit(x, n)``. The special formulas
``ascii`` and ``hex`` render the raw payload as text.
"""

from __future__ import annotations

import ast
import operator
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

USER_PROFILE_DIR = Path.home() / ".config" / "elm327obd" / "profiles"


@dataclass(frozen=True)
class ExtPid:
    name: str
    command: str
    header: str | None = None
    rx: str | None = None
    formula: str = "hex"
    unit: str = ""
    lo: float = 0.0
    hi: float = 100.0
    verified: bool = False
    notes: str = ""

    @property
    def service(self) -> int:
        return int(self.command[:2], 16)

    @property
    def echo_len(self) -> int:
        """Bytes of the positive response that echo the request (62 DD DD)."""
        return len(self.command) // 2


@dataclass
class Profile:
    key: str
    name: str
    description: str = ""
    pids: list[ExtPid] = field(default_factory=list)
    source: str = "built-in"


def _to_pid(raw: dict) -> ExtPid:
    command = str(raw["command"]).replace(" ", "").upper()
    if len(command) < 2 or len(command) % 2 or not all(c in "0123456789ABCDEF" for c in command):
        raise ValueError(f"pid {raw.get('name', '?')!r}: command must be hex bytes, got {command!r}")
    return ExtPid(
        name=raw["name"],
        command=command,
        header=raw.get("header"),
        rx=raw.get("rx"),
        formula=raw.get("formula", "hex"),
        unit=raw.get("unit", ""),
        lo=float(raw.get("min", 0)),
        hi=float(raw.get("max", 100)),
        verified=bool(raw.get("verified", False)),
        notes=raw.get("notes", ""),
    )


def _parse(key: str, text: str, source: str) -> Profile:
    data = tomllib.loads(text)
    return Profile(
        key=key,
        name=data.get("name", key.title()),
        description=data.get("description", ""),
        pids=[_to_pid(p) for p in data.get("pid", [])],
        source=source,
    )


def load_profiles(errors: list[str] | None = None) -> dict[str, Profile]:
    """Load built-in and user profiles. Broken user files are skipped and
    described in ``errors`` (if given) instead of stopping the app."""
    profiles: dict[str, Profile] = {}
    pkg = resources.files("elm327obd") / "profiles"
    for entry in sorted(pkg.iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".toml"):
            key = entry.name[:-5]
            profiles[key] = _parse(key, entry.read_text(encoding="utf-8"), "built-in")
    if USER_PROFILE_DIR.is_dir():
        for path in sorted(USER_PROFILE_DIR.glob("*.toml")):
            try:
                profiles[path.stem] = _parse(path.stem, path.read_text(encoding="utf-8"), str(path))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError,
                    TypeError, ValueError, AttributeError) as exc:
                if errors is not None:
                    errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
    return profiles


# --- Safe formula evaluation ---------------------------------------------------

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: lambda a, b: _bounded_pow(a, b), ast.LShift: lambda a, b: _bounded_lshift(a, b),
    ast.RShift: operator.rshift,
    ast.BitAnd: operator.and_, ast.BitOr: operator.or_, ast.BitXor: operator.xor,
}


# ** and << can build astronomically large ints and hang the UI; real decoding
# formulas never need results anywhere near this big.
_MAX_BITS = 256


def _bits(x) -> int:
    return abs(int(x)).bit_length() if isinstance(x, (int, float)) and abs(x) != float("inf") else 1 << 30


def _bounded_pow(a, b):
    if abs(b) > 64 or (b > 0 and _bits(a) * b > _MAX_BITS):
        raise FormulaError(f"{a} ** {b} is too large")
    return operator.pow(a, b)


def _bounded_lshift(a, b):
    if b > 64 or _bits(a) + b > _MAX_BITS:
        raise FormulaError(f"{a} << {b} is too large")
    return operator.lshift(a, b)


_UNOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Invert: operator.invert}

_FUNCS = {
    "signed": lambda x: x - 256 if x > 127 else x,
    "s16": lambda hi, lo: ((hi << 8) | lo) - 65536 if hi > 127 else (hi << 8) | lo,
    "u16": lambda hi, lo: (hi << 8) | lo,
    "bit": lambda x, n: (x >> n) & 1,
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
}


class FormulaError(ValueError):
    pass


def evaluate(formula: str, payload: bytes) -> float | str:
    """Evaluate ``formula`` against ``payload`` (bytes after the DID echo)."""
    f = formula.strip()
    if f == "hex":
        return payload.hex(" ").upper() or "(empty)"
    if f == "ascii":
        return "".join(chr(c) for c in payload if 32 <= c < 127).strip() or payload.hex(" ").upper()
    try:
        tree = ast.parse(f, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Bad formula {formula!r}: {exc.msg}") from exc
    env = {chr(ord("A") + i): payload[i] for i in range(min(26, len(payload)))}
    try:
        return _eval(tree.body, env, payload)
    except FormulaError:
        raise
    except (ArithmeticError, TypeError, ValueError, RecursionError) as exc:
        # e.g. division by zero on a 0 byte, wrong helper arity, overflow
        raise FormulaError(f"{formula!r}: {type(exc).__name__}: {exc}") from exc


def _eval(node: ast.AST, env: dict[str, int], payload: bytes):
    match node:
        case ast.Constant(value=v) if isinstance(v, (int, float)):
            return v
        case ast.Name(id=name):
            if name in env:
                return env[name]
            if len(name) == 1 and name.isupper():
                raise FormulaError(f"Byte {name} not present in {len(payload)}-byte reply")
            raise FormulaError(f"Unknown name {name!r}")
        case ast.BinOp(left=l, op=op, right=r) if type(op) in _BINOPS:
            return _BINOPS[type(op)](_eval(l, env, payload), _eval(r, env, payload))
        case ast.UnaryOp(op=op, operand=o) if type(op) in _UNOPS:
            return _UNOPS[type(op)](_eval(o, env, payload))
        case ast.Subscript(value=ast.Name(id="b"), slice=idx):
            i = _eval(idx, env, payload)
            if not isinstance(i, int) or not 0 <= i < len(payload):
                raise FormulaError(f"b[{i}] out of range for {len(payload)}-byte reply")
            return payload[i]
        case ast.Call(func=ast.Name(id=fname), args=args) if fname in _FUNCS:
            return _FUNCS[fname](*(_eval(a, env, payload) for a in args))
    raise FormulaError(f"Unsupported expression: {ast.dump(node)[:60]}")
