#!/usr/bin/env bash
# Backwards-compatible entry point; setup-script.sh is the canonical bootstrap.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/../setup-script.sh" "$@"
