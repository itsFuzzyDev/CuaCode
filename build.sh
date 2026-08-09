#!/usr/bin/env bash
# Build every frontend in go/frontends/ into bin/.
#   ./build.sh          build all
#   ./build.sh classic  build one
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v go &> /dev/null; then
    echo "error: go is not installed or not in PATH" >&2
    echo "       get it from https://go.dev/dl/" >&2
    exit 1
fi

mkdir -p bin

targets=("$@")
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

for name in "${targets[@]}"; do
    if [ ! -d "go/frontends/$name" ]; then
        echo "error: no such frontend: $name" >&2
        exit 1
    fi
    (cd go && go build -o "../bin/$name" "./frontends/$name")
    echo "built: bin/$name"
done
