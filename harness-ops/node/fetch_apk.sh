#!/bin/sh
# Legacy entry: SOURCE SIZE SERIAL SHA256, with explicit --cache-root.
exec python3 "$(dirname "$0")/fetch_apk.py" "$@"
