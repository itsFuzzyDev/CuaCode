#!/usr/bin/env bash
# Build every frontend in go/frontends/ into bin/.
#   ./build.sh                build all
#   ./build.sh classic        build one
#   ./build.sh --keep-going   build all, report failures at the end instead of
#                             stopping at the first (gio needs a C toolchain and
#                             platform headers; the terminal frontends do not)
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v go &> /dev/null; then
    echo "error: go is not installed or not in PATH" >&2
    echo "       get it from https://go.dev/dl/" >&2
    exit 1
fi

mkdir -p bin

keep_going=0
args=()
for arg in "$@"; do
    if [ "$arg" = "--keep-going" ]; then keep_going=1; else args+=("$arg"); fi
done

targets=("${args[@]+"${args[@]}"}")
if [ ${#targets[@]} -eq 0 ]; then
    for d in go/frontends/*/; do
        [ -d "$d" ] || continue
        targets+=("$(basename "$d")")
    done
fi

if [ ${#targets[@]} -eq 0 ]; then
    echo "no frontends in go/frontends/" >&2
    exit 1
fi

exe=""
[ "${OS:-}" = "Windows_NT" ] && exe=".exe"

failed=()
for name in "${targets[@]}"; do
    if [ ! -d "go/frontends/$name" ]; then
        echo "error: no such frontend: $name" >&2
        exit 1
    fi
    if (cd go && go build -o "../bin/$name$exe" "./frontends/$name"); then
        echo "built: bin/$name$exe"
    elif [ "$keep_going" -eq 1 ]; then
        failed+=("$name")
    else
        exit 1
    fi
done

if [ ${#failed[@]} -gt 0 ]; then
    echo "did not build: ${failed[*]}" >&2
    exit 1
fi
