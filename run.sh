#!/usr/bin/env bash
# Run one frontend straight from source.
#   ./run.sh            the quik TUI
#   ./run.sh cuacode    run the GUI, building it if it is stale
#   ./run.sh fastrs     the older terminal frontend
#   ./run.sh --list     the frontends in this tree
#   ./run.sh doctor     what this build is, and whether origin moved
set -euo pipefail

launch_dir="$PWD"      # where you ran this from, before either cd below
cd "$(dirname "$0")"

# The Rust frontends are the [[bin]] targets of the crates under rust/. Read
# from the manifests rather than listed here too, so adding one is a single edit.
rust_frontends() {
    # One "dir:bin" per frontend: the crate directory relative to the repo
    # root, and the binary that crate builds. Read from the manifests rather
    # than listed here, so adding a frontend is a single file.
    local m d b
    for m in rust/Cargo.toml rust/*/Cargo.toml; do
        [ -f "$m" ] || continue
        d="${m%/Cargo.toml}"
        for b in $(awk '/^\[\[bin\]\]/{f=1;next} /^\[/{f=0} f && /^name/{print $3}' "$m" | tr -d '"'); do
            echo "$d:$b"
        done
    done
}

# The crate a frontend belongs to, or nothing.
rust_bin_dir() {
    # No early `exit` in the awk: it would close the pipe while rust_frontends
    # still had a line to write, SIGPIPE on that write, and `pipefail` plus
    # `set -e` would then end the script silently before it runs anything.
    rust_frontends | awk -F: -v want="$1" '$2 == want && !seen { print $1; seen = 1 }'
}

# No argument means the TUI: the command is named after the app, so a bare
# `cuacode` has to land in a conversation rather than print a usage error. The
# list moved to --list, which is a question and exits 0.
[ $# -eq 0 ] && set -- quik

if [ "$1" = "--list" ] || [ "$1" = "list" ]; then
    echo "usage: ./run.sh <frontend> [args...]   anything else goes to the frontend"
    echo "       ./run.sh <frontend> --resume     carry on with an earlier conversation"
    echo "       ./run.sh --usage [--days N] [--json]"
    echo "frontends:"
    rust_frontends | awk -F: '$2 != "cuacode-doctor" { printf "  %s  (%s)\n", $2, $1 }'
    echo "  doctor  (rust)       the build, the tree, and how far behind origin it is"
    exit 0
fi

name="$1"
shift

# Not a frontend: a question about the app rather than a conversation with it, so it
# never starts a session of its own to answer. usage.py owns this, not the worker.
if [ "$name" = "--usage" ] || [ "$name" = "usage" ]; then
    py="${CUACODE_PYTHON:-}"
    for cand in venv/bin/python3 venv/bin/python .venv/bin/python3 .venv/bin/python; do
        [ -n "$py" ] && break
        [ -x "$cand" ] && py="$cand"
    done
    exec "${py:-python3}" -m handler.usage "$@"
fi

# Frontends locate main.py by walking up from cwd; pin it so the worker is
# always this checkout's, whatever directory you launched from.
if [ -n "${CUACODE_WORKER:-}" ] && [ "${CUACODE_WORKER:-}" != "$PWD/main.py" ]; then
    # An inherited value wins, which is right for a frontend run from a shell
    # that was itself started inside another checkout -- and a silent trap in
    # a tree whose whole point is its own Python side. Say it rather than
    # quietly driving somebody else's worker.
    echo "note: driving the worker at $CUACODE_WORKER, not this checkout's." >&2
    echo "      unset CUACODE_WORKER to use $PWD/main.py" >&2
fi
export CUACODE_WORKER="${CUACODE_WORKER:-$PWD/main.py}"

# A frontend exec'd from the tree would report the tree as the agent's cwd on
# every dev launch. Report the directory you were actually standing in instead.
export CUACODE_CWD="${CUACODE_CWD:-$launch_dir}"

# Also not a frontend: `doctor` prints what this tree is and exits, so it never
# starts a session to answer. Its small binary lives in the shared crate, which
# is also what Windows' bin\cuacode.cmd runs, so both platforms answer from the
# same implementation without needing the GUI. `cuacode doctor --upgrade` is
# the same command acting on the answer.
# --- vendored trees -----------------------------------------------------------
# rust/zeron is in .gitignore: it is vendored rather than tracked, and rust/gui
# links it by path. Cargo resolves the whole workspace before it builds anything,
# so without it every `cargo build -p ...` fails -- including doctor's own, which
# is the one command that would have explained why. rust/zeron.toml is the pin,
# read here and by doctor.rs so the two cannot disagree.
zeron_pin() {
    [ -f rust/zeron.toml ] || return 0
    sed -n 's/^[[:space:]]*\(repo\|rev\)[[:space:]]*=[[:space:]]*"\{0,1\}\([^" ]*\)"\{0,1\}.*/\1=\2/p' rust/zeron.toml
}

