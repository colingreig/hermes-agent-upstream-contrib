#!/bin/bash
# Apple Python is intentionally used only for the stdlib selector. The selector
# chooses a validated, release-absolute PyYAML interpreter without consulting
# runtime-current, so pointer corruption cannot suppress governed verification.
exec /usr/bin/python3 -S "$(dirname "$0")/verify_governed_paths_launcher.py" "$@"
