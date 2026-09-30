"""CSV data logging of live PID values."""

from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path
from typing import TextIO

from elm327obd.pids import PIDS, Value


class DataLogger:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser()
        self.path: Path | None = None
        self._fh: TextIO | None = None
        self._writer = None
        self._columns: list[int] = []
        self._t0 = 0.0
        self.rows = 0

    @property
    def active(self) -> bool:
        return self._fh is not None

    def start(self, pids: list[int]) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"obd-{datetime.now():%Y%m%d-%H%M%S}.csv"
        self._fh = self.path.open("w", newline="")
        self._writer = csv.writer(self._fh)
        self._columns = list(pids)
        header = ["time", "elapsed_s"] + [
            f"{PIDS[p].name} ({PIDS[p].unit})" if p in PIDS else f"PID {p:02X}" for p in pids
        ]
        self._writer.writerow(header)
        self._t0 = time.monotonic()
        self.rows = 0
        return self.path

    def write(self, values: dict[int, Value]) -> None:
        if not self._writer or not any(p in values for p in self._columns):
            return
        row = [datetime.now().isoformat(timespec="milliseconds"), f"{time.monotonic() - self._t0:.3f}"]
        row += [_cell(values.get(p, "")) for p in self._columns]
        self._writer.writerow(row)
        self.rows += 1
        if self.rows % 20 == 0 and self._fh:
            self._fh.flush()

    def stop(self) -> Path | None:
        if self._fh:
            self._fh.close()
        self._fh = self._writer = None
        return self.path


def _cell(value: Value | str) -> Value | str:
    return round(value, 3) if isinstance(value, float) else value
