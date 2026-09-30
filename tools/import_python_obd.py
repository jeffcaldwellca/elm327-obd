"""Regenerate src/elm327obd/data/python_obd_dtc.tsv from a python-OBD sdist.

    pip download obd==0.7.3 --no-deps --no-binary :all: -d /tmp/pyobd
    tar xzf /tmp/pyobd/obd-0.7.3.tar.gz -C /tmp/pyobd
    python tools/import_python_obd.py /tmp/pyobd/obd-0.7.3

The DTC table is read with ast.literal_eval (python-OBD's code is never
imported or executed). Its license (GPL-2.0) is copied alongside the data.
"""

from __future__ import annotations

import ast
import re
import shutil
import sys
from pathlib import Path

# OCR-style errors in the upstream strings, fixed on import (GPL §2a: noted in the file header).
FIXES = {r"\blntake\b": "Intake"}

OUT_DIR = Path(__file__).resolve().parents[1] / "src" / "elm327obd" / "data"


def main(sdist: Path) -> None:
    tree = ast.parse((sdist / "obd" / "codes.py").read_text(encoding="utf-8"))
    table = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "DTC"
    )
    version = re.search(r"^Version:\s*(\S+)", (sdist / "PKG-INFO").read_text(), re.M)
    version = version.group(1) if version else "unknown"

    fixed = 0
    rows = []
    for code in sorted(table):
        if not re.fullmatch(r"[PCBU][0-3][0-9A-F]{3}", code):
            raise SystemExit(f"unexpected code key {code!r}")
        text = table[code].strip()
        for pattern, repl in FIXES.items():
            text, n = re.subn(pattern, repl, text)
            fixed += n
        if "\t" in text or "\n" in text:
            raise SystemExit(f"unexpected whitespace in {code}")
        rows.append(f"{code}\t{text}")

    header = [
        f"# DTC descriptions from python-OBD {version} (obd/codes.py).",
        "# Copyright 2004 Donour Sizemore, 2009 Secons Ltd., 2009 Peter J. Creath,",
        "# 2016 Brendan Whitfield. https://github.com/brendan-w/python-OBD",
        "# Licensed under the GNU General Public License v2 or later; see LICENSE.python-OBD.",
        f"# Modified by elm327obd: converted to TSV; fixed {fixed} OCR typos ('lntake' -> 'Intake').",
        "# Regenerate with tools/import_python_obd.py. Format: CODE<TAB>DESCRIPTION",
    ]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "python_obd_dtc.tsv").write_text("\n".join(header + rows) + "\n", encoding="utf-8")
    shutil.copyfile(sdist / "LICENSE", OUT_DIR / "LICENSE.python-OBD")
    print(f"wrote {len(rows)} codes ({fixed} typo fixes) to {OUT_DIR}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]))
