#!/usr/bin/env fish
# Fish launcher for the canonical Bash setup implementation.
set -l script_dir (dirname (realpath (status --current-filename)))
if not command -sq bash
    echo "ERROR: bash is required to run setup-script.sh" >&2
    exit 127
end
exec bash "$script_dir/setup-script.sh" $argv
