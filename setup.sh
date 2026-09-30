#!/usr/bin/env bash
# One-shot setup: virtualenv, dependencies, optional tests, and an `obd`
# command on your PATH.
#
#   ./setup.sh            # install + link ~/.local/bin/obd
#   ./setup.sh --test     # also run the test suite
#   ./setup.sh --no-link  # don't touch ~/.local/bin
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
RUN_TESTS=0
LINK=1
for arg in "$@"; do
  case "$arg" in
    --test) RUN_TESTS=1 ;;
    --no-link) LINK=0 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

OS="$(uname -s)"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

# 1. Python 3.11+ (tomllib, match statements)
PY=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$PY" ]]; then
  echo "Python 3.11 or newer is required." >&2
  if [[ "$OS" == "Darwin" ]]; then
    echo "  Install it with:  brew install python" >&2
  else
    echo "  Debian/Ubuntu:  sudo apt install python3.11 python3.11-venv  (Ubuntu 22.04: via the deadsnakes PPA)" >&2
    echo "  Fedora/RHEL:    sudo dnf install python3.11" >&2
    echo "  Any distro:     https://docs.astral.sh/uv/  then  uv python install 3.12" >&2
  fi
  exit 1
fi
if ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
  ver="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  echo "$PY is missing the venv module. On Debian/Ubuntu:  sudo apt install python${ver}-venv" >&2
  exit 1
fi
say "Using $("$PY" --version) at $PY"

# 2. Virtualenv + package (editable, with test deps)
if [[ ! -x .venv/bin/python ]]; then
  say "Creating virtualenv in .venv"
  "$PY" -m venv .venv
fi
say "Installing elm327obd and dependencies"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[dev]"

# 3. Put `obd` on PATH
if [[ $LINK -eq 1 ]]; then
  mkdir -p "$HOME/.local/bin"
  ln -sf "$ROOT/.venv/bin/obd" "$HOME/.local/bin/obd"
  say "Linked $HOME/.local/bin/obd"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) case "${SHELL:-}" in
         */zsh) rc="~/.zshrc" ;;
         */bash) rc="~/.bashrc" ;;
         *) rc="your shell profile" ;;
       esac
       warn "~/.local/bin is not on your PATH. Add this to $rc:"
       echo '    export PATH="$HOME/.local/bin:$PATH"' ;;
  esac
fi

# 4. Optional test run (after linking, so a failing test doesn't block the install)
if [[ $RUN_TESTS -eq 1 ]]; then
  say "Running tests (about a minute)"
  if ! .venv/bin/python -m pytest -q; then
    warn "Some tests failed (see above). The app is installed; please report the failures."
  fi
fi

cat <<'EOF'

Done. Next steps:
  • Try it without a car:   obd --demo
  • With the car: ignition ON, join the adapter's Wi-Fi (e.g. WiFi_OBDII), then run:  obd
EOF
if [[ "$OS" == "Darwin" ]]; then
  cat <<'EOF'
  • If it can't reach the adapter, allow your terminal app under
    System Settings → Privacy & Security → Local Network, then restart the terminal.
EOF
fi
