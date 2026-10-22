"""Interpreter authorization: the running interpreter executes as launched.

T1 (Phase 1E, Linux CI): a POSIX virtualenv's bin/python is a symlink to the base
interpreter. The executable policy authorized it by realpath identity and then executed
that realpath, which left the venv, so `python -m pytest` ran without the venv's pytest.

The fix keeps the realpath identity check and executes sys.executable as launched. These
tests pin both halves: the venv context survives (proved with a real venv and real child
processes, not mocks), and no caller-supplied alias can select a new execution context.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from core.runtime.command_policy import DEFAULT_COMMAND_POLICY, resolve_and_authorize_executable
from core.runtime.process import run_hardened_subprocess

REPO_ROOT = Path(__file__).resolve().parents[1]
PY_NAME = "python.exe" if sys.platform == "win32" else "python"
LAUNCHED = os.path.abspath(sys.executable)
CANONICAL = os.path.realpath(sys.executable)

PROBE = textwrap.dedent("""\
    import json, sys
    try:
        import pytest
        pytest_version = pytest.__version__
    except ImportError:
        pytest_version = None
    print(json.dumps({"executable": sys.executable, "prefix": sys.prefix,
                      "base_prefix": sys.base_prefix, "pytest": pytest_version}))
""")


def _symlink(link: Path, target: str) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:  # Windows without symlink privilege
        raise unittest.SkipTest(f"cannot create symlinks here: {exc}")


class _Dirs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="bf-interp-")
        self.root = Path(os.path.realpath(self._tmp.name))
        self.workspace = self.root / "workspace"
        self.outside = self.root / "outside"
        self.workspace.mkdir()
        self.outside.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def authorize(self, exe: str):
        return resolve_and_authorize_executable(exe, self.workspace)

    def assert_denied(self, exe: str, reason_part: str = "") -> None:
        allowed, resolved, _, reason = self.authorize(exe)
        self.assertFalse(allowed, f"{exe!r} must be denied")
        self.assertIsNone(resolved)
        self.assertIn(reason_part, reason or "")


class TestRunningInterpreterSpellings(_Dirs):
    def test_sys_executable_runs_as_launched(self) -> None:
        allowed, resolved, canonical, reason = self.authorize(sys.executable)
        self.assertTrue(allowed, reason)
        self.assertEqual(canonical, "python")
        self.assertEqual(resolved, LAUNCHED)
        self.assertEqual(os.path.realpath(resolved), CANONICAL, "identity is still the running interpreter")

    def test_canonical_file_is_authorized_and_runs_the_launched_interpreter(self) -> None:
        allowed, resolved, canonical, reason = self.authorize(CANONICAL)
        self.assertTrue(allowed, reason)
        self.assertEqual((resolved, canonical), (LAUNCHED, "python"))

    def test_bare_alias_runs_the_launched_interpreter_not_its_canonical_target(self) -> None:
        for token in ("python", "python3"):
            with self.subTest(token):
                allowed, resolved, canonical, reason = self.authorize(token)
                self.assertTrue(allowed, reason)
                self.assertEqual((resolved, canonical), (LAUNCHED, "python"))

    def test_no_virtualenv_active_runs_the_interpreter_itself(self) -> None:
        if sys.prefix != sys.base_prefix:
            self.skipTest("a virtualenv is active")
        self.assertEqual(self.authorize("python")[1], LAUNCHED)

    @unittest.skipUnless(sys.platform == "win32", "Windows path rules")
    def test_windows_path_spelling_is_case_insensitive(self) -> None:
        allowed, resolved, _, reason = self.authorize(LAUNCHED.upper())
        self.assertTrue(allowed, reason)
        self.assertEqual(resolved, LAUNCHED)


class TestAliasesCannotSelectAContext(_Dirs):
    def test_unrelated_python_copy_is_denied(self) -> None:
        copy = self.outside / PY_NAME
        shutil.copyfile(CANONICAL, copy)
        copy.chmod(0o755)
        self.assert_denied(str(copy))

    def test_missing_executable_is_denied(self) -> None:
        self.assert_denied(str(self.outside / PY_NAME), "not found")

    def test_broken_link_is_denied(self) -> None:
        link = self.outside / PY_NAME
        _symlink(link, str(self.outside / "gone" / PY_NAME))
        self.assert_denied(str(link), "not found")

    def test_symlink_alias_with_its_own_pyvenv_cfg_is_denied(self) -> None:
        # The attack: a symlink to the very same system interpreter, next to a
        # pyvenv.cfg the attacker controls, would run with attacker-chosen packages.
        for base in (self.workspace, self.outside):
            with self.subTest(str(base.name)):
                (base / "bin").mkdir()
                (base / "pyvenv.cfg").write_text(
                    f"home = {Path(CANONICAL).parent}\ninclude-system-site-packages = false\n", encoding="utf-8")
                link = base / "bin" / PY_NAME
                _symlink(link, CANONICAL)
                self.assertEqual(os.path.realpath(link), CANONICAL, "identity matches")
                self.assert_denied(str(link))
                res = DEFAULT_COMMAND_POLICY.validate([str(link), "-m", "pytest", "--version"], cwd=self.workspace)
                self.assertFalse(res.is_allowed)

    def test_path_through_a_symlinked_directory_is_denied(self) -> None:
        linked_dir = self.outside / "pydir"
        _symlink(linked_dir, str(Path(CANONICAL).parent))
        self.assert_denied(str(linked_dir / Path(CANONICAL).name))

    def test_interpreter_inside_the_workspace_is_denied(self) -> None:
        # The workspace can rewrite pyvenv.cfg, so a venv there is not a trusted context.
        (self.workspace / "venv" / "bin").mkdir(parents=True)
        inside = self.workspace / "venv" / "bin" / PY_NAME
        try:
            os.symlink(CANONICAL, inside)
        except (OSError, NotImplementedError):
            shutil.copyfile(CANONICAL, inside)
        with patch.object(sys, "executable", str(inside)):
            for token in ("python", str(inside)):
                with self.subTest(token):
                    self.assert_denied(token)

    def test_replaced_or_missing_running_interpreter_is_denied(self) -> None:
        gone = self.outside / "venv" / "bin" / PY_NAME
        for exe in ("", "relative/python", str(gone)):
            with self.subTest(exe), patch.object(sys, "executable", exe):
                self.assert_denied("python")

    def test_arbitrary_executables_are_still_denied(self) -> None:
        for exe in ("bash", "sh", "node", "curl", str(self.outside / "tool")):
            with self.subTest(exe):
                self.assertFalse(self.authorize(exe)[0])


class TestVirtualenvContextIsPreserved(_Dirs):
    """Real child processes through run_hardened_subprocess; no mocks."""

    def probe(self, python: str) -> dict:
        (self.workspace / "probe.py").write_text(PROBE, encoding="utf-8")
        res = run_hardened_subprocess([python, "probe.py"], cwd=self.workspace, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout.strip().splitlines()[-1])

    def test_child_runs_in_the_running_interpreters_context(self) -> None:
        for token in ("python", sys.executable, CANONICAL):
            with self.subTest(token):
                child = self.probe(token)
                self.assertEqual(os.path.realpath(child["prefix"]), os.path.realpath(sys.prefix))
                self.assertEqual(os.path.realpath(child["executable"]), CANONICAL)

    def test_child_imports_the_running_interpreters_pytest(self) -> None:
        try:
            import pytest
        except ImportError:
            self.skipTest("pytest is not installed in this interpreter")
        self.assertEqual(self.probe("python")["pytest"], pytest.__version__)
        res = run_hardened_subprocess(["python", "-m", "pytest", "--version"], cwd=self.workspace, timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_symlinked_posix_venv_keeps_its_prefix(self) -> None:
        # A fresh venv built with symlinks (the POSIX default) runs the policy itself.
        venv = self.outside / "venv"
        cmd = [sys.executable, "-m", "venv", "--without-pip", str(venv)]
        if sys.platform != "win32":
            cmd.insert(3, "--symlinks")
        subprocess.run(cmd, check=True, timeout=120)
        venv_py = venv / ("Scripts" if sys.platform == "win32" else "bin") / PY_NAME
        (self.workspace / "probe.py").write_text(PROBE, encoding="utf-8")
        driver = self.outside / "driver.py"
        driver.write_text(textwrap.dedent("""\
            import json, os, sys, types
            from pathlib import Path
            # Both modules are stdlib-only; skip the package __init__s, which need the
            # project's third-party dependencies (not installed in this bare venv).
            for name in ("core", "core.runtime"):
                pkg = types.ModuleType(name)
                pkg.__path__ = [os.path.join(sys.argv[1], *name.split("."))]
                sys.modules[name] = pkg
            from core.runtime.command_policy import resolve_and_authorize_executable
            from core.runtime.process import run_hardened_subprocess
            ws = Path(sys.argv[2])
            out = {"self": sys.executable, "realpath": os.path.realpath(sys.executable),
                   "is_link": os.path.islink(sys.executable)}
            out["resolved"] = resolve_and_authorize_executable(sys.executable, ws)[1]
            res = run_hardened_subprocess(["python", "probe.py"], cwd=ws, timeout=60)
            out["child"] = json.loads(res.stdout.strip().splitlines()[-1]) if res.returncode == 0 else res.stderr
            print(json.dumps(out))
        """), encoding="utf-8")
        proc = subprocess.run([str(venv_py), "-I", str(driver), str(REPO_ROOT), str(self.workspace)],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        if sys.platform != "win32":
            self.assertTrue(out["is_link"], "a POSIX venv interpreter is a symlink")
            self.assertNotEqual(out["realpath"], out["self"], "it resolves to the base interpreter")
        self.assertEqual(out["resolved"], os.path.abspath(str(venv_py)), "execution keeps the venv path")
        self.assertIsInstance(out["child"], dict, out["child"])
        self.assertEqual(os.path.realpath(out["child"]["prefix"]), os.path.realpath(venv))
        self.assertNotEqual(os.path.realpath(out["child"]["prefix"]), os.path.realpath(out["child"]["base_prefix"]))


if __name__ == "__main__":
    unittest.main()
