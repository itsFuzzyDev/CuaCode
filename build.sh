#!/usr/bin/env bash
set -euo pipefail

if ! command -v go &> /dev/null; then
    echo "error: go is not installed or not in PATH" >&2
    echo "       get it from https://go.dev/dl/" >&2
    exit 1
fi

cd "$(dirname "$0")/tui"
go build .
echo "built: tui/tui"
