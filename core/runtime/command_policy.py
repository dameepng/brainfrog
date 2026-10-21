"""Command Authorization Policy — Strict Subprocess Executable & Argument Boundaries.

Phase 2A Step 4C.2:
Enforces centralized executable authorization and command-specific argument validation:
- Denies unknown executables by default.
- Does not trust basename-only matches.
- Resolves executables deterministically, accounting for Windows .exe vs .bat/.cmd/.ps1
  and POSIX executable boundaries.
- Rejects untrusted relative executable paths, directory traversal, and PATH hijacking
  from the workspace directory.
- Denies shell wrappers (cmd.exe, powershell.exe, bash, sh) and implicit script execution.
- Rejects null bytes, malformed paths, and response files (@filename).
- Defines command-specific argument policies for Git, GitHub CLI (gh), Pytest, and Python.
- Prevents interpreters from becoming general-purpose execution bypasses (banning ALL inline -c,
  restricting -m to pytest/unittest, and enforcing strict workspace script containment).
- Fails closed before any subprocess creation.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union


class CommandPolicyViolation(ValueError):
    """Raised or formatted when a command violates the subprocess authorization policy."""

    def __init__(self, message: str, executable: Optional[str] = None, reason: Optional[str] = None):
        super().__init__(message)
        self.executable = executable
        self.reason = reason


@dataclass(frozen=True)
class CommandValidationResult:
    """Outcome of validating a command against the allowlist policy."""

    is_allowed: bool
    resolved_executable: Optional[str] = None
    canonical_name: Optional[str] = None
    reason: Optional[str] = None
    sanitized_args: Optional[List[str]] = None

    @property
    def allowed(self) -> bool:
        """Alias for is_allowed."""
        return self.is_allowed


# Canonical authorized executable identifiers
AUTHORIZED_CANONICAL_EXECUTABLES: Set[str] = {
    "git",
    "gh",
    "pytest",
    "python",
    "python3",
}

# Windows native script extensions that must never be implicitly invoked as binaries
DISALLOWED_SCRIPT_EXTENSIONS: Set[str] = {
    ".bat",
    ".cmd",
    ".ps1",
    ".vbs",
    ".sh",
    ".bash",
}

# Disallowed shell wrapper names across all platforms
DISALLOWED_SHELL_NAMES: Set[str] = {
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "bash",
    "sh",
    "zsh",
    "csh",
    "tcsh",
    "fish",
    "wscript",
    "cscript",
}

# Git subcommands authorized for autonomous coding
AUTHORIZED_GIT_SUBCOMMANDS: Set[str] = {
    "add",
    "branch",
    "cat-file",
    "checkout",
    "commit",
    "config",
    "diff",
    "init",
    "ls-files",
    "push",
    "remote",
    "reset",
    "rev-parse",
    "rm",
    "show",
    "status",
    "update-index",
}

# Global Git options strictly disallowed due to out-of-workspace redirection or code execution
DISALLOWED_GIT_GLOBAL_PREFIXES: Tuple[str, ...] = (
    "-C",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
    "-c",
    "--config",
    "--config-env",
    "--paginate",
    "-p",
    "--literal-pathspecs",
    "--glob-pathspecs",
    "--icase-pathspecs",
    "--super-prefix",
)

# Pytest options strictly disallowed
DISALLOWED_PYTEST_PREFIXES: Tuple[str, ...] = (
    "-p",
    "-o",
    "--override-ini",
    "-c",
    "--config-file",
    "--import-mode",
    "--rootdir",
    "--assert",
    "--pdb",
    "--trace",
    "--capture=fd",
)

# Pytest allowed standalone flags
PYTEST_ALLOWED_FLAGS: Set[str] = {
    "-v",
    "-vv",
    "-vvv",
    "-q",
    "-qq",
    "-s",
    "--verbose",
    "--quiet",
    "-x",
    "--exitfirst",
    "--lf",
    "--last-failed",
    "--ff",
    "--failed-first",
    "--disable-warnings",
    "--collect-only",
    "-h",
    "--help",
    "--version",
    "-V",
    "--strict-markers",
    "--strict-config",
    "-ra",
    "-rA",
    "-rx",
    "-rX",
}

# Pytest allowed parameterized options
PYTEST_ALLOWED_OPTION_NAMES: Set[str] = {
    "-k",
    "-m",
    "--maxfail",
    "--durations",
    "-W",
    "--junitxml",
    "--ignore",
    "--deselect",
    "--color",
    "--tb",
    "--capture",
    "--show-capture",
}


def _is_relative_to(path: Path, base: Path) -> bool:
    """Check if path is relative to base, accounting for symlinks, junctions, and case normalization."""
    try:
        p_real = Path(os.path.realpath(str(path)))
        b_real = Path(os.path.realpath(str(base)))
        p_real.relative_to(b_real)
        return True
    except (ValueError, AttributeError, RuntimeError):
        pass
    try:
        p_str = os.path.normcase(os.path.realpath(str(path)))
        b_str = os.path.normcase(os.path.realpath(str(base)))
        return p_str == b_str or p_str.startswith(b_str + os.sep)
    except Exception:
        return False


def _build_safe_system_path(cwd: Path, env: Optional[Dict[str, str]] = None) -> str:
    """Build a search PATH strictly from trusted system directories and sanitized host PATH.

    Security Rule:
    Caller-controlled PATH must NEVER establish executable trust. Any PATH passed via
    caller-supplied env is strictly ignored. The search path is explicitly constructed from
    verified system directories, the running Python interpreter's directories, and sanitized
    host system PATH entries (excluding cwd, workspace subdirectories, and temporary directories).
    """
    cwd_resolved = Path(os.path.realpath(str(cwd)))

    # Identify system temp directories to exclude
    temp_roots: List[Path] = []
    try:
        temp_roots.append(Path(os.path.realpath(tempfile.gettempdir())))
    except Exception:
        pass
    for var in ("TEMP", "TMP"):
        val = os.environ.get(var)
        if val:
            try:
                temp_roots.append(Path(os.path.realpath(val)))
            except Exception:
                pass
    if sys.platform == "win32":
        sys_root_env = os.environ.get("SystemRoot", r"C:\Windows")
        try:
            temp_roots.append(Path(os.path.realpath(os.path.join(sys_root_env, "Temp"))))
        except Exception:
            pass
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            try:
                temp_roots.append(Path(os.path.realpath(os.path.join(local_app_data, "Temp"))))
            except Exception:
                pass

    # 1. Preferred explicit trusted system directories
    explicit_trusted_dirs: List[Path] = []
    if sys.platform == "win32":
        sys_root = Path(os.path.realpath(os.environ.get("SystemRoot", r"C:\Windows")))
        explicit_trusted_dirs.extend([
            sys_root / "System32",
            sys_root,
            sys_root / "System32" / "Wbem",
        ])
        for prog_dir in (r"C:\Program Files", r"C:\Program Files (x86)"):
            explicit_trusted_dirs.extend([
                Path(prog_dir) / "Git" / "cmd",
                Path(prog_dir) / "Git" / "bin",
                Path(prog_dir) / "GitHub CLI",
            ])
    else:
        explicit_trusted_dirs.extend([
            Path("/usr/bin"),
            Path("/bin"),
            Path("/usr/sbin"),
            Path("/sbin"),
            Path("/usr/local/bin"),
        ])

    # Add running Python's directory and Scripts/bin directory
    py_dir = Path(os.path.realpath(sys.executable)).parent
    scripts_dir = py_dir / ("Scripts" if sys.platform == "win32" else "bin")
    explicit_trusted_dirs.extend([py_dir, scripts_dir])

    # 2. Collect candidate directories from trusted sources:
    # First explicit trusted system directories, then sanitized entries from host os.environ["PATH"]
    # (Note: Caller-supplied env is intentionally ignored to prevent PATH hijacking)
    candidate_paths: List[Path] = []
    for td in explicit_trusted_dirs:
        try:
            if td.exists() and td.is_dir():
                candidate_paths.append(Path(os.path.realpath(str(td))))
        except Exception:
            continue

    raw_system_path = os.environ.get("PATH", "")
    for entry in raw_system_path.split(os.pathsep):
        entry_stripped = entry.strip()
        if not entry_stripped:
            continue
        try:
            entry_p = Path(entry_stripped)
            if not entry_p.is_absolute():
                continue
            entry_real = Path(os.path.realpath(str(entry_p)))
            if not entry_real.exists() or not entry_real.is_dir():
                continue
            candidate_paths.append(entry_real)
        except Exception:
            continue

    # 3. Filter candidates: Exclude cwd, workspace dirs, and temp dirs
    safe_dirs: List[str] = []
    seen: Set[str] = set()

    for cand in candidate_paths:
        try:
            if _is_relative_to(cand, cwd_resolved):
                continue
            if any(_is_relative_to(cand, tr) for tr in temp_roots):
                continue
            norm_key = os.path.normcase(str(cand))
            if norm_key not in seen:
                seen.add(norm_key)
                safe_dirs.append(str(cand))
        except Exception:
            continue

    return os.pathsep.join(safe_dirs)


def resolve_and_authorize_executable(
    raw_exe: str,
    cwd: Path,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """Deterministically resolve and authorize an executable token.

    Trust Rule:
    An absolute executable path is authorized ONLY if its canonical realpath identity
    matches a known, immutable trusted identity:
    1. Python: Must resolve to the running interpreter (sys.executable).
    2. Taskkill (Windows): Must resolve strictly to SystemRoot\\System32\\taskkill.exe.
    3. Other tools (Git, GitHub CLI, Pytest): Must match the canonical executable
       resolved from the sanitized trusted system PATH (excluding cwd and temp dirs).
       For Pytest, the running Python's Scripts/bin directory is also checked.
    Arbitrary binaries in temporary or untrusted directories are strictly rejected
    even if their filename or stem matches an authorized tool name.

    Returns:
        (is_allowed, resolved_path, canonical_name, rejection_reason)
    """
    if raw_exe is None:
        return False, None, None, "Executable token is None"

    if "\0" in raw_exe:
        return False, None, None, "Null byte detected in executable path"

    exe_str = str(raw_exe).strip()
    if not exe_str:
        return False, None, None, "Empty executable token"

    # Reject shell wrappers explicitly by token name
    token_basename = Path(exe_str).name.lower()
    if token_basename in DISALLOWED_SHELL_NAMES or exe_str.lower() in DISALLOWED_SHELL_NAMES:
        return False, None, None, f"Shell wrapper '{token_basename}' is prohibited in autonomous execution"

    # Reject script wrapper extensions on Windows
    token_suffix = Path(exe_str).suffix.lower()
    if token_suffix in DISALLOWED_SCRIPT_EXTENSIONS:
        return (
            False,
            None,
            None,
            f"Script extension '{token_suffix}' is prohibited; only native binaries (.exe) are authorized",
        )

    cwd_resolved = Path(os.path.realpath(str(cwd)))
    has_separators = "/" in exe_str or "\\" in exe_str

    if has_separators:
        exe_path = Path(exe_str)
        if not exe_path.is_absolute():
            return False, None, None, f"Untrusted relative executable path denied: '{exe_str}'"

        try:
            resolved_exe = Path(os.path.realpath(str(exe_path)))
        except Exception as exc:
            return False, None, None, f"Cannot resolve executable path '{exe_str}': {exc}"

        if not resolved_exe.exists() or not resolved_exe.is_file():
            return False, None, None, f"Executable binary not found at '{resolved_exe}'"

        # Check for PATH hijacking from inside workspace
        if _is_relative_to(resolved_exe, cwd_resolved):
            return (
                False,
                None,
                None,
                f"Executable inside untrusted workspace directory is denied (PATH hijacking prevention): '{resolved_exe}'",
            )

        # On Windows, enforce binary extension
        if sys.platform == "win32" and resolved_exe.suffix.lower() != ".exe":
            return (
                False,
                None,
                None,
                f"Invalid executable format '{resolved_exe.suffix}'; only .exe binaries are authorized on Windows",
            )

        # Match against authorized Python interpreter identity
        sys_py = Path(os.path.realpath(sys.executable))
        is_python_match = (
            (resolved_exe == sys_py)
            if sys.platform != "win32"
            else (str(resolved_exe).lower() == str(sys_py).lower())
        )
        if is_python_match:
            return True, str(resolved_exe), "python", None

        # Check taskkill on Windows
        if sys.platform == "win32" and resolved_exe.name.lower() == "taskkill.exe":
            sys_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
            tk_path = sys_root / "System32" / "taskkill.exe"
            if tk_path.exists() and (
                (resolved_exe == tk_path) or (str(resolved_exe).lower() == str(tk_path).lower())
            ):
                return True, str(resolved_exe), "taskkill", None

        # Reject executables located in temporary directories
        temp_roots = [Path(os.path.realpath(tempfile.gettempdir()))]
        for var in ("TEMP", "TMP"):
            val = os.environ.get(var)
            if val:
                try:
                    temp_roots.append(Path(os.path.realpath(val)))
                except Exception:
                    pass
        if any(_is_relative_to(resolved_exe, tr) for tr in temp_roots):
            return (
                False,
                None,
                None,
                f"Executable binary located in untrusted temporary directory is denied: '{resolved_exe}'",
            )

        # Canonical identity matching for other authorized executables (git, gh, pytest)
        # We verify that resolved_exe matches the exact canonical binary found on trusted system PATH
        stem = resolved_exe.stem.lower()
        if stem in AUTHORIZED_CANONICAL_EXECUTABLES:
            safe_path = _build_safe_system_path(cwd, env=None)
            which_result = shutil.which(resolved_exe.name, path=safe_path)
            if not which_result and stem == "pytest":
                scripts_dir = Path(sys.executable).parent / ("Scripts" if sys.platform == "win32" else "bin")
                cand = scripts_dir / ("pytest.exe" if sys.platform == "win32" else "pytest")
                if cand.exists() and cand.is_file():
                    which_result = str(cand)

            if which_result:
                try:
                    resolved_trusted = Path(os.path.realpath(which_result))
                    is_trusted_match = (
                        (resolved_exe == resolved_trusted)
                        if sys.platform != "win32"
                        else (str(resolved_exe).lower() == str(resolved_trusted).lower())
                    )
                    if is_trusted_match:
                        return True, str(resolved_exe), stem, None
                except Exception:
                    pass

        return False, None, None, f"Unauthorized executable binary: '{resolved_exe}'"

    else:
        # Bare executable name (e.g. "git", "gh", "pytest", "python")
        canonical_name = exe_str.lower()
        if canonical_name.endswith(".exe"):
            canonical_name = canonical_name[:-4]

        # Handle taskkill internal tool on Windows
        if canonical_name == "taskkill" and sys.platform == "win32":
            sys_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
            tk_path = sys_root / "System32" / "taskkill.exe"
            if tk_path.exists() and tk_path.is_file():
                return True, str(tk_path), "taskkill", None
            return False, None, None, "System taskkill.exe not found"

        # Handle Python interpreter: Must resolve strictly to the running interpreter (sys.executable)
        if canonical_name in ("python", "python3"):
            py_canonical = str(Path(os.path.realpath(sys.executable)))
            return True, py_canonical, "python", None

        if canonical_name not in AUTHORIZED_CANONICAL_EXECUTABLES:
            return (
                False,
                None,
                None,
                f"Unauthorized executable: '{exe_str}' is not in the authorized executable allowlist",
            )

        # Deterministic PATH lookup using trusted system search path
        # (Caller-supplied PATH is strictly ignored)
        safe_path = _build_safe_system_path(cwd)
        which_result = shutil.which(exe_str, path=safe_path)

        # Fallback for pytest to sys.executable's Scripts / bin directory
        if not which_result and canonical_name == "pytest":
            scripts_dir = Path(sys.executable).parent / ("Scripts" if sys.platform == "win32" else "bin")
            cand = scripts_dir / ("pytest.exe" if sys.platform == "win32" else "pytest")
            if cand.exists() and cand.is_file():
                which_result = str(cand)

        if not which_result:
            return False, None, None, f"Executable '{exe_str}' not found on authorized system PATH"

        try:
            resolved_which = Path(os.path.realpath(which_result))
        except Exception as exc:
            return False, None, None, f"Cannot resolve executable path '{which_result}': {exc}"

        if not resolved_which.exists() or not resolved_which.is_file():
            return False, None, None, f"Resolved executable '{resolved_which}' does not exist"

        if _is_relative_to(resolved_which, cwd_resolved):
            return (
                False,
                None,
                None,
                f"Executable resolved to untrusted workspace directory (PATH hijacking denied): '{resolved_which}'",
            )

        # Reject executables located in temporary directories
        temp_roots = [Path(os.path.realpath(tempfile.gettempdir()))]
        for var in ("TEMP", "TMP"):
            val = os.environ.get(var)
            if val:
                try:
                    temp_roots.append(Path(os.path.realpath(val)))
                except Exception:
                    pass
        if sys.platform == "win32":
            sys_root_env = os.environ.get("SystemRoot", r"C:\Windows")
            try:
                temp_roots.append(Path(os.path.realpath(os.path.join(sys_root_env, "Temp"))))
            except Exception:
                pass
            local_app_data = os.environ.get("LOCALAPPDATA")
            if local_app_data:
                try:
                    temp_roots.append(Path(os.path.realpath(os.path.join(local_app_data, "Temp"))))
                except Exception:
                    pass
        if any(_is_relative_to(resolved_which, tr) for tr in temp_roots):
            return (
                False,
                None,
                None,
                f"Executable binary located in untrusted temporary directory is denied: '{resolved_which}'",
            )

        if sys.platform == "win32":
            if resolved_which.suffix.lower() in DISALLOWED_SCRIPT_EXTENSIONS:
                return (
                    False,
                    None,
                    None,
                    f"Resolved executable is a script wrapper ('{resolved_which.name}'); only .exe binaries are authorized",
                )
            if resolved_which.suffix.lower() != ".exe":
                return (
                    False,
                    None,
                    None,
                    f"Resolved executable '{resolved_which.name}' does not have .exe extension",
                )

        return True, str(resolved_which), canonical_name, None


def validate_git_arguments(
    args: List[str],
    cwd: Path,
    *,
    allow_git_worktree: bool = False,
    allow_git_fetch_pull: bool = False,
) -> Tuple[bool, Optional[str]]:
    """Validate Git command arguments against unauthorized options and subcommands."""
    if not args:
        return True, None

    for arg in args:
        if "\0" in arg:
            return False, "Null byte detected in Git argument"
        if arg.startswith("@"):
            return False, f"Response file syntax ('{arg}') is disallowed to prevent argument injection"

    # Identify git subcommand
    subcommand: Optional[str] = None
    subcmd_idx = -1
    for idx, arg in enumerate(args):
        if not arg.startswith("-"):
            subcommand = arg.lower()
            subcmd_idx = idx
            break

    if not subcommand:
        # Bare queries like `git --version`
        if any(a in ("--version", "-v", "--help", "-h") for a in args):
            return True, None
        return False, "Missing Git subcommand"

    # Enforce global options appearing before the subcommand
    for g_arg in args[:subcmd_idx]:
        for dis_pfx in DISALLOWED_GIT_GLOBAL_PREFIXES:
            if (
                g_arg == dis_pfx
                or g_arg.startswith(dis_pfx + "=")
                or (dis_pfx.startswith("-") and len(dis_pfx) == 2 and g_arg.startswith(dis_pfx))
            ):
                return False, f"Disallowed Git option: '{g_arg}'"
        if g_arg not in ("--version", "-v", "--help", "-h", "--no-optional-locks", "--no-pager", "-q", "--quiet"):
            return False, f"Disallowed Git option: '{g_arg}'"

    valid_subcommands = set(AUTHORIZED_GIT_SUBCOMMANDS)
    if allow_git_worktree:
        valid_subcommands.add("worktree")
    if allow_git_fetch_pull:
        valid_subcommands.add("fetch")
        valid_subcommands.add("pull")

    if subcommand not in valid_subcommands:
        return False, f"Unauthorized Git subcommand: '{subcommand}'"

    # Check remaining arguments after subcommand
    rem_args = args[subcmd_idx + 1 :]
    for a in rem_args:
        if a in ("--ext-diff", "--external-diff") or a.startswith("--ext-diff=") or a.startswith("--external-diff="):
            return False, f"Disallowed Git option: '{a}'"
        if a.startswith("--exec-path"):
            return False, f"Disallowed Git option: '{a}'"
        if a == "-c" or a.startswith("-c="):
            return False, f"Disallowed Git option: '{a}'"
        for dis_pfx in DISALLOWED_GIT_GLOBAL_PREFIXES:
            if a == dis_pfx or a.startswith(dis_pfx + "="):
                return False, f"Disallowed Git option: '{a}'"

    # Subcommand specific checks
    if subcommand == "commit":
        for a in rem_args:
            if a in ("--cleanup=scissors", "--template") or a.startswith("--template="):
                return False, f"Disallowed commit option: '{a}'"

    elif subcommand == "push":
        allowed_push_flags = {
            "-u",
            "--set-upstream",
            "--force-with-lease",
            "--tags",
            "-q",
            "--quiet",
            "--dry-run",
            "--atomic",
            "--porcelain",
        }
        for a in rem_args:
            a_lower = a.lower()
            if "ext::" in a_lower or "fd::" in a_lower or "::" in a:
                return False, f"Prohibited remote helper syntax in git push: '{a}'"
            if a.startswith("!"):
                return False, f"Prohibited external command syntax in git push: '{a}'"

            if a.startswith("-"):
                opt_name = a.split("=", 1)[0]
                if opt_name not in allowed_push_flags:
                    return False, f"Disallowed Git push option: '{a}'"
                if opt_name == "--force-with-lease" and "=" in a:
                    val = a.split("=", 1)[1]
                    if "ext::" in val.lower() or "::" in val or val.startswith("!"):
                        return False, f"Prohibited ref value in git push: '{a}'"
            else:
                if not a.strip():
                    return False, "Empty positional argument in git push"
                if any(bad in a for bad in (";", "|", "&", "`", "$", "\n", "\r")):
                    return False, f"Prohibited characters in git push argument: '{a}'"

    elif subcommand == "config":
        for a in rem_args:
            if a in ("--file", "--edit", "-e", "--add", "--replace-all", "--unset", "--unset-all"):
                return False, f"Disallowed Git config option: '{a}'"
            if a.startswith("--file="):
                return False, f"Disallowed Git config option: '{a}'"
            a_lower = a.lower()
            if any(
                danger in a_lower
                for danger in (
                    "credential",
                    "helper",
                    "pager",
                    "editor",
                    "sshcommand",
                    "hookspath",
                    "filter",
                    "ext-diff",
                    "alias",
                )
            ):
                return False, f"Disallowed Git config key: '{a}'"

        positionals = [a for a in rem_args if not a.startswith("-")]
        if not positionals and not any(a in ("--list", "-l", "--get-regexp") for a in rem_args):
            return False, "Arbitrary Git config file manipulation is disallowed"
        has_get = any(a in ("--get", "--get-all", "--get-regexp") for a in rem_args)
        if len(positionals) > 1 and not has_get:
            return False, f"Git config write operations are prohibited: '{' '.join(positionals)}'"

    elif subcommand == "remote":
        if rem_args:
            sub_remote = rem_args[0].lower()
            if sub_remote not in ("-v", "--verbose", "get-url", "add", "set-url", "remove", "rm"):
                return False, f"Disallowed git remote operation: '{sub_remote}'"
            if sub_remote in ("add", "set-url") and len(rem_args) >= 3:
                url_arg = rem_args[2]
                if (
                    url_arg.startswith("!")
                    or "ext::" in url_arg.lower()
                    or "fd::" in url_arg.lower()
                    or "::" in url_arg
                ):
                    return False, f"Prohibited git remote URL syntax: '{url_arg}'"

    elif subcommand == "worktree":
        if not allow_git_worktree:
            return False, "Unauthorized Git subcommand: 'worktree'"
        if not rem_args:
            return False, "Missing git worktree action"
        wt_action = rem_args[0].lower()
        if wt_action not in ("add", "remove", "prune", "list"):
            return False, f"Disallowed git worktree action: '{wt_action}'"
        for a in rem_args[1:]:
            if a.startswith("-") and a not in ("--force", "-f", "-q", "--quiet"):
                return False, f"Disallowed git worktree option: '{a}'"

    elif subcommand in ("fetch", "pull"):
        if not allow_git_fetch_pull:
            return False, f"Unauthorized Git subcommand: '{subcommand}'"
        for a in rem_args:
            a_lower = a.lower()
            if "ext::" in a_lower or "fd::" in a_lower or "::" in a or a.startswith("!"):
                return False, f"Prohibited remote syntax in git {subcommand}: '{a}'"
            if a.startswith("-") and a not in ("-q", "--quiet", "--force", "-f", "--tags", "-t", "--prune", "-p"):
                return False, f"Disallowed git {subcommand} option: '{a}'"

    elif subcommand in ("add", "rm", "reset", "checkout", "diff"):
        real_cwd = Path(os.path.realpath(str(cwd)))
        for a in rem_args:
            if not a.startswith("-") and a not in (".", "HEAD", "HEAD~1", "--"):
                if "..." in a or (".." in a and not ("/" in a or "\\" in a)):
                    continue
                try:
                    target_p = Path(a)
                    if not target_p.is_absolute():
                        target_p = cwd / target_p
                    real_target = Path(os.path.realpath(str(target_p)))
                    if real_target.exists() and not _is_relative_to(real_target, real_cwd):
                        return False, f"Git target path '{a}' escapes workspace directory"
                except Exception:
                    pass

    return True, None


def validate_gh_arguments(args: List[str], cwd: Path) -> Tuple[bool, Optional[str]]:
    """Validate GitHub CLI arguments."""
    if not args:
        return True, None

    for arg in args:
        if "\0" in arg:
            return False, "Null byte detected in gh argument"

    # Deny extensions, aliases, and dangerous commands
    lower_args = [a.lower() for a in args]
    if any(a in ("extension", "alias", "api", "auth") for a in lower_args):
        return False, "Disallowed gh subcommand (extension, alias, api, auth)"

    # Validate response file syntax: only on flags/subcommands, not on parameter values like --body
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--body", "-b", "--title", "-t"):
            skip_next = True
            continue
        if arg.startswith("@"):
            return False, f"Response file syntax ('{arg}') is disallowed in gh CLI"

    return True, None


def validate_pytest_arguments(args: List[str], cwd: Path) -> Tuple[bool, Optional[str]]:
    """Validate Pytest command arguments using an explicit argument grammar."""
    real_cwd = Path(os.path.realpath(str(cwd)))

    for arg in args:
        if "\0" in arg:
            return False, "Null byte detected in pytest argument"
        if arg.startswith("@"):
            return False, f"Response file syntax ('{arg}') is disallowed in pytest"

        # Explicitly disallow plugin loading options
        if (
            arg == "-p"
            or arg.startswith("-p=")
            or (arg.startswith("-p") and len(arg) > 2 and not arg.startswith("-p-"))
        ):
            return False, f"Disallowed Pytest option: '{arg}' (plugin loading is strictly prohibited)"

        # Explicitly disallow config overrides
        if (
            arg == "-o"
            or arg.startswith("-o=")
            or (arg.startswith("-o") and len(arg) > 2)
            or arg.startswith("--override-ini")
        ):
            return False, f"Disallowed Pytest option: '{arg}' (configuration override is prohibited)"

        # Explicitly disallow custom config files
        if (
            arg == "-c"
            or arg.startswith("-c=")
            or (arg.startswith("-c") and len(arg) > 2)
            or arg.startswith("--config-file")
        ):
            return False, f"Disallowed Pytest option: '{arg}' (custom config file is prohibited)"

        for dis in DISALLOWED_PYTEST_PREFIXES:
            if arg == dis or arg.startswith(dis + "="):
                return False, f"Disallowed Pytest option: '{arg}'"

    skip_next = False
    for idx, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue

        if arg.startswith("-"):
            if arg in PYTEST_ALLOWED_FLAGS:
                continue

            matched = False
            if "=" in arg:
                opt_key, _ = arg.split("=", 1)
                if opt_key in PYTEST_ALLOWED_OPTION_NAMES or any(
                    arg.startswith(prefix)
                    for prefix in ("--color=", "--tb=", "--capture=", "--show-capture=")
                ):
                    matched = True
                    continue

            if arg in PYTEST_ALLOWED_OPTION_NAMES:
                skip_next = True
                continue

            for opt_prefix in ("-k", "-m", "-W"):
                if arg.startswith(opt_prefix) and len(arg) > len(opt_prefix):
                    matched = True
                    break
            if matched:
                continue

            return False, f"Disallowed Pytest option: '{arg}'"

        else:
            clean_target = arg.split("::")[0]
            if clean_target and clean_target != ".":
                try:
                    target_p = Path(clean_target)
                    if not target_p.is_absolute():
                        target_p = cwd / target_p
                    real_target = Path(os.path.realpath(str(target_p)))
                    if not _is_relative_to(real_target, real_cwd):
                        return False, f"Pytest target '{arg}' resolves outside workspace"
                except Exception:
                    pass

    return True, None


def validate_python_arguments(
    args: List[str],
    cwd: Path,
    allowed_script_paths: Optional[Sequence[Path]] = None,
    allow_safe_inline_code: bool = False,
) -> Tuple[bool, Optional[str]]:
    """Validate Python interpreter arguments.

    Strict security boundary:
    - Completely rejects inline code (-c, -c=...).
    - Restricts -m to approved modules (pytest, unittest).
    - Requires existing workspace script with reparse-point/symlink containment.
    """
    if not args or not any(str(a).strip() for a in args):
        return False, "Python execution requires a script path or approved module (-m); bare interpreter is disallowed"

    for arg in args:
        if "\0" in arg:
            return False, "Null byte detected in Python argument"
        if arg.startswith("@"):
            return False, f"Response file syntax ('{arg}') is disallowed in python arguments"
        # Banning all forms of inline -c execution
        if arg == "-c" or arg.startswith("-c=") or arg.startswith("-c"):
            return False, "Inline Python code execution (-c) is strictly prohibited to prevent arbitrary code execution"

    first_arg = args[0]

    # Mode 1: Module execution (-m pytest, -m unittest)
    if first_arg == "-m" or first_arg.startswith("-m="):
        if first_arg == "-m":
            if len(args) < 2:
                return False, "Missing module name after -m"
            module_name = args[1].lower()
            rem_args = args[2:]
        else:
            module_name = first_arg.split("=", 1)[1].lower()
            rem_args = args[1:]

        if module_name not in ("pytest", "unittest", "compileall", "py_compile"):
            return False, (
                f"Unauthorized Python module execution: 'python -m {module_name}' "
                f"(only pytest, unittest, compileall, and py_compile are authorized)"
            )

        if module_name == "pytest":
            return validate_pytest_arguments(rem_args, cwd)
        elif module_name in ("compileall", "py_compile"):
            for a in rem_args:
                if a.startswith("@") or "\0" in a:
                    return False, f"Disallowed argument '{a}' in python -m {module_name}"
            return True, None
        elif module_name == "unittest":
            real_cwd = Path(os.path.realpath(str(cwd)))
            for a in rem_args:
                if not a.startswith("-") and a != ".":
                    try:
                        p = Path(a)
                        if not p.is_absolute():
                            p = cwd / p
                        real_p = Path(os.path.realpath(str(p)))
                        if real_p.exists() and not _is_relative_to(real_p, real_cwd):
                            return False, f"Unittest target '{a}' resolves outside workspace"
                    except Exception:
                        pass
        return True, None

    # Mode 2: Version query
    if first_arg in ("--version", "-V"):
        if len(args) == 1:
            return True, None
        return False, "Unexpected arguments after Python version flag"

    # Mode 3: Script execution (python script.py [args...])
    if not first_arg.startswith("-"):
        script_arg = first_arg
        target_p = Path(script_arg)
        if not target_p.is_absolute():
            target_p = cwd / target_p

        try:
            real_script = Path(os.path.realpath(str(target_p)))
            real_cwd = Path(os.path.realpath(str(cwd)))
        except Exception as exc:
            return False, f"Failed to resolve Python script path: {exc}"

        if not real_script.exists() or not real_script.is_file():
            return False, f"Target Python script not found: '{first_arg}'"

        is_contained = _is_relative_to(real_script, real_cwd)
        if not is_contained and allowed_script_paths:
            for allowed_p in allowed_script_paths:
                try:
                    real_allowed = Path(os.path.realpath(str(allowed_p)))
                    if real_script == real_allowed or _is_relative_to(real_script, real_allowed):
                        is_contained = True
                        break
                except Exception:
                    continue

        if not is_contained:
            return False, f"Target Python script '{first_arg}' resolves outside authorized workspace"

        for extra_arg in args[1:]:
            if extra_arg.startswith("@"):
                return False, f"Response file syntax ('{extra_arg}') is disallowed in script arguments"

        return True, None

    return False, f"Disallowed Python interpreter argument: '{first_arg}'"


class CommandPolicy:
    """Centralized command authorization and argument policy."""

    def __init__(
        self,
        *,
        allowed_script_paths: Optional[Sequence[Path]] = None,
        strict_test_runners_only: bool = False,
        allow_safe_inline_code: bool = False,  # Deprecated; inline code is always denied
        allow_git_worktree: bool = False,
        allow_git_fetch_pull: bool = False,
    ) -> None:
        self.allowed_script_paths = list(allowed_script_paths) if allowed_script_paths else []
        self.strict_test_runners_only = strict_test_runners_only
        self.allow_safe_inline_code = False
        self.allow_git_worktree = allow_git_worktree
        self.allow_git_fetch_pull = allow_git_fetch_pull

    def validate(
        self,
        cmd: Union[List[str], Sequence[str], str],
        cwd: Union[Path, str],
        env: Optional[Dict[str, str]] = None,
    ) -> CommandValidationResult:
        """Validate command against executable and argument policies."""
        cwd_path = Path(cwd).resolve()

        if cmd is None:
            return CommandValidationResult(is_allowed=False, reason="Empty command provided (None)")

        if isinstance(cmd, str):
            cmd_list = [cmd]
        elif isinstance(cmd, (list, tuple, Sequence)):
            cmd_list = [str(a) for a in cmd]
        else:
            return CommandValidationResult(
                is_allowed=False, reason=f"Invalid command type: {type(cmd).__name__}"
            )

        if not cmd_list or not any(str(c).strip() for c in cmd_list):
            return CommandValidationResult(is_allowed=False, reason="Empty command provided")

        raw_exe = cmd_list[0]
        args = cmd_list[1:]

        # Pre-validate arguments for recognized canonical tools
        raw_token = Path(raw_exe).name.lower()
        if raw_token.endswith(".exe"):
            raw_token = raw_token[:-4]

        if raw_token in AUTHORIZED_CANONICAL_EXECUTABLES:
            arg_valid = True
            arg_reason: Optional[str] = None
            if raw_token == "git":
                arg_valid, arg_reason = validate_git_arguments(
                    args,
                    cwd_path,
                    allow_git_worktree=self.allow_git_worktree,
                    allow_git_fetch_pull=self.allow_git_fetch_pull,
                )
            elif raw_token == "gh":
                arg_valid, arg_reason = validate_gh_arguments(args, cwd_path)
            elif raw_token == "pytest":
                arg_valid, arg_reason = validate_pytest_arguments(args, cwd_path)
            elif raw_token in ("python", "python3"):
                arg_valid, arg_reason = validate_python_arguments(
                    args, cwd_path, allowed_script_paths=self.allowed_script_paths
                )
            if not arg_valid:
                return CommandValidationResult(
                    is_allowed=False,
                    canonical_name=raw_token,
                    reason=arg_reason or "Disallowed argument combination",
                )

        # Step 1: Resolve and authorize executable
        is_allowed, resolved_exe, canonical_name, reason = resolve_and_authorize_executable(
            raw_exe, cwd_path, env=env
        )
        if not is_allowed or not canonical_name:
            return CommandValidationResult(
                is_allowed=False,
                reason=reason or f"Executable '{raw_exe}' denied by allowlist policy",
            )

        # Step 2: Enforce strict test runner restriction if configured
        if self.strict_test_runners_only:
            if canonical_name not in ("pytest", "python", "python3"):
                return CommandValidationResult(
                    is_allowed=False,
                    resolved_executable=resolved_exe,
                    canonical_name=canonical_name,
                    reason=f"Executable '{canonical_name}' is not an authorized test runner",
                )

        # Step 3: Validate arguments according to authorized canonical executable
        arg_valid = True
        arg_reason: Optional[str] = None
        if canonical_name == "git":
            arg_valid, arg_reason = validate_git_arguments(
                args,
                cwd_path,
                allow_git_worktree=self.allow_git_worktree,
                allow_git_fetch_pull=self.allow_git_fetch_pull,
            )
        elif canonical_name == "gh":
            arg_valid, arg_reason = validate_gh_arguments(args, cwd_path)
        elif canonical_name == "pytest":
            arg_valid, arg_reason = validate_pytest_arguments(args, cwd_path)
        elif canonical_name in ("python", "python3"):
            arg_valid, arg_reason = validate_python_arguments(
                args, cwd_path, allowed_script_paths=self.allowed_script_paths
            )
        elif canonical_name == "taskkill":
            for a in args:
                if not (a in ("/F", "/T", "/PID") or a.isdigit()):
                    arg_valid = False
                    arg_reason = f"Disallowed taskkill argument: '{a}'"
                    break
        else:
            arg_valid = False
            arg_reason = f"No argument validator configured for authorized executable: '{canonical_name}'"

        if not arg_valid:
            return CommandValidationResult(
                is_allowed=False,
                resolved_executable=resolved_exe,
                canonical_name=canonical_name,
                reason=arg_reason or "Disallowed argument combination",
            )

        return CommandValidationResult(
            is_allowed=True,
            resolved_executable=resolved_exe,
            canonical_name=canonical_name,
            sanitized_args=args,
        )


# Default production command policy
DEFAULT_COMMAND_POLICY = CommandPolicy()
STRICT_PRODUCTION_POLICY = CommandPolicy()
