# elm327obd — OBD·TUI

Terminal OBD-II diagnostics for ELM327 **WiFi** adapters, built for macOS.
Live dashboard, trouble codes, readiness monitors, freeze frame, VIN/calibration
info, manufacturer (Mode 22) PIDs, CSV logging and a raw command console.

```
 ◉ OBD·TUI   ● ONLINE  192.168.0.10:35000  ELM327 v1.5  ISO 15765-4 CAN (11 bit, 500 kbaud)  ⚡14.1V  ↻ 12.6 Hz
 ① Dashboard  ② Live data  ③ Trouble codes  ④ Vehicle  ⑤ Extended (Mode 22)  ⑥ Console
 ╭─ Engine RPM ───────────────╮ ╭─ Vehicle speed ────────────╮ ╭─ Coolant temperature ──────╮
 │ ╺━┓╺┓ ╺━┓┏━┓               │ │ ┏━╸╺┓  ┏━┓                 │ │ ┏━┓╺┓  ╻ ╻                 │
 │  ━┫ ┃ ┏━┛┣━┫               │ │ ┗━┓ ┃  ┃ ┃                 │ │ ┗━┫ ┃  ┗━┫                 │
 │ ╺━┛╺┻╸┗━╸┗━┛ rpm           │ │ ╺━┛╺┻╸•┗━┛ km/h            │ │ ╺━┛╺┻╸•  ╹ °C              │
 │ ━━━━━━━━━━━━━━━━━━━━━━━━   │ │ ━━━━━━━━━━━━━━━━━━━━━━━    │ │ ━━━━━━━━━━━━━━━━━━━━━━━━   │
 │ ▁▁▂▂▃▃▄▄▅▅▆▆▇▇███████████  │ │ ▁▁▂▂▃▃▄▄▅▅▆▆▇▇██████████   │ │ ▁▁▄▄▄▄▄▄█▄█▄▄▄██████████   │
 ╰────────────── ▼819 ▲3,884 ─╯ ╰──────────────── ▼1.9 ▲67 ─╯ ╰────────────── ▼66 ▲91.4 ─╯
```

## Install

