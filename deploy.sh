#!/usr/bin/env bash
# Customer entry point; keep one implementation of the resumable installer.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/setup.sh" "$@"
