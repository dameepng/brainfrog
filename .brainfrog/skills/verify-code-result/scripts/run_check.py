#!/usr/bin/env python3
"""Run an argv command without a shell and emit a bounded JSON result."""

import argparse
import hashlib
import json
import subprocess
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.timeout <= 0:
        parser.error("provide a command and a positive timeout")
    started = time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, timeout=args.timeout, shell=False)
        code = result.returncode
        out, err = result.stdout, result.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        code = 124
        out, err = exc.stdout or b"", exc.stderr or b""
        timed_out = True
    print(json.dumps({
        "exit_code": code,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout_bytes": len(out),
        "stderr_bytes": len(err),
        "stdout_sha256": hashlib.sha256(out).hexdigest(),
        "stderr_sha256": hashlib.sha256(err).hexdigest(),
    }, sort_keys=True))
    return code if 0 <= code <= 125 else 1


if __name__ == "__main__":
    raise SystemExit(main())
