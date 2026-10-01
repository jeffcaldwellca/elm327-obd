"""Export the trouble-code guidance to docs/dtc.json for the GitHub Pages code lookup.

Run after changing dtc_guide.toml, the built-in names or the python-OBD table:

    .venv/bin/python tools/build_site_data.py

User overrides in ~/.config/elm327obd are ignored so the output is reproducible.
"""

import json
from pathlib import Path

from elm327obd import dtc
from elm327obd.pids import PIDS

OUT = Path(__file__).resolve().parent.parent / "docs" / "dtc.json"


def main() -> None:
    dtc.USER_DTC_FILE = dtc.USER_GUIDE_FILE = Path("/nonexistent")
    codes = sorted(set(dtc.GENERIC) | set(dtc._load_pyobd_codes()) | set(dtc._load_guide()))

    # Guide entries are shared by many codes (ranges like P0301-P0308), so store each
    # distinct guidance block once and point codes at it by index.
    blocks: list[dict] = []
    index: dict[str, int] = {}
    table: dict[str, list] = {}
    for code in codes:
        g = dtc.guide(code)
        block = {"s": g.severity, "m": g.summary}
        if g.causes:
            block["c"] = list(g.causes)
        if g.checks:
            block["k"] = list(g.checks)
        if g.watch:
            block["w"] = [PIDS[p].name for p in g.watch]
        key = json.dumps(block, sort_keys=True)
        if key not in index:
            index[key] = len(blocks)
            blocks.append(block)
        table[code] = [g.name, index[key], 1 if g.source == "estimated" else 0]

    OUT.write_text(json.dumps({"blocks": blocks, "codes": table}, separators=(",", ":"), ensure_ascii=False),
                   encoding="utf-8")
    print(f"{OUT}: {len(table)} codes, {len(blocks)} guidance blocks, {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
