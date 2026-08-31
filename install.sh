#!/usr/bin/env bash
# One command from a fresh clone to something you can run: interpreter, venv,
# dependencies, binaries.
#
#   ./install.sh               everything
#   ./install.sh --no-build    python side only (skip Go)
#   ./install.sh --no-venv     install deps into the interpreter as found
set -euo pipefail
cd "$(dirname "$0")"

MIN_PY="3.10"          # `X | None` annotations are evaluated at def time
VENV="venv"
build=1
use_venv=1

for arg in "$@"; do
    case "$arg" in
        --no-build) build=0 ;;
        --no-venv)  use_venv=0 ;;
        -h|--help)
            sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "error: unknown option: $arg" >&2; exit 1 ;;
    esac
done

say()  { printf '\033[1m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$1" >&2; exit 1; }

# --- python ------------------------------------------------------------------
# Version checked by asking the interpreter, not by parsing `--version`: the
# name says nothing about the version behind it, and `python3` is a different
# build on most machines than the one somebody expects.
py_ok() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

python=""
for cand in "${CUACODE_PYTHON:-}" python3 python; do
    [ -n "$cand" ] || continue
    command -v "$cand" >/dev/null 2>&1 || continue
    if py_ok "$cand"; then python="$(command -v "$cand")"; break; fi
done
[ -n "$python" ] || die "no python >= $MIN_PY found (looked for python3, python; set CUACODE_PYTHON to override)"
say "python: $python ($("$python" -c 'import platform; print(platform.python_version())'))"

# --- venv --------------------------------------------------------------------
if [ "$use_venv" -eq 1 ]; then
    if [ ! -x "$VENV/bin/python3" ] && [ ! -x "$VENV/bin/python" ]; then
        say "creating venv at ./$VENV"
        "$python" -m venv "$VENV" \
            || die "venv creation failed (on debian/ubuntu: apt install python3-venv)"
    else
        say "venv already at ./$VENV, reusing it"
    fi
    python="$PWD/$VENV/bin/python3"
    [ -x "$python" ] || python="$PWD/$VENV/bin/python"
fi

# --- dependencies ------------------------------------------------------------
# Through `python -m pip` rather than the pip script: the shebang in a copied
# or moved venv points at an interpreter that may not be there any more.
say "installing dependencies"
"$python" -m pip install --upgrade pip >/dev/null 2>&1 || warn "could not upgrade pip, carrying on"
"$python" -m pip install -r requirements.txt \
    || die "pip install failed -- see the output above"

# The worker imports these at boot, and until it can, a frontend shows nothing
# at all. Better to say so here than to leave that for the first launch.
say "checking the worker imports"
"$python" -c 'import ollama, openai, anthropic, mss, PIL, yaml, httpx' \
    || die "dependencies installed but do not import -- try deleting ./$VENV and running this again"

# --- frontends ---------------------------------------------------------------
if [ "$build" -eq 1 ]; then
    if command -v go >/dev/null 2>&1; then
        say "building frontends"
        # Keep going: gio needs a C toolchain and X11/Wayland headers, and its
        # absence is not a reason to leave somebody with no terminal frontend.
        ./build.sh --keep-going || warn "some frontends did not build (see above)"
    else
        warn "go not found, skipping the build -- get it from https://go.dev/dl/"
        warn "you can still run from source once go is installed: ./run.sh deck"
        build=0
    fi
fi

# --- done --------------------------------------------------------------------
echo
say "done"
echo "  run a frontend:   ./bin/deck          (or ./run.sh deck, straight from source)"
echo "  list frontends:   ./run.sh"
echo "  api keys/models:  ~/.cuacode/config.json, written on first launch"
[ "$build" -eq 1 ] || echo "  build later:      ./build.sh"