zeron_field() {
    zeron_pin | sed -n "s/^$1=//p" | head -n 1
}

ensure_vendored() {
    [ -f rust/zeron.toml ] || return 0
    local repo rev tree at stale
    repo="$(zeron_field repo)"
    rev="$(zeron_field rev)"
    [ -n "$repo" ] && [ -n "$rev" ] || return 0
    tree="rust/zeron"
    if [ -f "$tree/crates/ui/Cargo.toml" ]; then
        at="$(git -C "$tree" describe --tags --always 2>/dev/null || true)"
        [ "$at" = "$rev" ] && return 0
        # Renamed, not checked out: a half-written tree from an interrupted
        # clone is not something to guess at, and no one's work lives here.
        stale="rust/zeron.stale"
        rm -rf "$stale"
        mv "$tree" "$stale"
        echo "the vendored zeron tree was $at, fetching $rev" >&2
    fi
    echo "fetching the vendored zeron tree ($rev) -- once, it is not in the repository" >&2
    git -C rust clone --quiet --depth 1 --branch "$rev" -- "$repo" zeron
}

# An upgrade replaces a frontend by renaming the running image aside, because a
# running binary cannot be rewritten. Windows also refuses to delete such a copy
# while its process holds it, which is what makes this safe: on Unix there is no
# such copy, and on Windows the live one survives and the stale ones go.
clear_stale_images() {
    # .running- is the name an earlier build used for the same copy.
    rm -f rust/target/release/*.exe.old-* rust/target/release/*.exe.running-* 2>/dev/null || true
}

if [ "$name" = "doctor" ]; then
    ensure_vendored
    clear_stale_images
    bin="rust/target/release/cuacode-doctor"
    [ "${OS:-}" = "Windows_NT" ] && bin="$bin.exe"
    if [ ! -x "$bin" ] || [ -n "$(find rust/src rust/Cargo.toml -newer "$bin" -print -quit)" ]; then
        (cd rust && cargo build --release --bin cuacode-doctor) >&2
    fi
    exec "$bin" "$@"
fi

# Tripwire for a fresh clone: the worker's dependencies must exist before any
# frontend is started, and a missing install should announce itself at the
# front door instead of as a stack trace from inside the worker. One import
# checks, install.sh is the only thing that fixes it, and the sentinel keeps
# the check off every later launch (touching requirements.txt brings it back).
# Doctor is above this on purpose: it diagnoses a broken worker rather than
# needing one.
sentinel="bin/.deps-ok"
if [ ! -f "$sentinel" ] || [ requirements.txt -nt "$sentinel" ]; then
    if ! "${CUACODE_PYTHON:-python3}" -c 'import openai' >/dev/null 2>&1; then
        ./install.sh || exit 1
    fi
    mkdir -p bin && touch "$sentinel"
fi

crate="$(rust_bin_dir "$name")"
if [ -n "$crate" ]; then
    # Built on demand and exec'd from cargo's own output tree, so there is no
    # second copy in bin/ to go stale. Rebuilt only when the crate moved past
    # the binary: a cargo run with nothing to do still prints a line, and that
    # line lands on top of a fullscreen UI.
    bin="rust/target/release/$name"
    if [ ! -x "$bin" ] || [ -n "$(find "$crate/src" "$crate/Cargo.toml" -newer "$bin" -print -quit)" ]; then
        (cd "$crate" && cargo build --release) >&2
    fi
    exec "$bin" "$@"
fi

echo "error: no such frontend: $name" >&2
echo "       ./run.sh with no argument lists the ones in rust/" >&2
exit 1