```bash
cd ~/Development/elm327-obd
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

Then run `.venv/bin/obd` (or `source .venv/bin/activate` and just `obd`).

## Connect to the adapter

1. Plug the adapter into the OBD port (under the dash, driver side). Turn the ignition **ON** —
   engine running or not.
2. On the Mac, join the adapter's Wi-Fi network (commonly `WiFi_OBDII`, `V-LINK`, `OBDII`,
   password none or `12345678`). The Mac will have no internet while joined — that's normal.
3. Run `obd`. It scans the usual addresses (`192.168.0.10:35000` is most common) and connects
   automatically when it finds exactly one adapter. The last good address is remembered.

No adapter handy? `obd --demo` runs the whole app against a built-in simulated vehicle.

### macOS Local Network permission

On recent macOS, the terminal app needs **Local Network** access to reach the adapter.
If connection fails with "No route to host" / timeouts while you're definitely on the adapter's
Wi-Fi: *System Settings → Privacy & Security → Local Network* → enable Terminal / iTerm / Ghostty
(whichever you use), then restart it.

## Commands

| Command | What it does |
|---|---|
| `obd` | Full-screen TUI (scan → connect) |
| `obd -a 192.168.0.10:35000` | TUI, connect directly |
| `obd --demo` | TUI against the built-in emulator |
| `obd --imperial` | Start in °F / mph / psi (toggle anytime with `u`) |
| `obd scan` | List adapters found on the current network |
| `obd info [-a HOST:PORT]` | Print VIN, modules, calibration IDs, readiness (no TUI) |
| `obd codes [--clear]` | Print trouble codes; optionally clear (asks first) |
| `obd emulator [--port 35000] [--ignition-off]` | Run a fake ELM327 for other tools / testing |

## Keys

| Key | Action |
|---|---|
| `1`–`6` | Switch tabs (press `esc` first if you're typing in the console) |
| `e` | Choose dashboard gauges |
| `r` / `f` / `x` | Read codes / freeze frame / clear codes (clear always asks for confirmation) |
| `u` | Toggle metric ↔ imperial |
| `l` | Start/stop CSV recording of dashboard PIDs (→ `~/Documents/OBD Logs/`) |
| `m` | Reset min/max markers |
| `ctrl+r` | Retry vehicle detection / reconnect |
| `ctrl+p` | Command palette |
| `q` | Quit |

## Tabs

- **Dashboard**: big-digit gauges with colored level bars, sparklines and session min/max.
  The grid reflows to the terminal width. Gauges dim when their data goes stale.
- **Live data**: every supported Mode 01 PID with value, min/max and an inline trend.
  Requests are batched (up to 6 PIDs per CAN request), which usually gives 10+ Hz over Wi-Fi.
- **Trouble codes**: stored, pending and permanent codes from **every** module, with the
  check-engine light (MIL) status, descriptions and a freeze-frame snapshot.
- **Vehicle**: VIN, protocol, modules found, calibration IDs/CVNs, odometer (where supported)
  and the readiness monitors — useful before an emissions inspection.
- **Extended (Mode 22)**: per-make profiles (Ford, GM, Hyundai/Kia) plus a read-only
  **DID scanner** to discover what identifiers a module answers.
- **Console**: send raw AT/OBD commands and see decoded replies (`010C` → "Engine RPM = 797 rpm").
  `:monitor` shows the poller's traffic. Commands that write to, actuate, reset or reflash a
  module (services 04, 08, 10, 11, 27, 28, 2C, 2E, 2F, 30, 31, 34–37, 3B, 3D, 85, 87) and `ATPP`
  need confirmation. Leaving the console restores the adapter's settings (headers, filters,
  formatting) so the live poller keeps working even after you've experimented.

## Manufacturer profiles

Built-ins live in `src/elm327obd/profiles/*.toml`. Add or override your own in
`~/.config/elm327obd/profiles/<name>.toml`:

```toml
name = "My truck"

[[pid]]
name     = "Transmission fluid temp"
header   = "7E2"        # physical request address (ATSH)
rx       = "7EA"        # optional CAN receive filter (ATCRA)
command  = "221940"     # service 22 + DID
formula  = "A - 40"     # A,B,C… = bytes after the DID echo; b[i]; signed(), s16(), u16(), bit()
unit     = "°C"
min      = -40
max      = 150
verified = true
```

`formula = "ascii"` or `"hex"` shows the raw payload. Entries marked **unverified** in the
built-ins are community-sourced values, so check them against a known reading before you trust them.
The standard UDS identifiers (`F190` VIN, `F188`/`F195` software numbers) work on most 2010+
modules that support service 22.

Custom DTC descriptions (e.g. manufacturer P1xxx codes) go in `~/.config/elm327obd/dtc.csv`
(`code,description` per line).

## What an ELM327 can't do: tuning

This tool reads data, reads/clears codes and sends raw diagnostic requests. **It does not
reflash or tune ECUs, and an ELM327 isn't suited to that job.** Flashing needs a reliable
high-throughput interface (J2534 / vendor tools), the manufacturer's security-access
algorithms, and correct calibration files. A Wi-Fi dropout halfway through a flash can leave
the module unusable. For tuning, use a platform-specific tool: HP Tuners (GM/Ford),
Cobb/SCT (Ford), or the manufacturer's service software. FORScan (Ford) can also change
module configuration with a suitable adapter.

## Troubleshooting

- **"Adapter only – vehicle not responding"**: the ignition is off, or the car's OBD port
  isn't powered. Turn the key to ON and press `ctrl+r`.
- **Slow or erratic data**: many cheap clones are fake "v1.5"/"v2.1" chips. If batched requests
  fail, the app falls back to single-PID mode automatically (shown on the Vehicle tab).
- **Mode 22 "no reply"**: wrong header or receive address for that module. Try the DID scanner
  on `7E0`, `7E1`, `7E2`, …
- **Other adapter address**: `obd -a HOST:PORT`. In macOS Wi-Fi details, the router field is
  usually the adapter's IP.

## Development

```bash
.venv/bin/pytest            # protocol, decoders, client-vs-emulator, TUI + regression tests
.venv/bin/obd emulator      # fake adapter on 127.0.0.1:35000
```

Layout: `transport.py` (TCP + prompt handling) → `protocol.py` (frame / ISO-TP parsing) →
`elm.py` (client: Mode 01/02/03/04/07/09/0A/22) → `tui/` (Textual app). `pids.py`, `dtc.py`
and `profiles.py` hold the decoders.
