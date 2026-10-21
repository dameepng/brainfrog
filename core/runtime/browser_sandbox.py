# -*- coding: utf-8 -*-
"""Two-Container Isolated OCI Sandbox Backend for BrainFrog Phase 1D Browser Verification.

Architecture (one disposable set of resources per browser criterion):
- Network: a BrainFrog-owned ``docker network create --internal`` bridge. No default route,
  no external DNS, no published ports. Only the two containers below are attached.
- Container A (application): approved app image + approved profile argv only. The materialized
  source snapshot is mounted read-only; BrainFrog's launcher reports the digest it observes.
- Container B (browser): the approved, ID-pinned Playwright/Chromium image running BrainFrog's
  browser_runner.py against the fixed application origin. No host write mounts: evidence is
  streamed back as nonce-bound, hash-checked records.

Both containers: non-root, --read-only, --cap-drop=ALL, no-new-privileges, Docker's default
seccomp profile, CPU/memory/PID limits, --pull=never, no host ports, no Docker socket. The
effective configuration is checked with ``docker inspect`` before anything starts.
Cleanup removes only resources carrying this criterion's ownership labels.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from core.runtime.app_profile import (
    APP_HARNESS_MOUNT,
    APP_NETWORK_ALIAS,
    APP_SOURCE_MOUNT,
    AppProfile,
    UnsupportedProfileError,
    get_app_profile,
)
from core.runtime.browser_plan import BrowserPlan
from core.runtime.sandbox import (
    BRAINFROG_OWNER_LABEL,
    BRAINFROG_OWNER_VALUE,
    BRAINFROG_RUN_LABEL,
    DockerSandboxBackend,
    InfraErrorCategory,
    SANDBOX_UID_GID,
    SandboxInfraError,
    _run_bounded,
    compute_directory_digest,
)

RUNNER_SOURCE_PATH = Path(__file__).resolve().parent / "browser_runner.py"
# Browser-container seccomp profile: Docker Engine 29's default (github.com/moby/profiles/seccomp
# v0.1.0, sha256 01536f1d1df938ae...) plus exactly one rule allowing clone/unshare/chroot, which
# Chromium's namespace sandbox needs when the container has no capabilities. Pinned by hash.
BROWSER_SECCOMP_PROFILE_PATH = Path(__file__).resolve().parent / "browser_seccomp_chromium.json"
BROWSER_SECCOMP_PROFILE_SHA256 = "569408de219735e81062a130bbf23fdae7924655794bfc0b487d362b73104b39"
LAUNCHER_SOURCE_PATH = Path(__file__).resolve().parent / "app_launcher.py"
BROWSER_RECORD_PREFIX = "@@BF_BROWSER@@ "
APP_RECORD_PREFIX = "@@BF_APP@@ "
PHASE_LABEL = "io.brainfrog.phase"
ROLE_LABEL = "io.brainfrog.role"
CRITERION_LABEL = "io.brainfrog.criterion"
MAX_BROWSER_OUTPUT_BYTES = 24 * 1024 * 1024
MAX_APP_LOG_BYTES = 256 * 1024
MAX_BROWSER_TIMEOUT_S = 300.0

# Evidence files the runner may return, with size caps. Anything else is rejected.
RUNNER_ARTIFACT_LIMITS: Mapping[str, int] = MappingProxyType({
    "browser-assertions.json": 1024 * 1024,
    "browser-console.log": 256 * 1024,
    "browser-errors.log": 256 * 1024,
    "browser-network.log": 512 * 1024,
    "screenshot.png": 8 * 1024 * 1024,
})

# Operator-approved browser images, pinned by the exact local image ID. Specs cannot select images.
# brainfrog-browser-runner:v1.64.0 is built from docker/browser-runner/Dockerfile: the official
# Playwright image pinned by registry digest plus a hash-locked `playwright==1.64.0` install.
# BuildKit image IDs differ between builds of the same inputs, so every rebuild needs re-approval.
# The former v1.49.1 / Chromium 131 runtime is intentionally no longer approved (outdated browser).
APPROVED_BROWSER_IMAGES: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "brainfrog-browser-runner:v1.64.0": MappingProxyType({
        "image_id": "sha256:21d60e45fd0e7214a7027c115cf52ea34f9dc1fdde3224735f6d58197ab0c0de",
        "playwright_version": "1.64.0",
        "browser_version": "156.0.8078.4",
        "base_image": "mcr.microsoft.com/playwright/python:v1.64.0-noble@sha256:"
                      "684df257a1fa0ef3fa288239819984008f560e8f8db5b635ee9e2a50cbcd2e23",
        "requirements_lock": "docker/browser-runner/requirements.lock",
    }),
})
DEFAULT_BROWSER_IMAGE = "brainfrog-browser-runner:v1.64.0"

_UNSAFE_SECURITY_OPTS = ("seccomp=unconfined", "seccomp:unconfined", "apparmor=unconfined",
                         "apparmor:unconfined", "label=disable", "label:disable", "systempaths=unconfined")
_CRITERION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class BrowserLimits:
    cpus: float = 2.0
    memory_mb: int = 1536
    pids_limit: int = 256
    shm_mb: int = 256
    tmp_tmpfs_mb: int = 256
    evidence_tmpfs_mb: int = 32


@dataclass
class BrowserExecutionResult:
    status: str  # "PASS", "FAIL", "BLOCKED"
    duration_ms: float
    failure_reason: Optional[str] = None
    failure_kind: Optional[str] = None
    infrastructure_error_category: Optional[str] = None
    exit_code: Optional[int] = None
    artifacts: Dict[str, bytes] = field(default_factory=dict)
    assertion_manifest: Optional[Dict[str, Any]] = None
    assertions_executed: int = 0
    assertions_passed: int = 0
    assertions_failed: int = 0
    observed_source_digest: Optional[str] = None
    plan_sha256: Optional[str] = None
    browser_image: Optional[Dict[str, Any]] = None
    app_image: Optional[Dict[str, Any]] = None
    app_profile: Optional[Dict[str, Any]] = None
    browser_runtime: Optional[Dict[str, Any]] = None
    network: Optional[Dict[str, Any]] = None
    app_container: Optional[Dict[str, Any]] = None
    browser_container: Optional[Dict[str, Any]] = None
    cleanup: Optional[Dict[str, Any]] = None

    def run_metadata(self) -> Dict[str, Any]:
        """Host-side description of how this criterion was executed (stored as evidence)."""
        return {
            "status": self.status,
            "failure_kind": self.failure_kind,
            "infrastructure_error_category": self.infrastructure_error_category,
            "plan_sha256": self.plan_sha256,
            "observed_source_snapshot_digest": self.observed_source_digest,
            "browser_image": self.browser_image,
            "app_image": self.app_image,
            "app_profile": self.app_profile,
            "browser_runtime": self.browser_runtime,
            "network": self.network,
            "app_container": self.app_container,
            "browser_container": self.browser_container,
            "cleanup": self.cleanup,
        }


def load_browser_seccomp_profile() -> Tuple[str, Dict[str, Any]]:
    """Return (host path, parsed profile) after checking the file against its pinned hash."""
    data = BROWSER_SECCOMP_PROFILE_PATH.read_bytes()
    if hashlib.sha256(data).hexdigest() != BROWSER_SECCOMP_PROFILE_SHA256:
        raise SandboxInfraError(InfraErrorCategory.SECURITY_POLICY_NOT_ENFORCED,
                                "Browser seccomp profile does not match its pinned SHA-256; refusing to use it.")
    return DockerSandboxBackend._host_path(BROWSER_SECCOMP_PROFILE_PATH), json.loads(data)


def _summarize_with_seccomp_digest(inspect: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 1C config summary, with an embedded seccomp profile reduced to its canonical SHA-256."""
    summary = DockerSandboxBackend._summarize_config(inspect)
    opts = []
    for opt in summary.get("security_opt") or []:
        if opt.startswith("seccomp=") and opt[len("seccomp="):].lstrip().startswith("{"):
            canonical = json.dumps(json.loads(opt[len("seccomp="):]), sort_keys=True, separators=(",", ":"))
            opt = "seccomp=custom-profile-canonical-sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        opts.append(opt)
    summary["security_opt"] = opts
    return summary


