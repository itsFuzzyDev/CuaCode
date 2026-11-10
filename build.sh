#!/usr/bin/env bash
# Build every frontend into rust/target/release/.
#   ./build.sh                build all
#   ./build.sh fastrs         build one frontend
#   ./build.sh --keep-going   build all, report failures at the end instead of
#                             stopping at the first
set -euo pipefail

cd "$(dirname "$0")"

# Frontends are the [[bin]] targets of the crates under rust/.
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

mkdir -p bin

# rust/zeron is vendored, not tracked (`.gitignore`), and rust/gui links it by
# path. Cargo resolves the whole workspace before building anything, so a missing
# tree stops every crate, not just the GUI. Fetch it before the first build.
ensure_vendored() {
    [ -f rust/zeron.toml ] || return 0
    local repo rev tree at stale
    repo="$(sed -n 's/^[[:space:]]*repo[[:space:]]*=[[:space:]]*"\{0,1\}\([^" ]*\)"\{0,1\}.*/\1/p' rust/zeron.toml | head -n 1)"
    rev="$(sed -n 's/^[[:space:]]*rev[[:space:]]*=[[:space:]]*"\{0,1\}\([^" ]*\)"\{0,1\}.*/\1/p' rust/zeron.toml | head -n 1)"
    [ -n "$repo" ] && [ -n "$rev" ] || return 0
    tree="rust/zeron"
    if [ -f "$tree/crates/ui/Cargo.toml" ]; then
        at="$(git -C "$tree" describe --tags --always 2>/dev/null || true)"
        [ "$at" = "$rev" ] && return 0
        stale="rust/zeron.stale"
        rm -rf "$stale"
        mv "$tree" "$stale"
        echo "the vendored zeron tree was $at, fetching $rev" >&2
    fi
    echo "fetching the vendored zeron tree ($rev) -- once, it is not in the repository" >&2
    git -C rust clone --quiet --depth 1 --branch "$rev" -- "$repo" zeron
}

ensure_vendored

keep_going=0
args=()
for arg in "$@"; do
    if [ "$arg" = "--keep-going" ]; then keep_going=1; else args+=("$arg"); fi
done

targets=("${args[@]+"${args[@]}"}")
if [ ${#targets[@]} -eq 0 ]; then
    # Names, not "dir:bin": the loop below resolves a name back to its crate.
    for pair in $(rust_frontends); do
        targets+=("${pair#*:}")
    done
fi

if [ ${#targets[@]} -eq 0 ]; then
    echo "no frontends declared in rust/Cargo.toml" >&2
    exit 1
fi

exe=""
[ "${OS:-}" = "Windows_NT" ] && exe=".exe"

failed=()
for name in "${targets[@]}"; do
    if [ -n "$(rust_bin_dir "$name")" ]; then
        # Built from inside the crate: cargo walks up to the workspace on its
        # own, so the artifact lands in the one shared target directory and
        # run.sh has a single path to exec. `--bin` from the workspace root
        # would not find it: the binary belongs to a member, not to the root.
        crate="$(rust_bin_dir "$name")"
        if (cd "$crate" && cargo build --release); then
            echo "built: rust/target/release/$name$exe"
        elif [ "$keep_going" -eq 1 ]; then
            failed+=("$name")
        else
            exit 1
        fi
        continue
    fi
    echo "error: no such frontend: $name" >&2
    exit 1
done

if [ ${#failed[@]} -gt 0 ]; then
    echo "did not build: ${failed[*]}" >&2
    exit 1
fi
