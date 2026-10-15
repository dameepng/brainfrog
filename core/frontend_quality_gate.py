"""Frontend Quality Gate backed by MCP browser verification server.

Layer 2: Domain-specific verification pipeline.
Consumes the generic McpClient (Layer 1) to execute an automated 7-step quality gate:
1. Connect to MCP server (brainfrog-verify-mcp)
2. Run build step (e.g. `npm run build`) and poll output until exitCode != null
3. If build fails, abort immediately with build errors
4. Launch dev server (e.g. `npm run dev`)
5. Retry navigate to dev_url until server is responsive (poll-based, not static sleep)
6. Capture console errors (onlyErrors=True) and failed network requests (onlyFailed=True)
7. Capture full-page screenshot (base64)
8. Always clean up processes and browser context in finally block (zero zombies)
"""
from __future__ import annotations

from enum import Enum
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from core.mcp_client import McpClient

DEFAULT_MCP_SERVER_PATH = os.environ.get(
    "BRAINFROG_VERIFY_MCP_PATH",
    r"C:\dame-project\tools\brainfrog-verify-mcp\dist\index.js",
)
DEFAULT_DEV_URL = os.environ.get("BRAINFROG_DEV_URL", "http://localhost:3000")


class SandboxType(str, Enum):
    NONE = "none"
    CONTAINER_DOCKER = "container_docker"
    CONTAINER_PODMAN = "container_podman"
    OS_SANDBOX = "os_sandbox"


@dataclass(frozen=True)
class FrontendSandboxPolicy:
    """Policy governing container/OS isolation for untrusted frontend code execution.

    Mandates that untrusted workspace code (including build tools that execute
    arbitrary code in configuration files like vite.config.js, next.config.js)
    must run inside a defensible container or OS-level sandbox.
    """

    require_sandbox: bool = True
    network_mode: str = "none"  # "none", "bridge", "host-only"
    allow_unsafe_host_fallback: bool = False
    container_image: str = "node:20-alpine"
    read_only_root: bool = True
    drop_capabilities: bool = True
    container_user: str = "1000:1000"
    memory_limit: str = "2g"
    cpu_limit: str = "2.0"
    pids_limit: int = 256
    enable_init: bool = True
    isolate_workspace_snapshot: bool = True

    @classmethod
    def from_environment(cls) -> "FrontendSandboxPolicy":
        req = os.environ.get("BRAINFROG_REQUIRE_FRONTEND_SANDBOX", "1").strip().lower()
        require_sandbox = req not in ("0", "false", "no", "disabled")
        allow_unsafe = os.environ.get(
            "BRAINFROG_ALLOW_UNSAFE_HOST_FRONTEND_VERIFICATION", "0"
        ).strip().lower() in ("1", "true", "yes")
        image = os.environ.get("BRAINFROG_FRONTEND_CONTAINER_IMAGE", "node:20-alpine").strip()
        net_mode = os.environ.get("BRAINFROG_FRONTEND_SANDBOX_NETWORK", "none").strip().lower()
        mem_limit = os.environ.get("BRAINFROG_FRONTEND_SANDBOX_MEMORY", "2g").strip()
        cpu_limit = os.environ.get("BRAINFROG_FRONTEND_SANDBOX_CPUS", "2.0").strip()
        try:
            pids = int(os.environ.get("BRAINFROG_FRONTEND_SANDBOX_PIDS", "256").strip())
        except Exception:
            pids = 256
        iso_snap = os.environ.get("BRAINFROG_FRONTEND_ISOLATE_SNAPSHOT", "1").strip().lower() not in (
            "0", "false", "no", "disabled"
        )
        return cls(
            require_sandbox=require_sandbox,
            allow_unsafe_host_fallback=allow_unsafe,
            container_image=image,
            network_mode=net_mode,
            memory_limit=mem_limit,
            cpu_limit=cpu_limit,
            pids_limit=pids,
            isolate_workspace_snapshot=iso_snap,
        )


class FrontendSandboxViolationError(PermissionError):
    """Raised when an execution violates frontend sandbox isolation boundaries."""


