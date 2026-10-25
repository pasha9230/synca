#!/usr/bin/env bash
# Build the agent for the server (Linux x86_64, static musl) on the Mac: the server (2 cores, 1 GB) never compiles.
# Needs once: brew install zig; cargo install --locked cargo-zigbuild; rustup target add x86_64-unknown-linux-musl
set -euo pipefail
export PATH=/opt/homebrew/opt/rustup/bin:$HOME/.cargo/bin:$PATH
cd "$(dirname "$0")"
cargo zigbuild --release --locked --target x86_64-unknown-linux-musl
bin=target/x86_64-unknown-linux-musl/release/tire-photo-agent
file "$bin"
shasum -a 256 "$bin"
