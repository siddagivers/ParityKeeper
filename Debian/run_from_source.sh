#!/bin/sh
cd "$(dirname "$0")/src" || exit 1
exec python3 gui.py "$@"