def extract_records(stdout: str, prefix: str, nonce: str) -> Tuple[List[Dict[str, Any]], int]:
    """Return (records bound to nonce, count of prefixed lines that were not)."""
    good: List[Dict[str, Any]] = []
    bad = 0
    for line in stdout.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            rec = json.loads(line[len(prefix):])
        except ValueError:
            bad += 1
            continue
        if isinstance(rec, dict) and rec.get("nonce") == nonce:
            good.append(rec)
        else:
            bad += 1
    return good, bad


def recompute_browser_verdict(manifest: Dict[str, Any], plan: BrowserPlan) -> Tuple[str, Optional[str]]:
    """Derive the criterion verdict from step records alone (host-side, independent of the
    runner's own status field). Returns (status, inconsistency_reason)."""
    records = manifest.get("records") or []
    for i, rec in enumerate(records):
        if i >= len(plan.steps) or rec.get("action") != plan.steps[i].action or rec.get("step_index") != i + 1:
            return "BLOCKED", f"assertion record {i + 1} does not correspond to plan step {i + 1}"
        if rec.get("kind") != ("assertion" if plan.steps[i].is_assertion else "action"):
            return "BLOCKED", f"record {i + 1} misclassifies assertion/action"
    statuses = [r.get("status") for r in records]
    if manifest.get("status") == "BLOCKED" or "BLOCKED" in statuses:
        return "BLOCKED", None
    if "FAIL" in statuses:
        return "FAIL", None
    assertions = [r for r in records if r.get("kind") == "assertion"]
    shot = manifest.get("screenshot") or {}
    if (len(records) == len(plan.steps) and plan.assertion_count >= 1 and len(assertions) == plan.assertion_count
            and all(s == "PASS" for s in statuses) and shot.get("captured") and shot.get("sha256")):
        return "PASS", None
    return "BLOCKED", "plan not fully executed, no assertions, or required screenshot missing"