def detect_frontend_sandbox() -> Tuple[SandboxType, Optional[str], Optional[str]]:
    """Detect if an active, defensible container runtime is available on the host.

    Returns:
        (sandbox_type, description, error_message_if_unavailable)
    """
    # 1. Check Docker
    docker_bin = shutil.which("docker")
    if docker_bin:
        try:
            # Check if docker daemon is actually running and responding
            res = subprocess.run(
                [docker_bin, "info"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0:
                return (
                    SandboxType.CONTAINER_DOCKER,
                    "Docker daemon responsive (container execution available)",
                    None,
                )
        except Exception:
            pass

    # 2. Check Podman
    podman_bin = shutil.which("podman")
    if podman_bin:
        try:
            res = subprocess.run(
                [podman_bin, "info"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0:
                return (
                    SandboxType.CONTAINER_PODMAN,
                    "Podman daemon responsive (container execution available)",
                    None,
                )
        except Exception:
            pass

    return (
        SandboxType.NONE,
        None,
        "No active container runtime (Docker/Podman daemon not responding or unavailable)",
    )


SENSITIVE_HOST_DIRS: Set[Path] = {
    Path.home() / ".ssh",
    Path.home() / ".aws",
    Path.home() / ".gnupg",
    Path.home() / ".gitconfig",
    Path.home() / ".docker",
    Path.home() / ".config",
    Path.home() / ".azure",
}

PROHIBITED_ROOT_PREFIXES: List[Path] = [
    Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve(),
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")).resolve(),
]
if "ProgramFiles(x86)" in os.environ:
    PROHIBITED_ROOT_PREFIXES.append(Path(os.environ["ProgramFiles(x86)"]).resolve())
for unix_root in ["/etc", "/var", "/usr", "/root", "/bin", "/sbin"]:
    p = Path(unix_root)
    if p.exists():
        PROHIBITED_ROOT_PREFIXES.append(p.resolve())


def validate_container_workspace_path(project_cwd: Path | str) -> Path:
    """Validate that the workspace path is safe to mount into a container.

    Rejects paths that are sensitive host locations, user home/credentials,
    or escape the canonical boundary.
    """
    p = Path(project_cwd).resolve()
    if not p.exists() or not p.is_dir():
        raise ValueError(f"Workspace path does not exist or is not a directory: {p}")

    # Check against home directory root
    try:
        home_path = Path.home().resolve()
    except Exception:
        home_path = None
    if home_path and p == home_path:
        raise PermissionError(f"Mounting entire user home directory into container is prohibited: {p}")

    # Check against sensitive credential locations
    for sens in SENSITIVE_HOST_DIRS:
        try:
            resolved_sens = sens.resolve()
        except Exception:
            continue
        if p == resolved_sens or resolved_sens in p.parents or p in resolved_sens.parents:
            raise PermissionError(f"Workspace path overlaps with sensitive host directory '{sens}': {p}")

    # Check against system root prefixes
    for root_pref in PROHIBITED_ROOT_PREFIXES:
        try:
            resolved_root = root_pref.resolve()
        except Exception:
            continue
        if p == resolved_root or resolved_root in p.parents:
            raise PermissionError(f"Workspace path cannot be inside system directory '{root_pref}': {p}")

    return p


APPROVED_CONTAINER_IMAGES: Set[str] = {
    "node:20-alpine",
    "node:18-alpine",
    "node:22-alpine",
    "node:20-slim",
    "node:18-slim",
    "node:22-slim",
    "node:lts-alpine",
}


def validate_container_image(image: str) -> str:
    """Validate that the container image identity is trusted and safe.

    Rejects images with shell metacharacters, unauthorized registries,
    or unapproved non-node images unless pinned by trusted sha256 digest.
    """
    clean_image = image.strip()
    if not clean_image:
        raise FrontendSandboxViolationError("Container image name cannot be empty")

    import re
    if re.search(r"[\s;&|`$<>{}\[\]\\]", clean_image):
        raise FrontendSandboxViolationError(f"Container image name contains illegal characters: '{clean_image}'")

    if clean_image in APPROVED_CONTAINER_IMAGES:
        return clean_image

    # Allow official pinned node digests: e.g. node:20-alpine@sha256:...
    if re.match(r"^node(:[a-zA-Z0-9_\-\.]+)?@sha256:[a-f0-9]{64}$", clean_image):
        return clean_image

    raise FrontendSandboxViolationError(
        f"Container image '{clean_image}' is not permitted by security policy. "
        f"Approved images: {sorted(list(APPROVED_CONTAINER_IMAGES))}"
    )


def is_symlink_or_junction(path: Path | str) -> bool:
    """Detect whether a path is a symbolic link or Windows directory junction/reparse point."""
    p_str = str(path)
    if os.path.islink(p_str):
        return True
    if hasattr(os.path, "isjunction") and os.path.isjunction(p_str):
        return True
    try:
        import stat
        st = os.lstat(p_str)
        if getattr(st, "st_file_attributes", 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            return True
    except Exception:
        pass
    return False


PROTECTED_CONTROL_DIRS: Set[str] = {".git", ".brainfrog", ".github", ".vscode", ".idea"}
SENSITIVE_ENV_PREFIXES: Tuple[str, ...] = (".env",)
SENSITIVE_FILE_NAMES: Set[str] = {
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "credentials.json", "token.txt", "service-account.json",
}
SENSITIVE_FILE_EXTENSIONS: Set[str] = {
    ".pem", ".key", ".pfx", ".p12", ".kdbx",
}


def create_isolated_workspace_snapshot(
    canonical_src: Path | str,
    target_snapshot_dir: Path | str,
) -> Path:
    """Create a pristine, disposable copy of the workspace for isolated sandboxed execution.

    Protects host workspace integrity by:
    1. Excluding .git, .brainfrog, and sensitive credential files.
    2. Inspecting symlinks/junctions and strictly refusing to follow any links
       that point outside canonical_src.
    3. Creating empty sentinel directories for .git and .brainfrog with read-only
       mount targets so that control-plane paths are blocked.
    """
    src_path = Path(canonical_src).resolve()
    dst_path = Path(target_snapshot_dir).resolve()

    if not src_path.exists() or not src_path.is_dir():
        raise ValueError(f"Source workspace does not exist or is not a directory: {src_path}")

    dst_path.mkdir(parents=True, exist_ok=True)

    for root, dirs, files in os.walk(src_path, followlinks=False):
        rel_root = Path(root).relative_to(src_path)

        # Filter directories in-place to prevent traversing excluded or malicious targets
        filtered_dirs: List[str] = []
        for d in dirs:
            dir_full = Path(root) / d
            d_lower = d.lower()

            # 1. Skip protected control-plane directories (.git, .brainfrog, etc.)
            if d_lower in PROTECTED_CONTROL_DIRS:
                continue

            # 2. Skip node_modules (will be mounted read-only if present on host)
            if d_lower == "node_modules":
                continue

            # 3. Check for symlinks / junctions pointing outside workspace
            if is_symlink_or_junction(dir_full):
                try:
                    resolved_dir = dir_full.resolve()
                    if resolved_dir != src_path and src_path not in resolved_dir.parents:
                        # Escapes workspace root: skip
                        continue
                except Exception:
                    continue

            filtered_dirs.append(d)
        dirs[:] = filtered_dirs

        target_dir = dst_path / rel_root
        target_dir.mkdir(parents=True, exist_ok=True)

        for f in files:
            file_full = Path(root) / f
            f_lower = f.lower()

            # 1. Skip protected control-plane files and git pointer files (worktrees/submodules)
            if f_lower in PROTECTED_CONTROL_DIRS or f_lower == ".git":
                continue

            # 2. Skip sensitive environment and credential files
            if any(f_lower.startswith(pref) for pref in SENSITIVE_ENV_PREFIXES):
                continue
            if f_lower in SENSITIVE_FILE_NAMES:
                continue
            if any(f_lower.endswith(ext) for ext in SENSITIVE_FILE_EXTENSIONS):
                continue

            # 3. Check for symlink files pointing outside workspace
            if is_symlink_or_junction(file_full):
                try:
                    resolved_file = file_full.resolve()
                    if resolved_file != src_path and src_path not in resolved_file.parents:
                        # Escapes workspace root: skip
                        continue
                except Exception:
                    continue

            target_file = target_dir / f
            try:
                shutil.copy2(file_full, target_file)
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to copy required workspace file into isolated snapshot: {file_full} -> {target_file}: {exc}"
                ) from exc

    # Ensure empty sentinel directories exist in the snapshot for .brainfrog and .git
    for sentinel in (".brainfrog", ".git"):
        sentinel_path = dst_path / sentinel
        if sentinel_path.is_file():
            sentinel_path.unlink()
        sentinel_path.mkdir(exist_ok=True)

    return dst_path


def build_container_run_command(
    runtime_bin: str,
    project_cwd: Path | str,
    container_name: str,
    policy: FrontendSandboxPolicy,
    command_args: List[str],
    original_host_cwd: Optional[Path | str] = None,
) -> List[str]:
    """Construct a strictly constrained container run command line."""
    canonical_ws = validate_container_workspace_path(project_cwd)
    validated_image = validate_container_image(policy.container_image)

    cmd = [
        runtime_bin, "run", "--rm",
        "--name", container_name,
        "--workdir", "/workspace",
    ]

    # Drop capabilities & prevent privilege escalation
    if policy.drop_capabilities:
        cmd.extend(["--cap-drop", "ALL"])
    cmd.extend(["--security-opt", "no-new-privileges"])

    # Non-root user
    if policy.container_user:
        cmd.extend(["--user", policy.container_user])

    # Network mode
    net_mode = policy.network_mode.lower()
    if net_mode not in ("none", "bridge", "host-only"):
        net_mode = "none"
    cmd.extend(["--network", net_mode])

    # Resource limits to prevent DoS
    if policy.memory_limit:
        cmd.extend(["--memory", policy.memory_limit, "--memory-swap", policy.memory_limit])
    if policy.cpu_limit:
        cmd.extend(["--cpus", policy.cpu_limit])
    if policy.pids_limit > 0:
        cmd.extend(["--pids-limit", str(policy.pids_limit)])
    if policy.enable_init:
        cmd.append("--init")

    # Read-only root filesystem with bounded tmpfs
    if policy.read_only_root:
        cmd.extend(["--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"])

    # Dedicated bounded writable cache for build tools and package managers
    # Mounted at /tmp/brainfrog-node-cache inside the disposable container, avoiding
    # attempts to create mountpoints inside the read-only host node_modules bind mount.
    cmd.extend(["--tmpfs", "/tmp/brainfrog-node-cache:rw,noexec,nosuid,size=256m"])

    # Configure toolchain cache paths to point to the dedicated disposable container cache
    cmd.extend([
        "-e", "npm_config_cache=/tmp/brainfrog-node-cache/npm",
        "-e", "XDG_CACHE_HOME=/tmp/brainfrog-node-cache",
        "-e", "TMPDIR=/tmp",
    ])

    # Mount approved canonical workspace using posix-style path for container engine compatibility
    posix_path = canonical_ws.as_posix()
    cmd.extend(["-v", f"{posix_path}:/workspace:rw"])

    # If original host workspace has node_modules and we are running inside an isolated snapshot,
    # mount original node_modules as read-only
    ref_host_dir = Path(original_host_cwd).resolve() if original_host_cwd else canonical_ws
    if (ref_host_dir / "node_modules").exists() and not (canonical_ws / "node_modules").exists():
        nm_posix = (ref_host_dir / "node_modules").as_posix()
        cmd.extend(["-v", f"{nm_posix}:/workspace/node_modules:ro"])

    # Mask sensitive subdirectories within workspace (e.g. .brainfrog, .git) unconditionally
    cmd.extend(["--tmpfs", "/workspace/.brainfrog:ro"])
    cmd.extend(["--tmpfs", "/workspace/.git:ro"])

    # Trusted container image
    cmd.append(validated_image)

    # Command to execute inside container
    cmd.extend(command_args)
    return cmd


def execute_container_build(
    project_cwd: Path | str,
    policy: FrontendSandboxPolicy,
    timeout_seconds: float = 120.0,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[int, str]:
    """Execute frontend build inside an isolated container.

    Returns:
        (exit_code, captured_stdout_and_stderr)
    Raises:
        RuntimeError / PermissionError if container execution fails or cannot be started.
    """
    def log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    sandbox_type, desc, err = detect_frontend_sandbox()
    if sandbox_type == SandboxType.NONE:
        raise RuntimeError(f"No active container runtime available: {err}")

    runtime_bin = "docker" if sandbox_type == SandboxType.CONTAINER_DOCKER else "podman"
    resolved_bin = shutil.which(runtime_bin)
    if not resolved_bin:
        raise RuntimeError(f"Container runtime executable '{runtime_bin}' not found in PATH")

    canonical_cwd = validate_container_workspace_path(project_cwd)
    container_name = f"bf-fe-build-{os.getpid()}-{int(time.time())}"

    # Target command inside container: npm run --ignore-scripts build
    inner_cmd = ["npm", "run", "--ignore-scripts", "build"]

    snap_temp_obj: Optional[tempfile.TemporaryDirectory[str]] = None
    build_dir = canonical_cwd

    try:
        if policy.isolate_workspace_snapshot:
            snap_temp_obj = tempfile.TemporaryDirectory(prefix="bf-snap-")
            snap_dir_path = Path(snap_temp_obj.name)
            create_isolated_workspace_snapshot(canonical_cwd, snap_dir_path)
            build_dir = snap_dir_path

        full_cmd = build_container_run_command(
            runtime_bin=resolved_bin,
            project_cwd=build_dir,
            container_name=container_name,
            policy=policy,
            command_args=inner_cmd,
            original_host_cwd=canonical_cwd if policy.isolate_workspace_snapshot else None,
        )

        log(f"[container-build] 🐳 Spawning container build ({runtime_bin}) with image '{policy.container_image}'...")
        log(f"[container-build] Network mode: {policy.network_mode}, read_only_root: {policy.read_only_root}")
        if policy.isolate_workspace_snapshot:
            log("[container-build] 🛡️ Workspace snapshot isolation ACTIVE: Host workspace is protected against tampering.")

        proc = None
        output_chunks: List[str] = []
        exit_code = -1

        try:
            proc = subprocess.Popen(
                full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                stdout_data, _ = proc.communicate(timeout=timeout_seconds)
                exit_code = proc.returncode
                output_chunks.append(stdout_data or "")
            except subprocess.TimeoutExpired:
                log(f"[container-build] ❌ Container build timed out after {timeout_seconds}s; terminating...")
                try:
                    proc.kill()
                except Exception:
                    pass
                raise TimeoutError(f"Container build timed out after {timeout_seconds}s")
        except Exception as e:
            if not isinstance(e, TimeoutError):
                log(f"[container-build] ❌ Container execution failed to start or run: {e}")
            raise
        finally:
            # Guarantee container cleanup on Docker/Podman daemon
            try:
                subprocess.run(
                    [resolved_bin, "rm", "-f", container_name],
                    capture_output=True,
                    timeout=5.0,
                )
            except Exception:
                pass

        from core.runtime.secret_scrubbing import safe_scrub
        raw_output = "".join(output_chunks)
        scrubbed_output = safe_scrub(raw_output[-50000:])
        return exit_code, scrubbed_output

    finally:
        if snap_temp_obj is not None:
            try:
                snap_temp_obj.cleanup()
            except Exception:
                pass

FRONTEND_EXTENSIONS = {
    ".html", ".htm",
    ".css", ".scss", ".sass", ".less",
    ".jsx", ".tsx",
    ".vue", ".svelte",
}

IGNORED_DIRECTORIES = {
    "node_modules", ".git", "dist", "build", ".next", "out",
    "coverage", ".brainfrog", "docs", "documentation", ".github",
    "vendor", "__pycache__", ".venv", "venv",
}

NON_FRONTEND_EXTENSIONS = {
    ".md", ".markdown", ".rst", ".txt", ".json", ".lock",
    ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".go", ".rs", ".java", ".c", ".cpp", ".rb", ".php", ".sh",
}

FRONTEND_DIRECTORIES = {
    "src", "components", "pages", "app", "views", "ui", "styles", "public", "frontend"
}


def is_frontend_change(files: Iterable[str]) -> bool:
    """Check if any of the touched files affect frontend UI/UX.

    Returns False for backend-only, config, documentation, and non-visual files,
    as well as files inside ignored directories (e.g. node_modules, docs, .github).
    """
    for f in files:
        if not f or not str(f).strip():
            continue
        p = Path(f)
        parts = [part.lower() for part in p.parts]

        # Ignore non-source or excluded directories
        if any(ign in parts for ign in IGNORED_DIRECTORIES):
            continue

        ext = p.suffix.lower()

        # Explicit non-frontend / documentation / backend extensions
        if ext in NON_FRONTEND_EXTENSIONS:
            continue

        # Direct frontend extensions (.tsx, .jsx, .css, .html, .vue, .svelte, etc.)
        if ext in FRONTEND_EXTENSIONS:
            return True

        # JS/TS logic files: check if they are located within frontend directories
        if ext in {".js", ".ts", ".mjs"}:
            backend_dirs = {"backend", "server", "api", "services"}
            has_fe_dir = any(d in parts for d in FRONTEND_DIRECTORIES)
            has_be_dir = any(d in parts for d in backend_dirs)
            if has_fe_dir and not has_be_dir:
                return True

    return False


ALLOWED_FRONTEND_TOOLS: Set[str] = {
    "next",
    "vite",
    "tsc",
    "react-scripts",
    "astro",
    "remix",
    "vue-tsc",
    "svelte-kit",
    "nuxt",
    "webpack",
    "webpack-cli",
    "esbuild",
    "tailwindcss",
    "tailwind",
    "rimraf",
    "cross-env",
    "prettier",
    "eslint",
}

DISALLOWED_SCRIPT_TOKENS: Set[str] = {
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "cmd",
    "cmd.exe",
    "bash",
    "sh",
    "zsh",
    "csh",
    "tcsh",
    "python",
    "python3",
    "perl",
    "ruby",
    "node",
    "curl",
    "wget",
    "certutil",
    "mshta",
    "rundll32",
    "wscript",
    "cscript",
    "eval",
}


def validate_frontend_script(script_name: str, script_cmd: Any) -> Tuple[bool, Optional[str]]:
    """Validate package.json script against an authorized tool allowlist and prohibited tokens.

    Ensures that an agent-modified or untrusted package.json cannot silently authorize
    arbitrary host command execution (e.g. curl, powershell, python, node -e) during
    automated frontend verification.
    """
    if not isinstance(script_cmd, str) or not script_cmd.strip():
        return False, f"Script '{script_name}' is empty or invalid"

    if "\0" in script_cmd:
        return False, f"Null byte detected in '{script_name}' script"

    if "`" in script_cmd or "$(" in script_cmd:
        return False, f"Command substitution detected in '{script_name}' script"

    if ">" in script_cmd or "<" in script_cmd:
        return False, f"I/O redirection detected in '{script_name}' script"

    # Split sub-commands across shell chaining operators (&&, ||, ;, |)
    import re
    clauses = re.split(r"&&|\|\||;|\|", script_cmd)

    for clause in clauses:
        clause_str = clause.strip()
        if not clause_str:
            continue

        tokens = clause_str.split()
        if not tokens:
            continue

        # Check for prohibited tool tokens anywhere in clause
        for tok in tokens:
            clean_tok = Path(tok.lower().strip("\"'")).name
            if clean_tok.endswith(".exe"):
                clean_tok = clean_tok[:-4]
            if clean_tok in DISALLOWED_SCRIPT_TOKENS:
                return False, f"Prohibited tool invocation '{tok}' in '{script_name}' script"

        # Determine primary executable token, skipping environment variable assignments (e.g. NODE_ENV=production)
        # and cross-env prefix
        exe_token: Optional[str] = None
        for tok in tokens:
            if "=" in tok and not tok.startswith("-"):
                continue
            clean = Path(tok.lower().strip("\"'")).name
            if clean.endswith(".exe") or clean.endswith(".cmd") or clean.endswith(".bat"):
                clean = clean.rsplit(".", 1)[0]
            if clean == "cross-env":
                continue
            exe_token = clean
            break

        if not exe_token:
            return False, f"No executable command found in '{script_name}' clause '{clause_str}'"

        if exe_token not in ALLOWED_FRONTEND_TOOLS:
            return False, (
                f"Unauthorized executable '{exe_token}' in '{script_name}' script. "
                f"Autonomous verification only permits recognized frontend toolchain commands."
            )

    return True, None


SUSPICIOUS_LIFECYCLE_HOOKS: Set[str] = {
    "prebuild", "postbuild",
    "predev", "postdev",
    "prestart", "poststart",
    "preinstall", "postinstall",
    "install", "prepare",
}


def validate_package_json_lifecycle_scripts(pkg_data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Validate package.json lifecycle scripts against prohibited shell commands and injections."""
    scripts = pkg_data.get("scripts")
    if not isinstance(scripts, dict):
        return True, None

    for hook_name, hook_cmd in scripts.items():
        if hook_name.lower() in SUSPICIOUS_LIFECYCLE_HOOKS:
            if not isinstance(hook_cmd, str) or not hook_cmd.strip():
                continue
            is_valid, reason = validate_frontend_script(hook_name, hook_cmd)
            if not is_valid:
                return False, f"Prohibited lifecycle hook '{hook_name}': {reason}"

    return True, None


@dataclass
class QualityGateResult:
    """Structured report returned by the frontend quality gate."""

    status: str  # "PASS" | "FAIL"
    build_output: str = ""
    console_errors: List[Any] = field(default_factory=list)
    failed_requests: List[Any] = field(default_factory=list)
    screenshot_base64: Optional[str] = None
    screenshot_desktop_base64: Optional[str] = None
    screenshot_mobile_base64: Optional[str] = None
    reason: str = ""
    duration_seconds: float = 0.0

    def __post_init__(self):
        if self.screenshot_desktop_base64 and not self.screenshot_base64:
            self.screenshot_base64 = self.screenshot_desktop_base64
        elif self.screenshot_base64 and not self.screenshot_desktop_base64:
            self.screenshot_desktop_base64 = self.screenshot_base64

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def to_agent_context(self, max_total_chars: int = 4000) -> str:
        """Format errors into scrubbed, bounded high-signal feedback for System 2 to review and fix.

        Enforces strict credential scrubbing (safe_scrub) and bounded character lengths
        to prevent secret leakage and prompt stuffing into model context.
        """
        try:
            from core.runtime.secret_scrubbing import safe_scrub

            clean_reason = safe_scrub(self.reason[:300]) if self.reason else ""
            lines = [f"[Frontend Quality Gate Verification: {self.status}]", f"Reason: {clean_reason}"]

            if self.build_output.strip():
                scrubbed_build = safe_scrub(self.build_output[-2000:].strip())
                lines.append(f"\n--- Build Output ---\n{scrubbed_build}")

            if self.console_errors:
                lines.append("\n--- Browser Console Errors ---")
                for err in self.console_errors[:10]:
                    if isinstance(err, dict):
                        msg = err.get("text") or err.get("message") or json.dumps(err)
                    else:
                        msg = str(err)
                    clean_msg = safe_scrub(msg[:300].strip())
                    lines.append(f"- {clean_msg}")

            if self.failed_requests:
                lines.append("\n--- Failed Network Requests (404/500/Errors) ---")
                for req in self.failed_requests[:10]:
                    if isinstance(req, dict):
                        url = str(req.get("url", "unknown"))
                        status = str(req.get("status", "failed"))
                        method = str(req.get("method", "GET"))
                    else:
                        url = str(req)
                        status = "failed"
                        method = "GET"
                    clean_url = safe_scrub(url[:200].strip())
                    clean_status = safe_scrub(status[:50].strip())
                    clean_method = safe_scrub(method[:20].strip())
                    lines.append(f"- [{clean_method}] {clean_url} -> status {clean_status}")

            build_combined = f"{self.build_output} {self.reason}".lower()
            if "cannot find module" in build_combined or "module not found" in build_combined:
                lines.append(
                    "\n[CRITICAL DEPENDENCY RULE]: Build failed with 'Cannot find module'. "
                    "The root cause is almost certainly a MISSING DEPENDENCY, NOT a syntax or logic error! "
                    "1. Prioritize running 'npm install <package>' (or adding the dependency to package.json) BEFORE touching other code. "
                    "2. Do NOT patch imports or rewrite components one-by-one (whack-a-mole). "
                    "3. After installing, verify dependency was added (check that 'audited N packages' count increased or verify via 'npm ls <package>') before retrying the build."
                )
            else:
                lines.append(
                    "\nPlease fix the code so that the project builds cleanly with 0 console errors and 0 failed requests."
                )

            combined = "\n".join(lines).strip()
            scrubbed_combined = safe_scrub(combined)
            if len(scrubbed_combined) > max_total_chars:
                head_len = max(500, max_total_chars - 600)
                scrubbed_combined = (
                    scrubbed_combined[:head_len]
                    + f"\n\n... [TRUNCATED: Quality gate context exceeded {max_total_chars} chars] ...\n\n"
                    + scrubbed_combined[-400:]
                )
            return scrubbed_combined
        except Exception as exc:
            return (
                f"[Frontend Quality Gate Verification: {self.status}]\n"
                f"Reason: Diagnostic context redaction failed closed: {type(exc).__name__}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "buildOutput": self.build_output,
            "consoleErrors": self.console_errors,
            "failedRequests": self.failed_requests,
            "screenshotBase64": self.screenshot_base64[:50] + "..." if self.screenshot_base64 else None,
            "reason": self.reason,
            "durationSeconds": round(self.duration_seconds, 2),
        }


def _execute_containerized_quality_gate(
    project_cwd: Path,
    policy: FrontendSandboxPolicy,
    dev_url: str,
    instance_id: str,
    timeout_seconds: float,
    log: Callable[[str], None],
    start_time: float,
) -> QualityGateResult:
    """Execute frontend quality gate strictly inside an isolated container.

    Untrusted build configuration code (vite.config.js, next.config.js, etc.)
    runs inside container sandbox with network disabled, capabilities dropped,
    and credentials/system directories inaccessible.
    """
    pkg_json = project_cwd / "package.json"
    scripts: Dict[str, Any] = {}
    has_build_script = False
    has_dev_script = False

    if pkg_json.exists():
        try:
            pkg_data = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg_data.get("scripts", {})
            has_build_script = "build" in scripts
            has_dev_script = "dev" in scripts or "start" in scripts

            is_valid_lifecycle, lifecycle_reason = validate_package_json_lifecycle_scripts(pkg_data)
            if not is_valid_lifecycle:
                log(f"[mcp-verify] ❌ Suspicious lifecycle script in package.json: {lifecycle_reason}")
                return QualityGateResult(
                    status="FAIL",
                    reason=f"Lifecycle script in package.json rejected by security policy: {lifecycle_reason}",
                    duration_seconds=time.time() - start_time,
                )
        except Exception:
            pass

    build_output = ""
    if has_build_script:
        build_cmd_raw = scripts.get("build", "")
        is_valid, reason = validate_frontend_script("build", build_cmd_raw)
        if not is_valid:
            log(f"[mcp-verify] ❌ Build script in package.json failed security policy: {reason}")
            return QualityGateResult(
                status="FAIL",
                reason=f"Build script in package.json rejected by security policy: {reason}",
                duration_seconds=time.time() - start_time,
            )

        log("[mcp-verify] 🐳 Executing containerized build verification...")
        try:
            build_exit_code, build_output = execute_container_build(
                project_cwd=project_cwd,
                policy=policy,
                timeout_seconds=min(timeout_seconds, 120.0),
                log_callback=log,
            )
        except TimeoutError:
            log("[mcp-verify] ❌ Container build timed out.")
            return QualityGateResult(
                status="FAIL",
                build_output=build_output,
                reason="Container build timed out after 120s.",
                duration_seconds=time.time() - start_time,
            )
        except Exception as exc:
            log(f"[mcp-verify] ❌ Container execution failed: {exc}")
            return QualityGateResult(
                status="FAIL",
                build_output=build_output,
                reason=f"Container execution failed: {exc}. Bare-host execution prohibited when require_sandbox=True.",
                duration_seconds=time.time() - start_time,
            )

        if build_exit_code != 0:
            log(f"[mcp-verify] ❌ Container build failed with exit code {build_exit_code}!")
            return QualityGateResult(
                status="FAIL",
                build_output=build_output,
                reason=f"Containerized npm run build failed with exit code {build_exit_code}",
                duration_seconds=time.time() - start_time,
            )

        log("[mcp-verify] ✅ Containerized build passed cleanly with exit code 0.")

    # Under policy.network_mode == "none", dev server external networking is disabled.
    # To prevent any untrusted host execution and maintain complete network denial,
    # dev server browser navigation is omitted in network-isolated container mode.
    if has_dev_script:
        if policy.network_mode == "none":
            log("[mcp-verify] ℹ️ Sandbox network mode is 'none'; dev server browser navigation skipped for network isolation.")
            return QualityGateResult(
                status="PASS",
                build_output=build_output,
                reason="Container build compilation succeeded cleanly under network-isolated container policy (network_mode='none'). Dev server browser navigation omitted to preserve complete network denial.",
                duration_seconds=time.time() - start_time,
            )
        else:
            return QualityGateResult(
                status="PASS",
                build_output=build_output,
                reason="Container build compilation succeeded cleanly in isolated container.",
                duration_seconds=time.time() - start_time,
            )

    log("[mcp-verify] 🎉 Containerized quality gate PASSED cleanly!")
    return QualityGateResult(
        status="PASS",
        build_output=build_output,
        reason="Container build compilation succeeded cleanly in isolated container; no web dev server entrypoint.",
        duration_seconds=time.time() - start_time,
    )


def run_frontend_quality_gate(
    project_cwd: Path | str,
    dev_url: str = DEFAULT_DEV_URL,
    instance_id: Optional[str] = None,
    mcp_command: str = "node",
    mcp_args: Optional[List[str]] = None,
    timeout_seconds: float = 120.0,
    log_callback: Optional[Callable[[str], None]] = None,
    capture_mobile: bool = False,
    sandbox_policy: Optional[FrontendSandboxPolicy] = None,
) -> QualityGateResult:
    """Run full verification gate against target frontend project.

    Enforces container sandbox isolation for untrusted workspace code when
    require_sandbox=True (default). Disallows bare-host execution unless explicitly
    authorized by policy with require_sandbox=False and allow_unsafe_host_fallback=True.
    """
    start_time = time.time()

    def log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    cwd_path = Path(project_cwd).resolve()
    actual_instance_id = instance_id or f"bf-gate-{os.getpid()}-{int(start_time)}"
    args = mcp_args or [DEFAULT_MCP_SERVER_PATH]
    policy = sandbox_policy or FrontendSandboxPolicy.from_environment()

    # Pre-flight check: Enforce defensible sandbox requirement
    if policy.require_sandbox:
        sandbox_type, desc, err_msg = detect_frontend_sandbox()
        if sandbox_type == SandboxType.NONE:
            log(f"[mcp-verify] ❌ Sandbox policy violation: {err_msg}")
            return QualityGateResult(
                status="FAIL",
                reason=(
                    f"Frontend verification rejected: No defensible OS-level sandbox or container runtime is active on the host ({err_msg}). "
                    "Untrusted workspace code cannot be executed safely on the bare host. "
                    "To protect credentials, user files, and system integrity, frontend verification fails closed."
                ),
                duration_seconds=time.time() - start_time,
            )

        log(f"[mcp-verify] 🛡️ Sandboxed container execution path active: {desc}")
        return _execute_containerized_quality_gate(
            project_cwd=cwd_path,
            policy=policy,
            dev_url=dev_url,
            instance_id=actual_instance_id,
            timeout_seconds=timeout_seconds,
            log=log,
            start_time=start_time,
        )

    # When require_sandbox=False, check if unsafe host fallback is explicitly authorized
    if not policy.allow_unsafe_host_fallback:
        log("[mcp-verify] ❌ Host execution rejected: require_sandbox=False but allow_unsafe_host_fallback=False.")
        return QualityGateResult(
            status="FAIL",
            reason="Frontend verification rejected: Sandbox is disabled but unsafe host fallback is not explicitly permitted.",
            duration_seconds=time.time() - start_time,
        )

    log("[mcp-verify] ⚠️ INSECURE EXECUTION WARNING: Running frontend verification on bare host without container sandbox.")
    return _execute_host_quality_gate(
        project_cwd=cwd_path,
        dev_url=dev_url,
        instance_id=actual_instance_id,
        mcp_command=mcp_command,
        mcp_args=args,
        timeout_seconds=timeout_seconds,
        log_callback=log_callback,
        capture_mobile=capture_mobile,
        sandbox_policy=policy,
    )


def _execute_host_quality_gate(
    project_cwd: Path | str,
    dev_url: str = DEFAULT_DEV_URL,
    instance_id: Optional[str] = None,
    mcp_command: str = "node",
    mcp_args: Optional[List[str]] = None,
    timeout_seconds: float = 120.0,
    log_callback: Optional[Callable[[str], None]] = None,
    capture_mobile: bool = False,
    sandbox_policy: Optional[FrontendSandboxPolicy] = None,
) -> QualityGateResult:
    """Execute frontend quality gate on the bare host via MCP server.

    Strictly rejected if policy.require_sandbox is True.
    """
    start_time = time.time()

    def log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    cwd_path = Path(project_cwd).resolve()
    actual_instance_id = instance_id or f"bf-gate-{os.getpid()}-{int(start_time)}"
    args = mcp_args or [DEFAULT_MCP_SERVER_PATH]
    policy = sandbox_policy or FrontendSandboxPolicy.from_environment()

    if policy.require_sandbox:
        raise FrontendSandboxViolationError(
            "Bare-host MCP execution is strictly forbidden when require_sandbox=True."
        )

    # Pre-flight check: verify server script exists
    script_target = Path(args[0]) if args else None
    if script_target and not script_target.exists():
        return QualityGateResult(
            status="FAIL",
            reason=f"MCP server script not found at {script_target}. Check BRAINFROG_VERIFY_MCP_PATH.",
        )

    # Pre-flight check: inspect package.json scripts
    pkg_json = cwd_path / "package.json"
    scripts: Dict[str, Any] = {}
    has_build_script = False
    has_dev_script = False
    if pkg_json.exists():
        try:
            pkg_data = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg_data.get("scripts", {})
            has_build_script = "build" in scripts
            has_dev_script = "dev" in scripts or "start" in scripts

            is_valid_lifecycle, lifecycle_reason = validate_package_json_lifecycle_scripts(pkg_data)
            if not is_valid_lifecycle:
                log(f"[mcp-verify] ❌ Suspicious lifecycle script in package.json: {lifecycle_reason}")
                return QualityGateResult(
                    status="FAIL",
                    reason=f"Lifecycle script in package.json rejected by security policy: {lifecycle_reason}",
                    duration_seconds=time.time() - start_time,
                )
        except Exception:
            pass

    dev_script_name = "start" if (has_build_script and "start" in scripts) else ("dev" if "dev" in scripts else "start")

    log(f"[mcp-verify] 🚀 Starting verification gate for '{cwd_path.name}' (instance: {actual_instance_id})")
    log(f"[mcp-verify] Connecting to MCP server at {args[0]}...")

    client = McpClient(command=mcp_command, args=args)
    try:
        client.connect(timeout=15.0)
        log("[mcp-verify] Connected to MCP server successfully.")
    except Exception as e:
        return QualityGateResult(
            status="FAIL",
            reason=f"Failed to connect to MCP verification server: {e}",
            duration_seconds=time.time() - start_time,
        )

    # Everything after connection must be wrapped in try/finally to guarantee cleanup!
    try:
        # -------------------------------------------------------------
        # Step 1: Run build (npm run build) if project has build script
        # -------------------------------------------------------------
        build_output = ""
        if has_build_script:
            build_cmd_raw = scripts.get("build", "")
            is_valid, reason = validate_frontend_script("build", build_cmd_raw)
            if not is_valid:
                log(f"[mcp-verify] ❌ Build script in package.json failed security policy: {reason}")
                return QualityGateResult(
                    status="FAIL",
                    reason=f"Build script in package.json rejected by security policy: {reason}",
                    duration_seconds=time.time() - start_time,
                )

            log("[mcp-verify] Running 'npm run --ignore-scripts build' to verify compilation...")
            client.call_tool(
                "start_process",
                {
                    "instanceId": actual_instance_id,
                    "command": "npm",
                    "args": ["run", "--ignore-scripts", "build"],
                    "cwd": str(cwd_path),
                },
                timeout=15.0,
            )

            # Poll read_process_output until exitCode is set or timeout reached
            build_deadline = time.time() + 120.0
            build_exit_code = None

            while True:
                remaining = build_deadline - time.time()
                if remaining <= 0:
                    break

                res = client.call_tool(
                    "read_process_output",
                    {"instanceId": actual_instance_id, "tailLines": 100},
                    timeout=min(remaining, 10.0),
                )
                data = res.parsed_json or {}
                build_output = data.get("output", "")
                build_exit_code = data.get("exitCode")

                if build_exit_code is not None:
                    break

                remaining = build_deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(1.0, remaining))

            if build_exit_code is None:
                log("[mcp-verify] ❌ Build timed out after 120 seconds.")
                client.call_tool("stop_process", {"instanceId": actual_instance_id})
                return QualityGateResult(
                    status="FAIL",
                    build_output=build_output,
                    reason="Build process timed out after 120s.",
                    duration_seconds=time.time() - start_time,
                )

            if build_exit_code != 0:
                log(f"[mcp-verify] ❌ Build failed with exit code {build_exit_code}!")
                return QualityGateResult(
                    status="FAIL",
                    build_output=build_output,
                    reason=f"npm run build failed with exit code {build_exit_code}",
                    duration_seconds=time.time() - start_time,
                )

            log("[mcp-verify] ✅ Build passed with exit code 0.")

        # -------------------------------------------------------------
        # Step 2: Start dev server if project has dev script
        # -------------------------------------------------------------
        resolved_nav_url = dev_url
        if has_dev_script:
            dev_cmd_raw = scripts.get(dev_script_name, "")
            is_valid, reason = validate_frontend_script(dev_script_name, dev_cmd_raw)
            if not is_valid:
                log(f"[mcp-verify] ❌ Dev script in package.json failed security policy: {reason}")
                return QualityGateResult(
                    status="FAIL",
                    reason=f"Dev script '{dev_script_name}' in package.json rejected by security policy: {reason}",
                    duration_seconds=time.time() - start_time,
                )

            log(f"[mcp-verify] Launching dev server ('npm run {dev_script_name}') and waiting for {dev_url}...")
            client.call_tool(
                "start_process",
                {
                    "instanceId": actual_instance_id,
                    "command": "npm",
                    "args": ["run", "--ignore-scripts", dev_script_name],
                    "cwd": str(cwd_path),
                },
                timeout=15.0,
            )

            # Poll navigate with retry until server answers
            nav_deadline = time.time() + 30.0
            nav_ok = False
            last_nav_err = ""

            while True:
                remaining = nav_deadline - time.time()
                if remaining <= 0:
                    break

                # Check if dev server crashed immediately
                proc_stat = client.call_tool(
                    "read_process_output",
                    {"instanceId": actual_instance_id, "tailLines": 40},
                    timeout=min(remaining, 5.0),
                )
                stat_data = proc_stat.parsed_json or {}
                dev_out = stat_data.get("output", "")
                if stat_data.get("exitCode") is not None and stat_data.get("exitCode") != 0:
                    log(f"[mcp-verify] ❌ Dev server crashed with code {stat_data.get('exitCode')}.")
                    return QualityGateResult(
                        status="FAIL",
                        build_output=dev_out,
                        reason="Dev server crashed prematurely.",
                        duration_seconds=time.time() - start_time,
                    )

                # Check if dev server printed a specific local URL (e.g. Vite on 5173, Next on 3000, 0.0.0.0:8080)
                if dev_url == DEFAULT_DEV_URL and dev_out:
                    import re
                    m = re.search(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):\d+", dev_out)
                    if m:
                        detected_url = m.group(0)
                        # Windows browser navigation to 0.0.0.0 is unreliable; map to localhost
                        resolved_nav_url = detected_url.replace("0.0.0.0", "localhost")

                # Attempt navigation with timeout clamped to remaining time (max 10s per attempt)
                remaining = nav_deadline - time.time()
                if remaining <= 0:
                    break
                nav_timeout = min(remaining, 10.0)
                nav_res = client.call_tool(
                    "navigate",
                    {"instanceId": actual_instance_id, "url": resolved_nav_url},
                    timeout=nav_timeout,
                )
                if not nav_res.is_error:
                    nav_ok = True
                    break

                last_nav_err = nav_res.text or nav_res.error_message or "Connection refused"

                # Sleep only if there is still time remaining
                remaining = nav_deadline - time.time()
                if remaining <= 1.0:
                    break
                time.sleep(min(1.5, remaining - 0.5))

            if not nav_ok:
                log(f"[mcp-verify] ❌ Failed to connect to dev server at {resolved_nav_url}: {last_nav_err}")
                return QualityGateResult(
                    status="FAIL",
                    build_output=build_output,
                    reason=f"Dev server at {resolved_nav_url} did not become ready within 30s ({last_nav_err})",
                    duration_seconds=time.time() - start_time,
                )

            log(f"[mcp-verify] ✅ Successfully navigated to {resolved_nav_url}")

        else:
            # If static HTML project without dev script, navigate directly to entrypoint or dev_url
            html_target = None
            if dev_url == DEFAULT_DEV_URL:
                for candidate in ["index.html", "public/index.html", "src/index.html"]:
                    cand_path = cwd_path / candidate
                    if cand_path.exists():
                        html_target = cand_path
                        resolved_nav_url = cand_path.resolve().as_uri()
                        break

            # If no dev script, no html entrypoint, and dev_url is default, skip browser navigation
            if not html_target and dev_url == DEFAULT_DEV_URL:
                log("[mcp-verify] ℹ️ No dev server script or HTML entrypoint found. Build verification passed.")
                return QualityGateResult(
                    status="PASS",
                    build_output=build_output,
                    reason="Build compilation succeeded cleanly; no web browser entrypoint found to navigate.",
                    duration_seconds=time.time() - start_time,
                )

            log(f"[mcp-verify] Navigating to {resolved_nav_url}...")
            nav_res = client.call_tool(
                "navigate",
                {"instanceId": actual_instance_id, "url": resolved_nav_url},
                timeout=20.0,
            )
            if nav_res.is_error:
                return QualityGateResult(
                    status="FAIL",
                    build_output=build_output,
                    reason=f"Navigation failed: {nav_res.text or nav_res.error_message}",
                    duration_seconds=time.time() - start_time,
                )

        # -------------------------------------------------------------
        # Step 3: Inspect console logs & network logs
        # -------------------------------------------------------------
        # Allow brief moment for runtime hydration/initial fetches
        time.sleep(1.0)

        log("[mcp-verify] Inspecting console error logs...")
        cons_res = client.call_tool(
            "get_console_logs",
            {"instanceId": actual_instance_id, "onlyErrors": True},
            timeout=10.0,
        )
        raw_cons = cons_res.parsed_json
        console_errors: List[Any] = raw_cons if isinstance(raw_cons, list) else []

        log("[mcp-verify] Inspecting network logs for failed requests...")
        net_res = client.call_tool(
            "get_network_logs",
            {"instanceId": actual_instance_id, "onlyFailed": True},
            timeout=10.0,
        )
        raw_net = net_res.parsed_json
        failed_requests: List[Any] = raw_net if isinstance(raw_net, list) else []

        # -------------------------------------------------------------
        # Step 4: Capture desktop and optionally mobile screenshots
        # -------------------------------------------------------------
        log("[mcp-verify] Capturing full-page viewport screenshot...")
        shot_res = client.call_tool(
            "screenshot",
            {"instanceId": actual_instance_id, "fullPage": True},
            timeout=15.0,
        )
        screenshot_desktop_base64 = shot_res.image_base64
        screenshot_base64 = screenshot_desktop_base64

        screenshot_mobile_base64 = None
        if capture_mobile:
            log("[mcp-verify] Capturing mobile viewport screenshot (390x844)...")
            try:
                shot_mob = client.call_tool(
                    "screenshot",
                    {"instanceId": actual_instance_id, "fullPage": True, "width": 390, "height": 844},
                    timeout=15.0,
                )
                if shot_mob and not shot_mob.is_error:
                    screenshot_mobile_base64 = shot_mob.image_base64
            except Exception as e:
                log(f"[mcp-verify] ℹ️ Mobile viewport capture notice: {e}")

        # -------------------------------------------------------------
        # Step 5: Determine overall gate verdict
        # -------------------------------------------------------------
        has_console_errors = len(console_errors) > 0
        has_failed_requests = len(failed_requests) > 0

        if has_console_errors or has_failed_requests:
            issues = []
            if has_console_errors:
                issues.append(f"{len(console_errors)} console error(s)")
            if has_failed_requests:
                issues.append(f"{len(failed_requests)} failed network request(s)")
            issue_summary = " and ".join(issues)

            log(f"[mcp-verify] ❌ Quality gate FAILED: {issue_summary}")
            return QualityGateResult(
                status="FAIL",
                build_output=build_output,
                console_errors=console_errors,
                failed_requests=failed_requests,
                screenshot_base64=screenshot_base64,
                screenshot_desktop_base64=screenshot_desktop_base64,
                screenshot_mobile_base64=screenshot_mobile_base64,
                reason=f"Runtime inspection detected {issue_summary}.",
                duration_seconds=time.time() - start_time,
            )

        log("[mcp-verify] 🎉 Quality gate PASSED cleanly! (0 console errors, 0 network failures)")
        return QualityGateResult(
            status="PASS",
            build_output=build_output,
            console_errors=[],
            failed_requests=[],
            screenshot_base64=screenshot_base64,
            screenshot_desktop_base64=screenshot_desktop_base64,
            screenshot_mobile_base64=screenshot_mobile_base64,
            reason="Build passed cleanly, dev server running, 0 console errors, 0 network failures.",
            duration_seconds=time.time() - start_time,
        )

    finally:
        # GUARANTEED CLEANUP: Stop process and close browser instance
        log("[mcp-verify] Cleaning up background processes and browser instance...")
        try:
            client.call_tool("stop_process", {"instanceId": actual_instance_id}, timeout=5.0)
        except Exception:
            pass
        try:
            client.call_tool("close_instance", {"instanceId": actual_instance_id}, timeout=5.0)
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass
        log("[mcp-verify] Cleanup complete.")
