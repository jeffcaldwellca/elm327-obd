"""Small persisted settings (last adapter, dashboard layout, units)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from elm327obd.pids import DEFAULT_DASHBOARD

CONFIG_DIR = Path.home() / ".config" / "elm327obd"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class Config:
    host: str | None = None
    port: int | None = None
    imperial: bool = False
    dashboard: list[int] = field(default_factory=lambda: list(DEFAULT_DASHBOARD))
    profile: str = "ford"
    log_dir: str = str(Path.home() / "Documents" / "OBD Logs")

    @classmethod
    def load(cls) -> Config:
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2))