class DockerBrowserSandbox:
    """Manages the two-container browser verification lifecycle."""

    backend_type = "oci_browser_sandbox"

    def __init__(
        self,
        browser_image: str = DEFAULT_BROWSER_IMAGE,
        docker_executable: Optional[str] = None,
        approved_browser_images: Optional[Mapping[str, Mapping[str, str]]] = None,
        profiles: Optional[Mapping[str, AppProfile]] = None,
        limits: Optional[BrowserLimits] = None,
        browser_timeout_override_s: Optional[float] = None,
        before_browser_start: Optional[Callable[[str], None]] = None,
    ) -> None:
        # All parameters are operator/API-only; spec content never reaches this constructor.
        self.approved_browser_images = approved_browser_images or APPROVED_BROWSER_IMAGES
        if browser_image not in self.approved_browser_images:
            raise ValueError(f"Browser image '{browser_image}' is not an operator-approved browser image.")
        self.browser_image = browser_image
        self.profiles = profiles
        self.limits = limits or BrowserLimits()
        self.browser_timeout_override_s = browser_timeout_override_s
        self.before_browser_start = before_browser_start
        # Reuse the Phase 1C Docker helpers (inspect, ownership-checked removal, config summary).
        self._c = DockerSandboxBackend(docker_executable=docker_executable)
        self.docker_exe = self._c.docker_exe

    # -- docker helpers ---------------------------------------------------------------

    def _docker(self, args: List[str], timeout: float = 60.0, stdin_text: Optional[str] = None,
                on_timeout: Optional[Callable[[], None]] = None,
                max_bytes: int = 1024 * 1024) -> Tuple[Optional[int], str, str, bool, bool]:
        return _run_bounded([self.docker_exe] + args, timeout=timeout, stdin_text=stdin_text,
                            on_timeout=on_timeout, max_bytes=max_bytes)

    def is_available(self) -> Tuple[bool, Optional[str]]:
        avail, reason = self._c.is_available()
        if not avail:
            return False, reason
        try:
            self.resolve_browser_image()
        except SandboxInfraError as exc:
            return False, str(exc)
        return True, None

    def engine_version(self) -> Optional[str]:
        return self._c.engine_version()

    def resolve_browser_image(self) -> Dict[str, Any]:
        """Resolve the approved browser tag; its image ID must equal the pinned ID. Never pulls."""
        approval = self.approved_browser_images[self.browser_image]
        rc, out, err, timed_out, _ = self._docker(["image", "inspect", "--format", "{{json .}}", self.browser_image],
                                                  timeout=30.0)
        if timed_out:
            raise SandboxInfraError(InfraErrorCategory.DOCKER_UNAVAILABLE, "docker image inspect timed out")
        if rc != 0:
            raise SandboxInfraError(
                InfraErrorCategory.IMAGE_MISSING,
                f"Approved browser image '{self.browser_image}' is not available locally and pulls are disabled. "
                f"Build or load it manually (see phase1d-browser-security.md).",
            )
        info = json.loads(out)
        if info.get("Id") != approval["image_id"]:
            raise SandboxInfraError(
                InfraErrorCategory.IMAGE_NOT_APPROVED,
                f"Browser image tag '{self.browser_image}' resolves to {info.get('Id')}, "
                f"not the approved pinned ID {approval['image_id']}.",
            )
        if info.get("Os") != "linux":
            raise SandboxInfraError(InfraErrorCategory.IMAGE_NOT_APPROVED, "Browser image is not a linux image")
        return {
            "tag": self.browser_image,
            "image_id": info["Id"],
            "repo_digests": info.get("RepoDigests") or [],
            "platform": f"{info.get('Os')}/{info.get('Architecture')}",
            "expected_playwright_version": approval.get("playwright_version"),
            "expected_browser_version": approval.get("browser_version"),
            "base_image": approval.get("base_image"),
            # Environment baked into the approved image is part of its identity, not spec input.
            "image_env_keys": sorted(e.split("=", 1)[0] for e in ((info.get("Config") or {}).get("Env") or [])),
        }

    def resolve_app_image(self, profile: AppProfile) -> Dict[str, Any]:
        # Phase 1C allowlist: only operator-approved verification images resolve.
        return DockerSandboxBackend(image_name=profile.approved_container_image,
                                    docker_executable=self.docker_exe).resolve_image()

    # -- argument builders (fixed vectors; nothing derived from spec text) -------------

    @staticmethod
    def owner_labels(run_label: str, role: str, criterion_id: str) -> List[str]:
        return [
            f"--label={BRAINFROG_OWNER_LABEL}={BRAINFROG_OWNER_VALUE}",
            f"--label={BRAINFROG_RUN_LABEL}={run_label}",
            f"--label={PHASE_LABEL}=1d",
            f"--label={ROLE_LABEL}={role}",
            f"--label={CRITERION_LABEL}={criterion_id}",
        ]

    def build_network_create_args(self, network_name: str, run_label: str, criterion_id: str) -> List[str]:
        return ["network", "create", "--internal", "--driver=bridge"] + \
            self.owner_labels(run_label, "network", criterion_id) + [network_name]

    def build_app_create_args(self, container_name: str, network_name: str, run_label: str, criterion_id: str,
                              snapshot_dir: Path, app_harness_dir: Path, image_id: str,
                              profile: AppProfile) -> List[str]:
        src = DockerSandboxBackend._host_path(snapshot_dir)
        har = DockerSandboxBackend._host_path(app_harness_dir)
        return [
            "create",
            "--pull=never",
            f"--name={container_name}",
        ] + self.owner_labels(run_label, "app", criterion_id) + [
            f"--network={network_name}",
            f"--network-alias={APP_NETWORK_ALIAS}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--user={SANDBOX_UID_GID}",
            f"--pids-limit={int(profile.pids_limit)}",
            f"--memory={int(profile.memory_mb)}m",
            f"--memory-swap={int(profile.memory_mb)}m",
            f"--cpus={float(profile.cpus)}",
            "--ipc=private",
            "--restart=no",
            "--log-driver=json-file",
            "--log-opt=max-size=1m",
            "--log-opt=max-file=1",
            f"--mount=type=bind,source={src},target={APP_SOURCE_MOUNT},readonly",
            f"--mount=type=bind,source={har},target={APP_HARNESS_MOUNT},readonly",
            f"--tmpfs=/tmp:rw,nosuid,nodev,noexec,size={int(profile.tmp_tmpfs_mb)}m,uid=65532,gid=65532,mode=0700",
            f"--workdir={profile.working_directory}",
            "--env=HOME=/tmp",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTHONUNBUFFERED=1",
            image_id,
        ] + list(profile.approved_startup_argv)

    def build_browser_create_args(self, container_name: str, network_name: str, run_label: str, criterion_id: str,
                                  runner_dir: Path, input_dir: Path, image_id: str, origin: str,
                                  readiness: str, source_digest: str) -> List[str]:
        run = DockerSandboxBackend._host_path(runner_dir)
        inp = DockerSandboxBackend._host_path(input_dir)
        seccomp_path, _ = load_browser_seccomp_profile()
        lim = self.limits
        return [
            "create",
            "--interactive",
            "--pull=never",
            f"--name={container_name}",
        ] + self.owner_labels(run_label, "browser", criterion_id) + [
            f"--network={network_name}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--security-opt=seccomp={seccomp_path}",
            f"--user={SANDBOX_UID_GID}",
            f"--pids-limit={int(lim.pids_limit)}",
            f"--memory={int(lim.memory_mb)}m",
            f"--memory-swap={int(lim.memory_mb)}m",
            f"--cpus={float(lim.cpus)}",
            "--ipc=private",
            f"--shm-size={int(lim.shm_mb)}m",
            "--restart=no",
            "--log-driver=none",
            f"--mount=type=bind,source={run},target=/bf-harness,readonly",
            f"--mount=type=bind,source={inp},target=/bf-input,readonly",
            f"--tmpfs=/tmp:rw,nosuid,nodev,noexec,size={int(lim.tmp_tmpfs_mb)}m,uid=65532,gid=65532,mode=0700",
            f"--tmpfs=/evidence:rw,nosuid,nodev,noexec,size={int(lim.evidence_tmpfs_mb)}m,uid=65532,gid=65532,mode=0700",
            "--workdir=/tmp",
            "--env=HOME=/tmp",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            image_id,
            "python3", "-I", "-B", "/bf-harness/browser_runner.py",
            "--plan", "/bf-input/plan.json",
            "--origin", origin,
            "--criterion-id", criterion_id,
            "--readiness", readiness,
            "--source-digest", source_digest,
            "--browser-image", image_id,
            "--evidence-dir", "/evidence",
        ]

    # -- effective configuration checks -------------------------------------------------

    @staticmethod
    def _common_violations(inspect: Dict[str, Any], network_name: str, image_id: str, run_label: str,
                           memory_mb: int, cpus: float, pids: int) -> List[str]:
        hc = inspect.get("HostConfig") or {}
        cfg = inspect.get("Config") or {}
        v: List[str] = []
        if cfg.get("User") != SANDBOX_UID_GID:
            v.append(f"user={cfg.get('User')!r}")
        if inspect.get("Image") != image_id:
            v.append(f"image {inspect.get('Image')} != approved {image_id}")
        if hc.get("NetworkMode") != network_name:
            v.append(f"network_mode={hc.get('NetworkMode')!r}")
        nets = sorted(((inspect.get("NetworkSettings") or {}).get("Networks") or {}).keys())
        if nets != [network_name]:
            v.append(f"attached networks {nets}")
        if hc.get("ReadonlyRootfs") is not True:
            v.append("rootfs not read-only")
        if hc.get("Privileged"):
            v.append("privileged")
        sec = hc.get("SecurityOpt") or []
        if "no-new-privileges" not in sec and "no-new-privileges:true" not in sec:
            v.append("no-new-privileges missing")
        if any(o.replace(" ", "") in _UNSAFE_SECURITY_OPTS for o in sec):
            v.append(f"unsafe security options {sec}")
        if [c.upper() for c in (hc.get("CapDrop") or [])] != ["ALL"] or hc.get("CapAdd"):
            v.append(f"capabilities drop={hc.get('CapDrop')} add={hc.get('CapAdd')}")
        if hc.get("PidMode") not in ("", None) or hc.get("IpcMode") != "private" or hc.get("UTSMode") not in ("", None):
            v.append(f"namespace sharing pid={hc.get('PidMode')!r} ipc={hc.get('IpcMode')!r} uts={hc.get('UTSMode')!r}")
        if hc.get("Memory") != int(memory_mb) * 1024 * 1024 or hc.get("MemorySwap") != int(memory_mb) * 1024 * 1024:
            v.append(f"memory={hc.get('Memory')} swap={hc.get('MemorySwap')}")
        if hc.get("NanoCpus") != int(round(float(cpus) * 1e9)):
            v.append(f"nano_cpus={hc.get('NanoCpus')}")
        if hc.get("PidsLimit") != int(pids):
            v.append(f"pids_limit={hc.get('PidsLimit')}")
        if hc.get("Devices"):
            v.append("devices mapped")
        if hc.get("PortBindings") or hc.get("PublishAllPorts"):
            v.append(f"host ports published {hc.get('PortBindings')}")
        if hc.get("Binds"):
            v.append(f"legacy binds present {hc.get('Binds')}")
        if hc.get("ExtraHosts"):
            v.append(f"extra hosts {hc.get('ExtraHosts')}")
        mounts = inspect.get("Mounts") or []
        if any("docker.sock" in str(m.get("Source", "")) or "docker_engine" in str(m.get("Source", "")) for m in mounts):
            v.append("docker socket mounted")
        labels = cfg.get("Labels") or {}
        if labels.get(BRAINFROG_OWNER_LABEL) != BRAINFROG_OWNER_VALUE or labels.get(BRAINFROG_RUN_LABEL) != run_label:
            v.append("ownership labels missing")
        return v

    @staticmethod
    def _mount_violations(inspect: Dict[str, Any], expected: Dict[str, str], tmpfs: set) -> List[str]:
        v: List[str] = []
        seen: Dict[str, str] = {}
        for m in inspect.get("Mounts") or []:
            dest = m.get("Destination")
            if dest not in expected:
                v.append(f"unexpected mount {dest}")
                continue
            if m.get("Type") != "bind" or m.get("RW") is not False:
                v.append(f"mount {dest} not a read-only bind")
            seen[dest] = m.get("Source", "")
        for dest, src in expected.items():
            if dest not in seen:
                v.append(f"missing mount {dest}")
            elif os.path.normcase(seen[dest].rstrip("\\/")) != os.path.normcase(src.rstrip("\\/")):
                v.append(f"mount {dest} source mismatch")
        actual_tmpfs = set(((inspect.get("HostConfig") or {}).get("Tmpfs") or {}).keys())
        if actual_tmpfs != tmpfs:
            v.append(f"tmpfs set {sorted(actual_tmpfs)}")
        return v

    def verify_app_config(self, inspect: Dict[str, Any], network_name: str, image_id: str, run_label: str,
                          profile: AppProfile, snapshot_dir: str, harness_dir: str) -> List[str]:
        v = self._common_violations(inspect, network_name, image_id, run_label,
                                    profile.memory_mb, profile.cpus, profile.pids_limit)
        v += self._mount_violations(inspect, {APP_SOURCE_MOUNT: snapshot_dir, APP_HARNESS_MOUNT: harness_dir}, {"/tmp"})
        cfg = inspect.get("Config") or {}
        if list(cfg.get("Cmd") or []) != list(profile.approved_startup_argv):
            v.append("startup argv differs from approved profile")
        aliases = (((inspect.get("NetworkSettings") or {}).get("Networks") or {}).get(network_name) or {}).get("Aliases") or []
        if APP_NETWORK_ALIAS not in aliases:
            v.append(f"app network alias missing ({aliases})")
        if any(o.startswith(("seccomp=", "seccomp:")) for o in ((inspect.get("HostConfig") or {}).get("SecurityOpt") or [])):
            v.append("app container must use Docker's default seccomp profile")
        allowed_env = {"HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PATH", "LANG", "GPG_KEY",
                       "PYTHON_VERSION", "PYTHON_SHA256"} | set(profile.environment_allowlist)
        extra = sorted(k for k in (e.split("=", 1)[0] for e in (cfg.get("Env") or [])) if k not in allowed_env)
        if extra:
            v.append(f"unexpected env {extra}")
        return v

    def verify_browser_config(self, inspect: Dict[str, Any], network_name: str, image_id: str, run_label: str,
                              runner_dir: str, input_dir: str, image_env_keys: Optional[List[str]] = None) -> List[str]:
        lim = self.limits
        v = self._common_violations(inspect, network_name, image_id, run_label, lim.memory_mb, lim.cpus, lim.pids_limit)
        v += self._mount_violations(inspect, {"/bf-harness": runner_dir, "/bf-input": input_dir}, {"/tmp", "/evidence"})
        hc = inspect.get("HostConfig") or {}
        if hc.get("ShmSize") != int(lim.shm_mb) * 1024 * 1024:
            v.append(f"shm_size={hc.get('ShmSize')}")
        # The engine must hold exactly BrainFrog's pinned seccomp profile (docker inspect embeds it).
        seccomp_opts = [o for o in (hc.get("SecurityOpt") or []) if o.startswith(("seccomp=", "seccomp:"))]
        try:
            _, expected_profile = load_browser_seccomp_profile()
            if len(seccomp_opts) != 1 or json.loads(seccomp_opts[0][len("seccomp="):]) != expected_profile:
                v.append("browser seccomp profile is not the pinned BrainFrog Chromium profile")
        except (ValueError, SandboxInfraError) as exc:
            v.append(f"browser seccomp profile unverifiable: {exc}")
        allowed_env = {"HOME", "PYTHONDONTWRITEBYTECODE", "PATH", "PLAYWRIGHT_BROWSERS_PATH"} | set(image_env_keys or ())
        extra = sorted(k for k in (e.split("=", 1)[0] for e in ((inspect.get("Config") or {}).get("Env") or []))
                       if k not in allowed_env)
        if extra:
            v.append(f"unexpected env {extra}")
        return v

    def _inspect_network(self, name: str) -> Optional[Dict[str, Any]]:
        rc, out, _, _, _ = self._docker(["network", "inspect", "--format", "{{json .}}", name], timeout=30.0)
        if rc != 0:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None

    @staticmethod
    def verify_network(info: Dict[str, Any], run_label: str, expected_container_ids: List[str]) -> List[str]:
        v: List[str] = []
        if info.get("Internal") is not True:
            v.append("network is not internal")
        if info.get("Driver") != "bridge":
            v.append(f"driver={info.get('Driver')!r}")
        labels = info.get("Labels") or {}
        if labels.get(BRAINFROG_OWNER_LABEL) != BRAINFROG_OWNER_VALUE or labels.get(BRAINFROG_RUN_LABEL) != run_label:
            v.append("network ownership labels missing")
        attached = sorted((info.get("Containers") or {}).keys())
        if attached != sorted(expected_container_ids):
            v.append(f"unexpected network peers {attached}")
        return v

    def remove_owned_network(self, name: str, run_label: str) -> bool:
        info = self._inspect_network(name)
        if info is None:
            return True
        labels = info.get("Labels") or {}
        if labels.get(BRAINFROG_OWNER_LABEL) != BRAINFROG_OWNER_VALUE or labels.get(BRAINFROG_RUN_LABEL) != run_label:
            return False  # not ours: never touch it
        self._docker(["network", "rm", info.get("Id", name)], timeout=60.0)
        return self._inspect_network(name) is None

    def list_owned_resources(self, run_label: str) -> Dict[str, List[str]]:
        flt = f"label={BRAINFROG_RUN_LABEL}={run_label}"
        _, cout, _, _, _ = self._docker(["ps", "--all", "--quiet", "--no-trunc", "--filter", flt], timeout=30.0)
        _, nout, _, _, _ = self._docker(["network", "ls", "--quiet", "--no-trunc", "--filter", flt], timeout=30.0)
        return {"containers": [x for x in cout.split() if x], "networks": [x for x in nout.split() if x]}

    # -- execution ----------------------------------------------------------------------

    def browser_timeout_seconds(self, plan: BrowserPlan) -> float:
        if self.browser_timeout_override_s is not None:
            return float(self.browser_timeout_override_s)
        # plan budget + app readiness + browser launch/screenshot overhead
        return min(MAX_BROWSER_TIMEOUT_S, plan.budget_ms / 1000.0 + 15.0 + 60.0)

    def execute_browser_criterion(
        self,
        criterion_id: str,
        browser_plan: BrowserPlan,
        source_snapshot_dir: Path,
        source_snapshot_digest: str,
        app_profile_id: str = "python-static",
        run_id: str = "run",
    ) -> BrowserExecutionResult:
        """Run one browser criterion in a fresh app container + browser container + network."""
        t0 = time.perf_counter()
        plan_sha = browser_plan.plan_sha256()

        def blocked(category: str, reason: str, kind: Optional[str] = None, **extra: Any) -> BrowserExecutionResult:
            return BrowserExecutionResult(status="BLOCKED", duration_ms=(time.perf_counter() - t0) * 1000.0,
                                          failure_reason=reason, failure_kind=kind or category,
                                          infrastructure_error_category=category, plan_sha256=plan_sha, **extra)

        if not _CRITERION_ID_RE.match(criterion_id or ""):
            return blocked(InfraErrorCategory.INTERNAL_ERROR, f"Unsupported criterion id {criterion_id!r}")
        try:
            profile = get_app_profile(app_profile_id, self.profiles)
        except UnsupportedProfileError as exc:
            return blocked("unsupported_app_profile", f"BLOCKED — Unsupported Application Startup Profile: {exc}")

        avail, reason = self._c.is_available()
        if not avail:
            return blocked(InfraErrorCategory.DOCKER_UNAVAILABLE, reason or "Docker unavailable")
        try:
            browser_image = self.resolve_browser_image()
        except SandboxInfraError as exc:
            return blocked(exc.category, str(exc), kind="browser_unavailable")
        try:
            app_image = self.resolve_app_image(profile)
        except (SandboxInfraError, ValueError) as exc:
            return blocked(getattr(exc, "category", InfraErrorCategory.IMAGE_NOT_APPROVED), str(exc),
                           kind="app_image_unavailable", browser_image=browser_image)

        snapshot_dir = Path(source_snapshot_dir).resolve()
        if not snapshot_dir.is_dir():
            return blocked(InfraErrorCategory.MOUNT_UNAVAILABLE, f"Snapshot directory missing: {snapshot_dir}")

        token = secrets.token_hex(6)
        run_label = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', run_id)[:48]}.{criterion_id}.{token}"
        names = {"network": f"bf1d_net_{token}", "app": f"bf1d_app_{token}", "browser": f"bf1d_brw_{token}"}
        app_nonce = secrets.token_hex(32)
        browser_nonce = secrets.token_hex(32)

        work = Path(tempfile.mkdtemp(prefix="bf_browser_"))
        app_harness = work / "app_harness"
        runner_dir = work / "runner"
        input_dir = work / "input"
        for d in (app_harness, runner_dir, input_dir):
            d.mkdir()
        (app_harness / "app_launcher.py").write_bytes(LAUNCHER_SOURCE_PATH.read_bytes())
        (app_harness / "nonce").write_text(app_nonce, encoding="ascii")
        runner_bytes = RUNNER_SOURCE_PATH.read_bytes()
        (runner_dir / "browser_runner.py").write_bytes(runner_bytes)
        (input_dir / "plan.json").write_bytes(browser_plan.canonical_json().encode("utf-8"))

        ctx: Dict[str, Any] = {
            "browser_image": browser_image, "app_image": app_image, "app_profile": profile.to_dict(),
            "network": {"name": names["network"], "internal": None, "run_label": run_label},
            "browser_runtime": {"runner_sha256": hashlib.sha256(runner_bytes).hexdigest(),
                                "launcher_sha256": hashlib.sha256(LAUNCHER_SOURCE_PATH.read_bytes()).hexdigest()},
        }
        created = {"network": False, "app": False, "browser": False}
        result: Optional[BrowserExecutionResult] = None
        try:
            result = self._run(criterion_id, browser_plan, plan_sha, snapshot_dir, source_snapshot_digest, profile,
                               browser_image, app_image, run_label, names, app_nonce, browser_nonce,
                               app_harness, runner_dir, input_dir, ctx, created, blocked)
        finally:
            # Runs on success, failure, timeout and cancellation (KeyboardInterrupt).
            cleanup = {"browser_removed": (not created["browser"]) or self._c.remove_owned_container(names["browser"], run_label),
                       "app_removed": (not created["app"]) or self._c.remove_owned_container(names["app"], run_label)}
            cleanup["network_removed"] = (not created["network"]) or self.remove_owned_network(names["network"], run_label)
            leftovers = self.list_owned_resources(run_label)
            cleanup["remaining_owned"] = leftovers
            shutil.rmtree(work, ignore_errors=True)
        cleanup_ok = all(cleanup[k] for k in ("browser_removed", "app_removed", "network_removed")) and \
            not leftovers["containers"] and not leftovers["networks"]
        if not cleanup_ok:
            failed = blocked(InfraErrorCategory.CLEANUP_FAILED,
                             f"BrainFrog-owned browser verification resources could not be removed: {cleanup}",
                             **{k: ctx.get(k) for k in ("browser_image", "app_image", "app_profile", "network")})
            failed.cleanup = cleanup
            return failed
        result.cleanup = cleanup
        result.duration_ms = (time.perf_counter() - t0) * 1000.0
        return result

    def _run(self, criterion_id: str, plan: BrowserPlan, plan_sha: str, snapshot_dir: Path, snapshot_digest: str,
             profile: AppProfile, browser_image: Dict[str, Any], app_image: Dict[str, Any], run_label: str,
             names: Dict[str, str], app_nonce: str, browser_nonce: str, app_harness: Path, runner_dir: Path,
             input_dir: Path, ctx: Dict[str, Any], created: Dict[str, bool], blocked: Callable[..., BrowserExecutionResult],
             ) -> BrowserExecutionResult:
        meta = lambda: {k: ctx.get(k) for k in ("browser_image", "app_image", "app_profile", "network",  # noqa: E731
                                                  "browser_runtime", "app_container", "browser_container")}
        artifacts: Dict[str, bytes] = {}

        def infra(category: str, reason: str, kind: Optional[str] = None) -> BrowserExecutionResult:
            res = blocked(category, reason, kind, **meta())
            res.artifacts = artifacts
            return res

        try:
            # 1. Private internal network
            rc, out, err, timed_out, _ = self._docker(self.build_network_create_args(names["network"], run_label, criterion_id))
            if timed_out or rc != 0:
                return infra(InfraErrorCategory.CONTAINER_CREATE_FAILED,
                             f"Private network creation failed: {(err or out).strip()[:300]}", "network_create_failed")
            created["network"] = True
            net = self._inspect_network(names["network"]) or {}
            ctx["network"].update(id=net.get("Id"), internal=net.get("Internal"), driver=net.get("Driver"))
            net_v = self.verify_network(net, run_label, [])
            if net_v:
                return infra(InfraErrorCategory.SECURITY_POLICY_NOT_ENFORCED, f"Private network policy violated: {net_v}",
                             "network_isolation_not_enforced")

            # 2. Application container: create, verify, start, await nonce-bound readiness
            rc, out, err, timed_out, _ = self._docker(self.build_app_create_args(
                names["app"], names["network"], run_label, criterion_id, snapshot_dir, app_harness,
                app_image["image_id"], profile))
            if timed_out or rc != 0:
                return infra(InfraErrorCategory.CONTAINER_CREATE_FAILED,
                             f"Application container creation failed: {(err or out).strip()[:300]}", "app_startup_failed")
            created["app"] = True
            app_inspect = self._c._inspect(names["app"]) or {}
            ctx["app_container"] = DockerSandboxBackend._summarize_config(app_inspect)
            app_v = self.verify_app_config(app_inspect, names["network"], app_image["image_id"], run_label, profile,
                                           str(snapshot_dir), str(app_harness.resolve()))
            if app_v:
                return infra(InfraErrorCategory.SECURITY_POLICY_NOT_ENFORCED,
                             f"Application container configuration violates policy: {app_v}", "app_isolation_not_enforced")
            rc, out, err, timed_out, _ = self._docker(["start", app_inspect["Id"]], timeout=60.0)
            if timed_out or rc != 0:
                return infra(InfraErrorCategory.CONTAINER_START_FAILED,
                             f"Application container failed to start: {(err or out).strip()[:300]}", "app_startup_failed")

            ready, observed_digest, app_log, why = self._await_app(app_inspect["Id"], app_nonce, profile)
            artifacts["app-server.log"] = app_log
            if not ready:
                return infra("app_startup_failed", f"Application did not become ready: {why}", "app_startup_failed")
            ctx["observed_source_digest"] = observed_digest
            if observed_digest != snapshot_digest:
                return infra(InfraErrorCategory.SNAPSHOT_IDENTITY_MISMATCH,
                             f"Application container observed source digest {observed_digest} != materialized "
                             f"snapshot {snapshot_digest}", "snapshot_identity_mismatch")

            # 3. Browser container: create, verify, check network peers, run
            rc, out, err, timed_out, _ = self._docker(self.build_browser_create_args(
                names["browser"], names["network"], run_label, criterion_id, runner_dir, input_dir,
                browser_image["image_id"], profile.origin, profile.readiness_endpoint, snapshot_digest))
            if timed_out or rc != 0:
                return infra(InfraErrorCategory.CONTAINER_CREATE_FAILED,
                             f"Browser container creation failed: {(err or out).strip()[:300]}", "browser_unavailable")
            created["browser"] = True
            brw_inspect = self._c._inspect(names["browser"]) or {}
            ctx["browser_container"] = _summarize_with_seccomp_digest(brw_inspect)
            brw_v = self.verify_browser_config(brw_inspect, names["network"], browser_image["image_id"], run_label,
                                               str(runner_dir.resolve()), str(input_dir.resolve()),
                                               browser_image.get("image_env_keys"))
            net = self._inspect_network(names["network"]) or {}
            brw_v += self.verify_network(net, run_label, [app_inspect["Id"]])
            if brw_v:
                return infra(InfraErrorCategory.SECURITY_POLICY_NOT_ENFORCED,
                             f"Browser container/network configuration violates policy: {brw_v}",
                             "browser_isolation_not_enforced")

            if self.before_browser_start is not None:  # operator/test hook (e.g. stop the app)
                self.before_browser_start(app_inspect["Id"])

            timeout_s = self.browser_timeout_seconds(plan)
            ctx["browser_runtime"]["timeout_seconds"] = timeout_s
            brw_id = brw_inspect["Id"]
            rc, out, err, timed_out, truncated = self._docker(
                ["start", "--attach", "--interactive", brw_id], timeout=timeout_s, stdin_text=browser_nonce + "\n",
                on_timeout=lambda: self._docker(["kill", brw_id], timeout=30.0), max_bytes=MAX_BROWSER_OUTPUT_BYTES)
            post = (self._c._inspect(brw_id) or {}).get("State") or {}
            artifacts["app-server.log"] = self._app_log(app_inspect["Id"])
            if timed_out:
                return infra(InfraErrorCategory.TIMEOUT, f"Browser verification exceeded {timeout_s:.0f}s and was killed.",
                             "timeout")
            if post.get("OOMKilled"):
                return infra(InfraErrorCategory.RESOURCE_LIMIT, "Browser container was OOM-killed (memory limit).",
                             "browser_crashed")
            if truncated:
                return infra(InfraErrorCategory.OUTPUT_TRUNCATED, "Browser runner output exceeded the capture limit.",
                             "evidence_export_failed")
            exit_code = post.get("ExitCode", rc)

            result = self._interpret(out, err, exit_code, browser_nonce, criterion_id, plan, plan_sha,
                                     snapshot_digest, browser_image, artifacts, infra)
            if result.status != "BLOCKED" and compute_directory_digest(snapshot_dir) != snapshot_digest:
                return infra(InfraErrorCategory.SNAPSHOT_MUTATED, "Materialized snapshot changed during execution.",
                             "snapshot_mutated")
            if result.assertion_manifest:
                reported = result.assertion_manifest.get("runtime") or {}
                ctx["browser_runtime"].update({k: reported.get(k) for k in (
                    "browser_name", "browser_version", "playwright_version", "chromium_internal_sandbox",
                    "container_uid", "launch_args", "locale", "timezone_id", "reduced_motion", "service_workers")})
            for k, val in meta().items():
                setattr(result, k, val)
            result.observed_source_digest = observed_digest
            result.plan_sha256 = plan_sha
            result.exit_code = exit_code
            return result
        except SandboxInfraError as exc:
            return infra(exc.category, str(exc))
        except OSError as exc:
            return infra(InfraErrorCategory.DOCKER_UNAVAILABLE, f"Docker CLI failure: {exc}")

    def _app_log(self, container_id: str) -> bytes:
        _, out, err, _, _ = self._docker(["logs", container_id], timeout=30.0, max_bytes=MAX_APP_LOG_BYTES)
        return (out + ("\n" if out and err else "") + err).encode("utf-8")

    def _await_app(self, container_id: str, nonce: str, profile: AppProfile) -> Tuple[bool, Optional[str], bytes, str]:
        """Wait for the launcher's nonce-bound 'listening' record. Returns (ready, digest, log, reason)."""
        deadline = time.monotonic() + profile.startup_timeout_seconds
        digest: Optional[str] = None
        while True:
            _, out, err, _, _ = self._docker(["logs", container_id], timeout=30.0, max_bytes=MAX_APP_LOG_BYTES)
            records, _ = extract_records(out, APP_RECORD_PREFIX, nonce)
            for rec in records:
                if rec.get("event") == "source_digest":
                    digest = rec.get("digest")
            log = (out + ("\n" if out and err else "") + err).encode("utf-8")
            if any(r.get("event") == "listening" for r in records):
                return True, digest, log, ""
            state = (self._c._inspect(container_id) or {}).get("State") or {}
            if not state.get("Running"):
                return False, digest, log, f"application process exited (code {state.get('ExitCode')})"
            if time.monotonic() >= deadline:
                return False, digest, log, f"no readiness signal within {profile.startup_timeout_seconds:.0f}s"
            time.sleep(0.25)

    @staticmethod
    def _interpret(out: str, err: str, exit_code: Optional[int], nonce: str, criterion_id: str, plan: BrowserPlan,
                   plan_sha: str, snapshot_digest: str, browser_image: Dict[str, Any], artifacts: Dict[str, bytes],
                   infra: Callable[..., BrowserExecutionResult]) -> BrowserExecutionResult:
        records, forged = extract_records(out, BROWSER_RECORD_PREFIX, nonce)
        if forged:
            return infra(InfraErrorCategory.FORGED_RUNNER_RECORD, f"{forged} browser record(s) not bound to this run's nonce.",
                         "evidence_export_failed")
        if not any(r.get("event") == "started" for r in records):
            tail = (err or "").strip()[-300:]
            return infra(InfraErrorCategory.RUNNER_NOT_STARTED,
                         f"Browser runner did not start (exit {exit_code}): {tail}", "browser_unavailable")
        completes = [r for r in records if r.get("event") == "complete"]
        if len(completes) != 1:
            return infra(InfraErrorCategory.INCOMPLETE_RUNNER_RESULT,
                         f"Browser runner produced {len(completes)} completion records (exit {exit_code}); "
                         "the browser run was interrupted or crashed.", "browser_crashed")
        complete = completes[0]
        if complete.get("criterion_id") != criterion_id:
            return infra(InfraErrorCategory.INCOMPLETE_RUNNER_RESULT, "Runner result bound to a different criterion.",
                         "evidence_export_failed")

        declared = complete.get("artifacts") or {}
        for rec in records:
            if rec.get("event") != "artifact":
                continue
            name = rec.get("name")
            if name not in RUNNER_ARTIFACT_LIMITS or name in artifacts:
                return infra(InfraErrorCategory.INCOMPLETE_RUNNER_RESULT, f"Unexpected or duplicate artifact {name!r}.",
                             "evidence_export_failed")
            try:
                data = base64.b64decode(rec.get("data_b64", ""), validate=True)
            except (ValueError, TypeError):
                return infra(InfraErrorCategory.INCOMPLETE_RUNNER_RESULT, f"Artifact {name} is not valid base64.",
                             "evidence_export_failed")
            digest = hashlib.sha256(data).hexdigest()
            if (len(data) > RUNNER_ARTIFACT_LIMITS[name] or digest != rec.get("sha256")
                    or (declared.get(name) or {}).get("sha256") != digest):
                return infra(InfraErrorCategory.INCOMPLETE_RUNNER_RESULT, f"Artifact {name} failed size/hash validation.",
                             "evidence_export_failed")
            artifacts[name] = data
        if set(declared) != {n for n in artifacts if n in RUNNER_ARTIFACT_LIMITS}:
            return infra(InfraErrorCategory.INCOMPLETE_RUNNER_RESULT, "Declared and received artifacts differ.",
                         "evidence_export_failed")

        runner_status = complete.get("status")
        if "browser-assertions.json" not in artifacts:
            if runner_status == "BLOCKED":
                return infra(complete.get("failure_kind") or InfraErrorCategory.INCOMPLETE_RUNNER_RESULT,
                             complete.get("failure_reason") or "Browser runner blocked.", complete.get("failure_kind"))
            return infra(InfraErrorCategory.INCOMPLETE_RUNNER_RESULT, "Assertion manifest missing.", "evidence_export_failed")
        try:
            manifest = json.loads(artifacts["browser-assertions.json"].decode("utf-8"))
        except ValueError:
            return infra(InfraErrorCategory.INCOMPLETE_RUNNER_RESULT, "Assertion manifest is not valid JSON.",
                         "evidence_export_failed")

        binding_errors = []
        if manifest.get("criterion_id") != criterion_id:
            binding_errors.append("criterion_id")
        if manifest.get("plan_sha256") != plan_sha:
            binding_errors.append("plan_sha256")
        if manifest.get("source_snapshot_digest") != snapshot_digest:
            binding_errors.append("source_snapshot_digest")
        if manifest.get("browser_image_identity") != browser_image["image_id"]:
            binding_errors.append("browser_image_identity")
        if manifest.get("status") != runner_status:
            binding_errors.append("status")
        shot = manifest.get("screenshot") or {}
        if shot.get("captured"):
            if "screenshot.png" not in artifacts or shot.get("sha256") != hashlib.sha256(artifacts["screenshot.png"]).hexdigest():
                binding_errors.append("screenshot_sha256")
        elif "screenshot.png" in artifacts:
            binding_errors.append("unreferenced screenshot")
        expected_pw = browser_image.get("expected_playwright_version")
        runtime = manifest.get("runtime") or {}
        if runtime.get("playwright_version") and expected_pw and runtime["playwright_version"] != expected_pw:
            binding_errors.append("playwright_version")
        expected_browser = browser_image.get("expected_browser_version")
        if runtime.get("browser_version") and expected_browser and runtime["browser_version"] != expected_browser:
            binding_errors.append("browser_version")
        if runner_status in ("PASS", "FAIL"):
            # A verdict on page behaviour is only accepted from a run whose renderers were proven sandboxed.
            sandbox = runtime.get("renderer_sandbox") or {}
            if not (runtime.get("chromium_internal_sandbox") is True and sandbox.get("verified") is True
                    and (runtime.get("renderer_sandbox_at_end") or {}).get("verified") is True):
                binding_errors.append("renderer_sandbox_unverified")
        if binding_errors:
            return infra(InfraErrorCategory.INCOMPLETE_RUNNER_RESULT,
                         f"Browser evidence binding failed: {binding_errors}", "evidence_binding_failed")

        status, inconsistency = recompute_browser_verdict(manifest, plan)
        if inconsistency and runner_status != "BLOCKED":
            return infra(InfraErrorCategory.INCOMPLETE_RUNNER_RESULT, f"Inconsistent runner result: {inconsistency}",
                         "evidence_binding_failed")
        if status != runner_status:
            return infra(InfraErrorCategory.INCOMPLETE_RUNNER_RESULT,
                         f"Runner status {runner_status} disagrees with recomputed verdict {status}.",
                         "evidence_binding_failed")

        summary = manifest.get("summary") or {}
        kind = manifest.get("failure_kind")
        return BrowserExecutionResult(
            status=status,
            duration_ms=0.0,
            failure_reason=manifest.get("failure_reason"),
            failure_kind=kind,
            infrastructure_error_category=kind if status == "BLOCKED" else None,
            artifacts=artifacts,
            assertion_manifest=manifest,
            assertions_executed=int(summary.get("assertions_executed", 0)),
            assertions_passed=int(summary.get("assertions_passed", 0)),
            assertions_failed=int(summary.get("assertions_failed", 0)),
        )
