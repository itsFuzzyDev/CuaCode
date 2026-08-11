#!/usr/bin/env bash
# Run one frontend straight from source.
#   ./run.sh            list frontends
#   ./run.sh classic    run go/frontends/classic
set -euo pipefail

launch_dir="$PWD"      # where you ran this from, before either cd below
cd "$(dirname "$0")"

if [ $# -eq 0 ]; then
    echo "usage: ./run.sh <frontend>"
    echo "       ./run.sh --usage [--days N] [--json]"
    echo "frontends:"
    for d in go/frontends/*/; do
        [ -d "$d" ] && echo "  $(basename "$d")"
    done
    exit 1
fi

name="$1"
shift

# Not a frontend: a question about the app rather than a conversation with it.
# The worker prints what every session has cost and exits, so it never starts a
# session of its own to answer.
if [ "$name" = "--usage" ] || [ "$name" = "usage" ]; then
    py="${CUACODE_PYTHON:-}"
    for cand in venv/bin/python3 venv/bin/python .venv/bin/python3 .venv/bin/python; do
        [ -n "$py" ] && break
        [ -x "$cand" ] && py="$cand"
    done
    exec "${py:-python3}" main.py --usage "$@"
fi

if [ ! -d "go/frontends/$name" ]; then
    echo "error: no such frontend: $name" >&2
    exit 1
fi

# Frontends locate main.py by walking up from cwd; pin it so the worker is
# always this checkout's, whatever directory you launched from.
export CUACODE_WORKER="${CUACODE_WORKER:-$PWD/main.py}"

# `go run` executes the binary from the module directory, so the frontend's own
# cwd is go/ and the agent's shell would start there on every dev launch. Report
# the directory you were actually standing in instead.
export CUACODE_CWD="${CUACODE_CWD:-$launch_dir}"

cd go
exec go run "./frontends/$name" "$@"
