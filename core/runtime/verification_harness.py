"""BrainFrog-controlled verification harness (stdlib only, runs inside the sandbox).

This file is executed as a standalone script, either inside an OCI container
(mounted read-only at /bf-harness) or on the host in TRUSTED_OPERATOR mode. It must
not import anything from the BrainFrog package.

Responsibilities:
1. Copy the materialized source snapshot into an ephemeral workspace while hashing the
   exact bytes it copies (so the host can confirm which snapshot the checks ran on).
2. Run a supported check (unittest or Python compilation) and emit a structured result
   record instead of relying on human-readable runner text.

Result protocol:
    The host supplies a one-time nonce (via stdin or a file that is deleted on read).
    The harness writes exactly two records to the original stdout file descriptor:
        @@BF_HARNESS@@ {"event": "started", "nonce": ...}
        @@BF_HARNESS@@ {"event": "complete", "nonce": ..., ...counts...}
    Records whose nonce does not match are treated by the host as forged.

Limitation (documented in phase1c-finalization-report.md): repository code executing in
the same interpreter can, in principle, introspect harness memory to recover the nonce.
The protocol defeats static forgery (printing fake "Ran N tests / OK" text) and accidental
early exits; it is not a cryptographic attestation against fully adversarial in-process code.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys

RECORD_PREFIX = "@@BF_HARNESS@@ "

_result_fd = None
_nonce = ""


def _emit(event, **payload):
    record = {"event": event, "nonce": _nonce}
    record.update(payload)
    line = "\n" + RECORD_PREFIX + json.dumps(record, sort_keys=True) + "\n"
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os.write(_result_fd, line.encode("utf-8"))


def _snapshot_digest_and_copy(source, workspace):
    """Copy every regular file from source to workspace; return (digest, file_count).

    The digest algorithm mirrors core.runtime.sandbox.materialize_source_snapshot.
    """
    entries = []
    for root, dirs, files in os.walk(source, followlinks=False):
        dirs.sort()
        for name in files:
            full = os.path.join(root, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, source).replace(os.sep, "/")
            entries.append((rel, full))
    entries.sort(key=lambda e: e[0])

    hasher = hashlib.sha256()
    for rel, full in entries:
        with open(full, "rb") as fh:
            data = fh.read()
        hasher.update(f"{rel}:{len(data)}:".encode("utf-8"))
        hasher.update(hashlib.sha256(data).digest())
        if workspace is not None:
            dst = os.path.join(workspace, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as out:
                out.write(data)
    return hasher.hexdigest(), len(entries)


def _run_unittest(args):
    import unittest

    class _RecordingProgram(unittest.TestProgram):
        discovered = None

        def runTests(self):
            self.discovered = self.test.countTestCases()
            super().runTests()

    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2 if "-v" in args else 1)
    try:
        program = _RecordingProgram(module=None, argv=["python -m unittest"] + list(args), exit=False, testRunner=runner)
    except SystemExit as exc:
        _emit("complete", mode="unittest", status="usage_error", detail=f"unittest argument error (exit {exc.code})")
        return 2

    result = program.result
    unexpected = len(getattr(result, "unexpectedSuccesses", []))
    _emit(
        "complete",
        mode="unittest",
        status="ran",
        discovered=program.discovered,
        tests_run=result.testsRun,
        failures=len(result.failures),
        errors=len(result.errors),
        skipped=len(result.skipped),
        expected_failures=len(result.expectedFailures),
        unexpected_successes=unexpected,
        interrupted=bool(result.shouldStop),
        was_successful=result.wasSuccessful(),
    )
    if result.testsRun == 0:
        return 5
    return 0 if result.wasSuccessful() else 1


def _run_compile(module, args):
    recurse = True
    paths = []
    for a in args:
        if a in ("-q", "-f"):
            continue
        if a == "-l" and module == "compileall":
            recurse = False
            continue
        if a.startswith("-"):
            _emit("complete", mode="compile", status="usage_error", detail=f"unsupported {module} option '{a}'")
            return 2
        paths.append(a)
    if not paths:
        if module == "py_compile":
            _emit("complete", mode="compile", status="usage_error", detail="py_compile requires at least one file")
            return 2
        paths = ["."]

    root = os.path.realpath(os.getcwd())
    targets = []
    missing = []
    for p in paths:
        real = os.path.realpath(os.path.join(root, p))
        if real != root and not real.startswith(root + os.sep):
            _emit("complete", mode="compile", status="usage_error", detail=f"path '{p}' escapes workspace")
            return 2
        if os.path.isfile(real):
            targets.append(real)
        elif os.path.isdir(real) and module == "compileall":
            for dirpath, dirs, files in os.walk(real):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__") if recurse else []
                targets.extend(os.path.join(dirpath, f) for f in sorted(files) if f.endswith(".py"))
        else:
            missing.append(p)

    errors = []
    for t in targets:
        try:
            with open(t, "rb") as fh:
                compile(fh.read(), t, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            rel = os.path.relpath(t, root).replace(os.sep, "/")
            errors.append(f"{rel}: {type(exc).__name__}: {exc}")
            print(f"*** Error compiling {rel}: {exc}", file=sys.stderr)

    _emit(
        "complete",
        mode="compile",
        status="ran",
        files_compiled=len(targets),
        compile_errors=len(errors),
        missing_targets=missing,
        error_details=errors[:50],
    )
    return 1 if (errors or missing) else (5 if not targets else 0)


def main(argv):
    global _result_fd, _nonce
    # argv: <mode> (--nonce-stdin | --nonce-file PATH) --source SRC --workspace WS -- <runner args>
    if "--" not in argv:
        print("bf-harness: malformed invocation", file=sys.stderr)
        return 3
    sep = argv.index("--")
    opts, runner_args = argv[:sep], argv[sep + 1:]
    mode = opts[0]
    source = opts[opts.index("--source") + 1]
    workspace = opts[opts.index("--workspace") + 1]

    if "--nonce-stdin" in opts:
        _nonce = sys.stdin.readline().strip()
        try:
            sys.stdin.close()
        except Exception:
            pass
        sys.stdin = io.StringIO("")
    else:
        nonce_path = opts[opts.index("--nonce-file") + 1]
        with open(nonce_path, "r", encoding="utf-8") as fh:
            _nonce = fh.read().strip()
        os.remove(nonce_path)
    if not _nonce:
        print("bf-harness: missing nonce", file=sys.stderr)
        return 3

    # Private copy of stdout reserved for structured records.
    _result_fd = os.dup(1)

    sys.dont_write_bytecode = True
    digest, file_count = _snapshot_digest_and_copy(source, workspace)
    os.chdir(workspace)
    # Mirror `python -m`: the working directory is the first import root; drop the harness dir.
    sys.path[:] = [workspace] + [p for p in sys.path[1:] if p and os.path.realpath(p) != os.path.realpath(os.path.dirname(__file__))]

    _emit("started", mode=mode, source_digest=digest, source_file_count=file_count)

    if mode == "unittest":
        return _run_unittest(runner_args)
    if mode in ("compileall", "py_compile"):
        return _run_compile(mode, runner_args)
    _emit("complete", mode=mode, status="usage_error", detail=f"unsupported harness mode '{mode}'")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
