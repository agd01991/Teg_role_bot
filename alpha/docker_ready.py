"""Bounded Docker readiness check; never render configuration or connection secrets."""

import subprocess
import sys

try:
    result = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Docker unavailable")
    print("Docker engine ready.")
except (OSError, RuntimeError, subprocess.TimeoutExpired):
    print(
        "Docker engine is not ready. Open Docker Desktop and wait for Linux engine startup."
    )
    sys.exit(1)
