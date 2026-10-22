"""Trusted CI gate for BrainFrog verification on GitHub Actions (Phase 1E).

Trust model:
- This module runs from the *trusted* checkout (the PR base revision). The verifier code, the acceptance
  policy, the acceptance specs, the dependency locks and the browser-image recipe all come from there.
- The *candidate* checkout (the revision GitHub tested for the PR) is data. It is snapshotted and executed
  only inside the Phase 1C/1D OCI backends; it is never imported or executed by this host process.
- The CI outcome is derived from validated report bundles, not from any single status field: a missing,
  malformed, tampered or inconsistent report is never a success.

Subcommands (see main()):
    provision  pull the digest-pinned Python image and build the browser runner from the trusted recipe
               (GitHub-hosted CI only; refuses to run elsewhere)
    gate       identity checks + verification + evidence bundle + decision (always writes decision.json)
    digest     print/record the sha256 of the finalized evidence manifest
    enforce    re-check the evidence bundle and map the decision to the step exit code
    selftest   run the candidate's own unit-test modules on the CI host with a strict quarantine policy
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.ci_trust import resolve_trusted_base
from core.runtime.browser_sandbox import (
    APPROVED_BROWSER_IMAGES,
    BROWSER_SECCOMP_PROFILE_PATH,
    BROWSER_SECCOMP_PROFILE_SHA256,
    DockerBrowserSandbox,
)
from core.runtime.sandbox import APPROVED_DOCKER_VERIFICATION_IMAGES, DockerSandboxBackend, _run_bounded
from core.runtime.secret_scrubbing import safe_scrub, scrub_text_patterns
from core.verifier import StandaloneVerifier, parse_spec_markdown, verify_report_integrity

CI_POLICY_SCHEMA = "brainfrog.ci-policy/1e"
CI_METADATA_SCHEMA = "brainfrog.ci-metadata/1e"
CI_MANIFEST_SCHEMA = "brainfrog.ci-evidence-manifest/1e"
POLICY_REL_PATH = ".github/brainfrog/policy.json"
EVIDENCE_DIRNAME = "brainfrog-ci-evidence"

OUTCOME_EXIT_CODES = {"success": 0, "failure": 1, "blocked": 2}

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_REPO_DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,200}@sha256:[0-9a-f]{64}$")
_TEST_ID_RE = re.compile(r"^[A-Za-z0-9_.]{1,300}$")
# Machine-specific paths that must never be published in CI evidence.
_PRIVATE_PATH_RE = re.compile(r"(?i)(?:[a-z]:[\\/]+users[\\/]+|/Users/|/home/(?!runner/))")
# Value-based secret detection for publication. Key-name heuristics are deliberately not used here: report
# keys such as 'authorization_reference' are metadata, not credentials.
_SECRET_VALUE_TAGS = ("[REDACTED_API_KEY]", "[REDACTED_BOT_TOKEN]", "[REDACTED_TOKEN]", "[REDACTED_PRIVATE_KEY",
                      "_REDACTED]")
_SECRET_HEADER_RE = re.compile(r"(?im)(?:^|[{,])[\s\"']*(?:set-cookie|cookie|proxy-authorization|authorization)"
                               r"[\"']?\s*:\s*[\"']?[^\s\"',}\]]")


def contains_secret_values(text: str) -> bool:
    """True for token/key formats, PEM private keys, live secret env values, cookie/authorization headers."""
    scrubbed = scrub_text_patterns(text)
    if any(scrubbed.count(tag) > text.count(tag) for tag in _SECRET_VALUE_TAGS):
        return True
    return bool(_SECRET_HEADER_RE.search(text))


class PolicyError(ValueError):
    """The trusted CI policy is missing or malformed (the gate is BLOCKED, never green)."""


# =============================================================================
# Small helpers
# =============================================================================

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, data: Any) -> bytes:
    raw = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _safe_relpath(value: Any, what: str) -> str:
    """A repository-relative POSIX path with no traversal, drive or absolute component."""
    if not isinstance(value, str) or not value or len(value) > 300:
        raise PolicyError(f"{what} must be a non-empty relative path")
    if value.startswith(("/", "\\")) or ":" in value or "\\" in value:
        raise PolicyError(f"{what} must be a relative POSIX path: {value!r}")
    if any(part in ("..", "") for part in value.rstrip("/").split("/")) and value not in (".",):
        raise PolicyError(f"{what} must not contain empty or '..' segments: {value!r}")
    return value


def _contains_link(root: Path, rel: str) -> bool:
    """True if any component of root/rel is a symlink or junction (never followed by the gate)."""
    cur = root
    for part in [p for p in rel.split("/") if p not in ("", ".")]:
        cur = cur / part
        if cur.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(cur)):
            return True
    return False


# =============================================================================
# Trusted policy
# =============================================================================

def load_policy(trusted_root: Path) -> Tuple[Dict[str, Any], str]:
    """Load and validate the trusted CI policy. Returns (policy, sha256 of the policy bytes)."""
    path = Path(trusted_root) / POLICY_REL_PATH
    if _contains_link(Path(trusted_root), POLICY_REL_PATH) or not path.is_file():
        raise PolicyError(f"trusted policy '{POLICY_REL_PATH}' is missing")
    raw = path.read_bytes()
    try:
        policy = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise PolicyError(f"trusted policy is not valid JSON: {exc}") from exc
    if not isinstance(policy, dict) or policy.get("schema") != CI_POLICY_SCHEMA:
        raise PolicyError(f"trusted policy schema must be '{CI_POLICY_SCHEMA}'")

    targets = policy.get("targets")
    if not isinstance(targets, list) or not targets:
        raise PolicyError("policy 'targets' must be a non-empty list")
    seen = set()
    for t in targets:
        if not isinstance(t, dict) or not _ID_RE.match(str(t.get("id", ""))) or t["id"] in seen:
            raise PolicyError("every target needs a unique id matching [A-Za-z0-9_-]{1,64}")
        seen.add(t["id"])
        _safe_relpath(t.get("path"), f"target {t['id']} path")
        spec = _safe_relpath(t.get("spec"), f"target {t['id']} spec")
        if not spec.startswith(".github/brainfrog/"):
            raise PolicyError(f"target {t['id']} spec must live under .github/brainfrog/")
        req = t.get("required_criteria")
        if not isinstance(req, list) or not req or not all(isinstance(c, str) and _ID_RE.match(c) for c in req):
            raise PolicyError(f"target {t['id']} needs a non-empty required_criteria list")
        if len(set(req)) != len(req):
            raise PolicyError(f"target {t['id']} required_criteria contains duplicates")

    py = policy.get("python_image") or {}
    if py.get("tag") not in set(APPROVED_DOCKER_VERIFICATION_IMAGES.values()):
        raise PolicyError("python_image.tag must be a Phase 1C approved verification image")
    if not _REPO_DIGEST_RE.match(str(py.get("repo_digest", ""))):
        raise PolicyError("python_image.repo_digest must be '<repository>@sha256:<64 hex>'")

    br = policy.get("browser_image") or {}
    if br.get("tag") not in APPROVED_BROWSER_IMAGES:
        raise PolicyError("browser_image.tag must be a Phase 1D approved browser image tag")
    for key in ("dockerfile", "requirements_lock"):
        _safe_relpath(br.get(key), f"browser_image.{key}")
    for key in ("dockerfile_sha256", "requirements_lock_sha256"):
        if not _SHA256_RE.match(str(br.get(key, ""))):
            raise PolicyError(f"browser_image.{key} must be a sha256 hex digest")

    trusted_paths = policy.get("trusted_paths")
    if not isinstance(trusted_paths, list) or not trusted_paths:
        raise PolicyError("policy 'trusted_paths' must be a non-empty list")
    for p in trusted_paths:
        _safe_relpath(p, "trusted_paths entry")

    limits = policy.get("evidence_limits") or {}
    for key in ("max_total_bytes", "max_files", "max_file_bytes"):
        if not isinstance(limits.get(key), int) or limits[key] <= 0:
            raise PolicyError(f"evidence_limits.{key} must be a positive integer")
    return policy, hashlib.sha256(raw).hexdigest()


# =============================================================================
# Identity (event, checkouts, trusted configuration)
# =============================================================================

def git_head(repo: Path) -> Optional[str]:
    """HEAD commit of a checkout, read with hooks, fsmonitor and global/system config disabled."""
    env = {k: v for k, v in os.environ.items() if k.upper() in ("PATH", "SYSTEMROOT", "TEMP", "TMP")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                "HOME": tempfile.gettempdir()})
    argv = ["git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
            "-c", f"safe.directory={Path(repo).resolve().as_posix()}", "-C", str(repo),
            "rev-parse", "--verify", "HEAD^{commit}"]
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env, shell=False)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = res.stdout.strip()
    return sha if res.returncode == 0 and _SHA1_RE.match(sha) else None


def _event_str(value: Any, limit: int = 255) -> Optional[str]:
    """Untrusted event strings are kept as inert data, bounded in size, never interpolated into commands."""
    return value[:limit] if isinstance(value, str) else None


def collect_identity(event: Mapping[str, Any], env: Mapping[str, str], trusted_root: Path,
                     candidate_root: Path, base_root: Optional[Path] = None) -> Dict[str, Any]:
    """Bind the run to the PR, the tested revision and the trusted revision. Problems make the gate BLOCKED.

    The trusted revision must be the verified first parent of the tested PR merge (core.ci_trust), which is
    re-derived here from the trusted code; the event's base.sha is recorded only as a diagnostic because
    GitHub does not refresh it when the base branch moves."""
    problems: List[str] = []
    pr = event.get("pull_request") if isinstance(event.get("pull_request"), dict) else {}
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}

    event_name = env.get("GITHUB_EVENT_NAME")
    repository = env.get("GITHUB_REPOSITORY", "")
    number = event.get("number", pr.get("number"))
    head_sha, base_sha, github_sha = head.get("sha"), base.get("sha"), env.get("GITHUB_SHA")

    if event_name != "pull_request":
        problems.append(f"unsupported event '{_event_str(event_name, 40)}': the gate only evaluates pull_request runs")
    if not _REPO_RE.match(repository):
        problems.append("GITHUB_REPOSITORY is missing or malformed")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        problems.append("pull request number is missing or malformed")
        number = None
    for name, value in (("pull_request.head.sha", head_sha), ("pull_request.base.sha", base_sha),
                        ("GITHUB_SHA", github_sha)):
        if not isinstance(value, str) or not _SHA1_RE.match(value):
            problems.append(f"{name} is missing or not a 40-character commit SHA")

    tested_sha = git_head(candidate_root)
    trusted_sha = git_head(trusted_root)
    if tested_sha is None:
        problems.append("candidate checkout HEAD could not be read")
    elif tested_sha != github_sha:
        problems.append(f"candidate checkout is at {tested_sha}, but the run is for GITHUB_SHA {github_sha}")
    trust = resolve_trusted_base(event, env, Path(candidate_root), Path(base_root) if base_root else None)
    problems.extend(f"trusted base: {p}" for p in trust["problems"])
    if trusted_sha is None:
        problems.append("trusted checkout HEAD could not be read")
    elif trust["trusted_base_sha"] is not None and trusted_sha != trust["trusted_base_sha"]:
        problems.append(f"trusted checkout is at {trusted_sha}, not the verified merge base {trust['trusted_base_sha']}")

    def _int(name: str) -> Optional[int]:
        v = env.get(name, "")
        return int(v) if v.isdigit() else None

    run_id, run_attempt = _int("GITHUB_RUN_ID"), _int("GITHUB_RUN_ATTEMPT")
    if run_id is None or run_attempt is None:
        problems.append("GITHUB_RUN_ID / GITHUB_RUN_ATTEMPT are missing or malformed")
    head_full_name = _event_str(head_repo.get("full_name"), 201)
    return {
        "ci_provider": "github-actions",
        "repository": repository if _REPO_RE.match(repository) else None,
        "event_name": _event_str(event_name, 40),
        "pull_request_number": number,
        "head_sha": head_sha if isinstance(head_sha, str) and _SHA1_RE.match(head_sha) else None,
        "base_sha": base_sha if isinstance(base_sha, str) and _SHA1_RE.match(base_sha) else None,
        "github_sha": github_sha if isinstance(github_sha, str) and _SHA1_RE.match(github_sha) else None,
        # The commit actually materialized and executed. For pull_request runs this is normally GitHub's
        # synthetic merge commit (refs/pull/N/merge), NOT the PR head commit.
        "tested_sha": tested_sha,
        "tested_sha_is_pr_head": tested_sha is not None and tested_sha == head_sha,
        "head_repository": head_full_name,
        "is_fork_pull_request": bool(head_full_name and repository and head_full_name != repository),
        "head_ref": _event_str(head.get("ref")),
        "workflow_ref": _event_str(env.get("GITHUB_WORKFLOW_REF"), 400),
        "workflow_sha": env.get("GITHUB_WORKFLOW_SHA") if _SHA1_RE.match(env.get("GITHUB_WORKFLOW_SHA", "")) else None,
        "workflow_run_id": run_id,
        "workflow_run_attempt": run_attempt,
        "trusted_verifier_revision": trusted_sha,
        "trusted_base": {k: v for k, v in trust.items() if k != "problems"},
        "problems": problems,
    }


def _files_under(root: Path, rel: str) -> Dict[str, Optional[str]]:
    """Map rel paths under root/rel to their sha256 (None for symlinks/special files), without following links."""
    out: Dict[str, Optional[str]] = {}
    base = root / rel
    if rel.endswith("/"):
        if _contains_link(root, rel) or not base.is_dir():
            return {rel: None} if (base.exists() or base.is_symlink()) else {}
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            for name in sorted(filenames + [d for d in dirnames if (Path(dirpath) / d).is_symlink()]):
                p = Path(dirpath) / name
                key = p.relative_to(root).as_posix()
                out[key] = None if p.is_symlink() or not p.is_file() else _sha256_file(p)
        return out
    if base.is_symlink() or _contains_link(root, rel):
        return {rel: None}
    if base.is_file():
        return {rel: _sha256_file(base)}
    return {}


def trusted_config_changes(policy: Mapping[str, Any], trusted_root: Path, candidate_root: Path) -> List[str]:
    """Trusted-path files the candidate adds, removes, alters or replaces with links."""
    changed: List[str] = []
    for rel in policy["trusted_paths"]:
        a, b = _files_under(Path(trusted_root), rel), _files_under(Path(candidate_root), rel)
        for key in sorted(set(a) | set(b)):
            if a.get(key) is None or a.get(key) != b.get(key):
                changed.append(key)
    return changed


# =============================================================================
# OCI images
# =============================================================================

def _docker_json(docker: str, args: Sequence[str], timeout: float = 60.0) -> Tuple[Optional[Any], str]:
    try:
        rc, out, err, timed_out, _ = _run_bounded([docker, *args], timeout=timeout)
    except OSError as exc:
        return None, f"docker could not be launched: {exc}"
    if timed_out:
        return None, "docker command timed out"
    if rc != 0:
        return None, (err or out).strip()[:300]
    try:
        return json.loads(out), ""
    except ValueError:
        return None, "docker returned malformed JSON"


def inspect_images(policy: Mapping[str, Any], trusted_root: Path, docker: str = "docker") -> Dict[str, Any]:
    """Confirm the images the verifier will use are exactly the policy-approved ones. Never pulls or builds."""
    problems: List[str] = []
    py_pol, br_pol = policy["python_image"], policy["browser_image"]
    result: Dict[str, Any] = {"python": None, "browser": None, "docker_engine_version": None, "problems": problems}

    version, err = _docker_json(docker, ["version", "--format", "{{json .Server.Version}}"], timeout=20.0)
    if not isinstance(version, str):
        problems.append(f"Docker engine unavailable: {err or 'no server version'}")
        return result
    result["docker_engine_version"] = version

    info, err = _docker_json(docker, ["image", "inspect", "--format", "{{json .}}", py_pol["tag"]])
    if not isinstance(info, dict):
        problems.append(f"approved Python image '{py_pol['tag']}' is not present: {err}")
    else:
        digests = info.get("RepoDigests") or []
        result["python"] = {"tag": py_pol["tag"], "image_id": info.get("Id"), "repo_digests": digests,
                            "required_repo_digest": py_pol["repo_digest"],
                            "platform": f"{info.get('Os')}/{info.get('Architecture')}"}
        if py_pol["repo_digest"] not in digests:
            problems.append(f"'{py_pol['tag']}' does not carry the pinned registry digest {py_pol['repo_digest']}")
        if info.get("Os") != "linux":
            problems.append("approved Python image is not a linux image")

    # The browser image is built in CI from the trusted recipe; the recipe itself must be the reviewed one.
    for key in ("dockerfile", "requirements_lock"):
        rel = br_pol[key]
        path = Path(trusted_root) / rel
        if _contains_link(Path(trusted_root), rel) or not path.is_file():
            problems.append(f"trusted browser recipe file '{rel}' is missing")
        elif _sha256_file(path) != br_pol[f"{key}_sha256"]:
            problems.append(f"trusted browser recipe file '{rel}' does not match its pinned sha256")
    seccomp_actual = _sha256_file(BROWSER_SECCOMP_PROFILE_PATH) if BROWSER_SECCOMP_PROFILE_PATH.is_file() else None
    if seccomp_actual != BROWSER_SECCOMP_PROFILE_SHA256:
        problems.append("browser seccomp profile does not match its pinned sha256")

    info, err = _docker_json(docker, ["image", "inspect", "--format", "{{json .}}", br_pol["tag"]])
    if not isinstance(info, dict):
        problems.append(f"browser runner image '{br_pol['tag']}' is not present: {err}")
    else:
        image_id = info.get("Id", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(image_id)) or info.get("Os") != "linux":
            problems.append("browser runner image has an unexpected identity or platform")
        approval = APPROVED_BROWSER_IMAGES[br_pol["tag"]]
        result["browser"] = {
            "tag": br_pol["tag"], "image_id": image_id,
            "platform": f"{info.get('Os')}/{info.get('Architecture')}",
            "base_image": approval.get("base_image"),
            "expected_playwright_version": approval.get("playwright_version"),
            "expected_browser_version": approval.get("browser_version"),
            "dockerfile_sha256": br_pol["dockerfile_sha256"],
            "requirements_lock_sha256": br_pol["requirements_lock_sha256"],
            "provenance": "built from the trusted recipe (digest-pinned base image, hash-locked pip install); "
                          "the image ID is approved for this run only",
        }
    result["seccomp_profile_sha256"] = seccomp_actual
    return result


def provision_images(policy: Mapping[str, Any], trusted_root: Path, env: Mapping[str, str],
                     docker: str = "docker", python_only: bool = False) -> List[str]:
    """Fetch/build the approved images. Only on GitHub-hosted CI: local hosts never pull or build here."""
    if env.get("GITHUB_ACTIONS") != "true":
        return ["image provisioning only runs inside GitHub Actions; provision images manually on other hosts"]
    py_pol, br_pol = policy["python_image"], policy["browser_image"]
    steps: List[Tuple[str, List[str], float]] = [
        ("pull python image by digest", ["pull", py_pol["repo_digest"]], 600.0),
        ("tag python image", ["tag", py_pol["repo_digest"], py_pol["tag"]], 60.0),
    ]
    if not python_only:
        dockerfile = Path(trusted_root) / br_pol["dockerfile"]
        if _contains_link(Path(trusted_root), br_pol["dockerfile"]) or not dockerfile.is_file() \
                or _sha256_file(dockerfile) != br_pol["dockerfile_sha256"]:
            return ["trusted browser Dockerfile is missing or does not match its pinned sha256"]
        lock = Path(trusted_root) / br_pol["requirements_lock"]
        if not lock.is_file() or _sha256_file(lock) != br_pol["requirements_lock_sha256"]:
            return ["trusted browser requirements lock is missing or does not match its pinned sha256"]
        steps.append(("build browser runner from trusted recipe",
                      ["build", "--tag", br_pol["tag"], "--file", str(dockerfile), str(dockerfile.parent)], 1800.0))
    for label, args, timeout in steps:
        rc, out, err, timed_out, _ = _run_bounded([docker, *args], timeout=timeout, max_bytes=256 * 1024)
        print(f"[brainfrog-ci] {label}: {'timeout' if timed_out else f'exit {rc}'}", flush=True)
        if timed_out or rc != 0:
            return [f"{label} failed: {(err or out).strip()[-300:]}"]
    return []


# =============================================================================
# Verification
# =============================================================================

def run_targets(policy: Mapping[str, Any], trusted_root: Path, candidate_root: Path, work_dir: Path,
                images: Mapping[str, Any], docker: str = "docker") -> List[Dict[str, Any]]:
    """Run every policy target through the standalone verifier with trusted specs and approved images."""
    runs: List[Dict[str, Any]] = []
    br = images.get("browser") or {}
    approvals = {
        policy["browser_image"]["tag"]: {**dict(APPROVED_BROWSER_IMAGES[policy["browser_image"]["tag"]]),
                                          "image_id": br.get("image_id")}
    }
    for target in policy["targets"]:
        tid = target["id"]
        rec: Dict[str, Any] = {"target": tid, "path": target["path"], "spec": target["spec"]}
        runs.append(rec)
        spec_path = Path(trusted_root) / target["spec"]
        if _contains_link(Path(trusted_root), target["spec"]) or not spec_path.is_file():
            rec["error"] = "trusted spec is missing"
            continue
        rec["spec_sha256"] = _sha256_file(spec_path)
        spec_doc = parse_spec_markdown(spec_path.read_text(encoding="utf-8"), spec_path=target["spec"])
        ids = [c.criterion_id for c in spec_doc.criteria]
        missing = sorted(set(target["required_criteria"]) - set(ids))
        if missing or len(set(ids)) != len(ids):
            rec["error"] = f"trusted spec does not define each required criterion exactly once (missing: {missing})"
            continue
        repo = Path(candidate_root) / target["path"]
        if _contains_link(Path(candidate_root), target["path"]) or not repo.is_dir():
            rec["error"] = f"candidate target path '{target['path']}' is missing or is a link"
            continue
        reports = Path(work_dir) / "reports" / tid
        try:
            verifier = StandaloneVerifier(
                repo_dir=repo,
                spec_root=Path(trusted_root),
                sandbox_mode="docker",
                sandbox_backend=DockerSandboxBackend(image_name=policy["python_image"]["tag"], docker_executable=docker),
                browser_backend=DockerBrowserSandbox(browser_image=policy["browser_image"]["tag"],
                                                     docker_executable=docker, approved_browser_images=approvals),
                reports_base_dir=reports,
            )
            report = verifier.verify_spec(target["spec"])
        except Exception as exc:  # recorded and BLOCKED; the exception text is not used to decide anything
            rec["error"] = f"verifier raised {type(exc).__name__}: {safe_scrub(str(exc))[:300]}"
            continue
        rec.update(run_id=report.run_id, exit_code=report.exit_code, overall=report.overall_verdict.value)
    return runs


# =============================================================================
# Decision + evidence bundle
# =============================================================================

def _criterion_table(manifest: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [{"criterion_id": c.get("criterion_id"), "status": c.get("status"),
             "verification_type": c.get("verification_type"), "execution_backend": c.get("execution_backend"),
             "evidence_hashes": c.get("evidence_hashes") or {}} for c in manifest.get("criteria") or []]


def _bundle_hygiene(report_dir: Path, limits: Mapping[str, int]) -> List[str]:
    """Pre-publication checks: no links, bounded size, no private paths, no secret-like content."""
    findings: List[str] = []
    total, count = 0, 0
    for dirpath, dirnames, filenames in os.walk(report_dir, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(report_dir).as_posix()
            if p.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(p)):
                findings.append(f"link in report bundle: {rel}")
                continue
            if not p.is_file():
                continue
            size = p.stat().st_size
            count, total = count + 1, total + size
            if size > limits["max_file_bytes"]:
                findings.append(f"evidence file exceeds size limit: {rel}")
                continue
            if p.suffix.lower() in (".json", ".md", ".log", ".txt"):
                text = p.read_text(encoding="utf-8", errors="replace")
                if _PRIVATE_PATH_RE.search(text):
                    findings.append(f"machine-specific private path in evidence: {rel}")
                if contains_secret_values(text):
                    findings.append(f"secret-like content in evidence: {rel}")
    if count > limits["max_files"] or total > limits["max_total_bytes"]:
        findings.append(f"report bundle exceeds limits ({count} files, {total} bytes)")
    return findings


def evaluate_target(policy_target: Mapping[str, Any], run: Mapping[str, Any], work_dir: Path,
                    limits: Mapping[str, int]) -> Dict[str, Any]:
    """Re-derive one target's outcome from its validated report bundle."""
    out: Dict[str, Any] = {"target": policy_target["id"], "outcome": "blocked", "reasons": [], "criteria": []}
    reasons: List[str] = out["reasons"]
    if run.get("error"):
        reasons.append(run["error"])
        return out
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"verify_[A-Za-z0-9_]{1,80}", run_id):
        reasons.append("verifier produced no report")
        return out
    report_dir = Path(work_dir) / "reports" / policy_target["id"] / run_id
    out["report_dir"] = report_dir
    ok, findings = verify_report_integrity(report_dir)
    if not ok:
        reasons.extend(f"report integrity: {f}" for f in findings[:20])
        return out
    hygiene = _bundle_hygiene(report_dir, limits)
    if hygiene:
        reasons.extend(hygiene[:20])
        return out
    manifest = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    crit = _criterion_table(manifest)
    out["criteria"] = crit
    out["report"] = {k: manifest.get(k) for k in ("run_id", "report_schema", "spec_path", "spec_sha256",
                                                  "overall_verdict", "exit_code", "execution_backend")}
    out["materialized_source_snapshot_digest"] = (manifest.get("snapshot_identity") or {}).get(
        "materialized_source_snapshot_digest")
    if manifest.get("spec_sha256") is None or manifest.get("spec_path") != policy_target["spec"]:
        reasons.append("report was not produced from the trusted spec")
        return out
    ids = [c["criterion_id"] for c in crit]
    missing = sorted(set(policy_target["required_criteria"]) - set(ids))
    if missing:
        reasons.append(f"required criteria missing from report: {missing}")
        return out
    if len(ids) != len(set(ids)) or not ids:
        reasons.append("report criteria are empty or duplicated")
        return out
    statuses = [c["status"] for c in crit]
    if "FAIL" in statuses:
        derived, code = "failure", 1
    elif all(s == "PASS" for s in statuses):
        derived, code = "success", 0
    else:
        derived, code = "blocked", 2
    expected_verdict = {"success": "PASS", "failure": "FAIL", "blocked": "BLOCKED"}[derived]
    # The verifier's own drift detection can downgrade a PASS to BLOCKED; never the reverse.
    if derived == "success" and (manifest.get("overall_verdict") != "PASS" or manifest.get("exit_code") != 0
                                 or run.get("exit_code") != 0):
        reasons.append("report verdict/exit code does not confirm the PASS derived from criteria")
        return out
    if derived != "success" and manifest.get("overall_verdict") not in (expected_verdict, "BLOCKED"):
        reasons.append("report verdict is inconsistent with its criteria")
        return out
    if run.get("exit_code") != manifest.get("exit_code"):
        reasons.append("verifier process exit code differs from the report")
        return out
    if derived == "failure":
        reasons.append("required criteria failed: " + ", ".join(c["criterion_id"] for c in crit if c["status"] == "FAIL"))
    elif derived == "blocked":
        reasons.append("criteria not proven: " + ", ".join(f"{c['criterion_id']}={c['status']}" for c in crit
                                                           if c["status"] != "PASS"))
    out["outcome"] = derived
    out["exit_code"] = code
    return out


def _combine(outcomes: Sequence[str]) -> str:
    if not outcomes or "blocked" in outcomes:
        return "blocked"
    return "failure" if "failure" in outcomes else "success"


def build_evidence(out_dir: Path, metadata: Dict[str, Any], decision: Dict[str, Any],
                   evaluations: Sequence[Mapping[str, Any]]) -> Path:
    """Assemble the publishable bundle: validated report copies, CI metadata, decision and a hash manifest."""
    bundle = Path(out_dir) / EVIDENCE_DIRNAME
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    for ev in evaluations:
        src = ev.get("report_dir")
        # Only bundles that passed integrity + hygiene are published; others are described in decision.json.
        if src is not None and not ev.get("withheld"):
            dst = bundle / "reports" / ev["target"] / Path(src).name
            shutil.copytree(src, dst, symlinks=True)
            ok, findings = verify_report_integrity(dst)
            if not ok:
                shutil.rmtree(dst)
                decision["outcome"] = "blocked"
                decision["reasons"].append(f"{ev['target']}: copied report failed re-verification")
    _write_json(bundle / "ci-metadata.json", metadata)
    _write_json(bundle / "decision.json", decision)
    files = {}
    for p in sorted(bundle.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            files[p.relative_to(bundle).as_posix()] = _sha256_file(p)
    _write_json(bundle / "manifest.json", {
        "schema": CI_MANIFEST_SCHEMA,
        "outcome": decision["outcome"],
        "provenance": "unsigned; bound to the GitHub Actions run recorded in ci-metadata.json",
        "files": files,
    })
    return bundle


def run_gate(trusted_root: Path, candidate_root: Path, out_dir: Path, event: Mapping[str, Any],
             env: Mapping[str, str], docker: str = "docker", base_root: Optional[Path] = None) -> Dict[str, Any]:
    """Identity -> images -> verification -> decision. Always returns (and records) a decision."""
    out_dir = Path(out_dir)
    work = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    reasons: List[str] = []
    metadata: Dict[str, Any] = {"schema": CI_METADATA_SCHEMA, "generated_at": _now(),
                                "runtime_platform": runtime_platform()}
    evaluations: List[Dict[str, Any]] = []
    policy: Optional[Dict[str, Any]] = None
    try:
        policy, policy_digest = load_policy(trusted_root)
        metadata["trusted_policy_digest"] = policy_digest
    except PolicyError as exc:
        reasons.append(f"trusted policy: {exc}")

    identity = collect_identity(event, env, Path(trusted_root), Path(candidate_root), base_root)
    metadata.update({k: v for k, v in identity.items() if k != "problems"})
    reasons.extend(f"identity: {p}" for p in identity["problems"])

    if policy is not None:
        images = inspect_images(policy, Path(trusted_root), docker=docker)
        metadata["python_image_identity"] = images.get("python")
        metadata["browser_image_identity"] = images.get("browser")
        metadata["docker_engine_version"] = images.get("docker_engine_version")
        metadata["seccomp_profile_digest"] = images.get("seccomp_profile_sha256")
        reasons.extend(f"images: {p}" for p in images["problems"])
        if not reasons:
            runs = run_targets(policy, Path(trusted_root), Path(candidate_root), work, images, docker=docker)
            for target in policy["targets"]:
                run = next(r for r in runs if r["target"] == target["id"])
                ev = evaluate_target(target, run, work, policy["evidence_limits"])
                ev["spec_sha256"] = run.get("spec_sha256")
                evaluations.append(ev)
        else:
            reasons.append("verification not started: preconditions failed")
        # Evidence is still produced for self-modifying PRs, but they can never certify themselves.
        changes = trusted_config_changes(policy, Path(trusted_root), Path(candidate_root))
        metadata["candidate_trusted_config_changes"] = changes
        if changes:
            reasons.append("candidate modifies trusted CI configuration (" + ", ".join(changes[:10]) +
                           "); changes take effect only after review and merge, and cannot certify themselves")

    outcome = "blocked" if reasons else _combine([e["outcome"] for e in evaluations])
    for ev in evaluations:
        reasons.extend(f"{ev['target']}: {r}" for r in ev["reasons"])
        # Bundles that failed integrity or hygiene checks are never published.
        ev["withheld"] = ev.get("report_dir") is not None and ev["outcome"] == "blocked" and not ev.get("criteria")
    metadata["targets"] = [{
        "target": ev["target"],
        "outcome": ev["outcome"],
        "trusted_spec_sha256": ev.get("spec_sha256"),
        "verification_plan_digest": (ev.get("report") or {}).get("spec_sha256"),
        "source_snapshot_digest": ev.get("materialized_source_snapshot_digest"),
        "report_run_id": (ev.get("report") or {}).get("run_id"),
        "criterion_results": ev.get("criteria", []),
        "evidence_published": not ev.get("withheld") and ev.get("report_dir") is not None,
    } for ev in evaluations]
    # Per-file hashes of everything published are in manifest.json, which also covers this file.
    metadata["evidence_artifact_hashes"] = "see manifest.json (sha256 of every published file)"
    metadata["overall_verdict"] = {"success": "PASS", "failure": "FAIL", "blocked": "BLOCKED"}[outcome]
    metadata["provenance"] = {
        "kind": "unsigned-ci-context",
        "statement": "Hashes make the bundle tamper-evident; they do not prove who produced it. Authenticated "
                     "provenance requires a verified GitHub artifact attestation over manifest.json.",
    }
    decision = {"schema": CI_METADATA_SCHEMA, "outcome": outcome, "exit_code": OUTCOME_EXIT_CODES[outcome],
                "reasons": reasons}
    build_evidence(out_dir, metadata, decision, evaluations)
    return decision


# =============================================================================
# Enforcement
# =============================================================================

def verify_evidence_bundle(bundle: Path) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Check manifest.json against the bundle (exact file set, hashes, no links); return the decision."""
    findings: List[str] = []
    manifest_path, decision_path = bundle / "manifest.json", bundle / "decision.json"
    if not manifest_path.is_file() or not decision_path.is_file():
        return None, ["evidence bundle is missing manifest.json or decision.json"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
    except ValueError:
        return None, ["evidence manifest or decision is not valid JSON"]
    if not isinstance(manifest, dict) or manifest.get("schema") != CI_MANIFEST_SCHEMA \
            or not isinstance(manifest.get("files"), dict) or not isinstance(decision, dict):
        return None, ["evidence manifest has an unexpected structure"]
    actual: Dict[str, str] = {}
    for p in bundle.rglob("*"):
        rel = p.relative_to(bundle).as_posix()
        if p.is_symlink():
            findings.append(f"link in evidence bundle: {rel}")
        elif p.is_file() and rel != "manifest.json":
            actual[rel] = _sha256_file(p)
    expected = manifest["files"]
    for rel in sorted(set(expected) | set(actual)):
        if rel not in actual:
            findings.append(f"missing evidence file: {rel}")
        elif rel not in expected:
            findings.append(f"unexpected evidence file: {rel}")
        elif expected[rel] != actual[rel]:
            findings.append(f"evidence file modified: {rel}")
    if decision.get("outcome") != manifest.get("outcome") or decision.get("outcome") not in OUTCOME_EXIT_CODES:
        findings.append("decision outcome is missing or inconsistent with the manifest")
    for report in sorted((bundle / "reports").glob("*/*")) if (bundle / "reports").is_dir() else []:
        ok, rep_findings = verify_report_integrity(report)
        if not ok:
            findings.append(f"report {report.relative_to(bundle).as_posix()} failed integrity: {rep_findings[:3]}")
    return decision, findings


def enforce(out_dir: Path) -> int:
    decision, findings = verify_evidence_bundle(Path(out_dir) / EVIDENCE_DIRNAME)
    if findings or decision is None:
        for f in findings:
            print(f"[brainfrog-ci] evidence check: {f}")
        print("[brainfrog-ci] BLOCKED: the evidence bundle could not be validated")
        return OUTCOME_EXIT_CODES["blocked"]
    outcome = decision["outcome"]
    print(f"[brainfrog-ci] verification outcome: {outcome.upper()}")
    for r in decision.get("reasons") or []:
        print(f"[brainfrog-ci]   - {safe_scrub(str(r))[:500]}")
    return OUTCOME_EXIT_CODES[outcome]


def manifest_digest(out_dir: Path) -> Optional[str]:
    path = Path(out_dir) / EVIDENCE_DIRNAME / "manifest.json"
    return _sha256_file(path) if path.is_file() and not path.is_symlink() else None


def _md(value: Any) -> str:
    """Escape a value for a Markdown table cell (no HTML, no pipes, no code spans, no links, one line)."""
    text = re.sub(r"[\r\n]+", " ", str(value))[:300]
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()|#!])", r"\\\1", text)


def write_step_summary(path: Optional[str], decision: Mapping[str, Any], out_dir: Path) -> None:
    if not path:
        return
    meta_path = Path(out_dir) / EVIDENCE_DIRNAME / "ci-metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    lines = [f"## BrainFrog verification: {_md(decision['outcome'].upper())}", "",
             f"Tested revision: {_md(meta.get('tested_sha'))} (PR head {_md(meta.get('head_sha'))}, "
             f"base {_md(meta.get('base_sha'))}); trusted verifier: {_md(meta.get('trusted_verifier_revision'))}", "",
             "| Target | Criterion | Status |", "| --- | --- | --- |"]
    for t in meta.get("targets") or []:
        for c in t.get("criterion_results") or []:
            lines.append(f"| {_md(t['target'])} | {_md(c.get('criterion_id'))} | {_md(c.get('status'))} |")
    lines += ["", "Reasons:"] + [f"- {_md(r)}" for r in decision.get("reasons") or []] + [""]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# =============================================================================
# Candidate self-test (diagnostic job; candidate code runs on the runner)
# =============================================================================

_RESULT_TOKEN = r"(ok|skipped|ERROR|FAIL|expected failure|unexpected success)"
_TEST_HEAD_RE = re.compile(r"^(\w+) \(([\w.]+)\)(.*)$")
_RESULT_TAIL_RE = re.compile(r" \.\.\. " + _RESULT_TOKEN + r"\b")
_RESULT_ALONE_RE = re.compile(r"^" + _RESULT_TOKEN + r"(?: .*)?$")


def parse_unittest_verbose(stderr: str) -> Dict[str, Any]:
    """Parse `python -m unittest -v` stderr into per-test results and error signatures.

    A result is the first ' ... <result>' after a test header, or a line holding only the result
    (when the test or its docstring line wrote output before unittest printed the outcome).
    """
    results: Dict[str, str] = {}
    pending: Optional[str] = None
    for line in stderr.splitlines():
        if line.startswith(("=" * 20, "-" * 20, "Ran ")):
            pending = None
            continue
        head = _TEST_HEAD_RE.match(line)
        rest = line
        if head:
            pending, rest = head.group(2), head.group(3)
        if pending is None:
            continue
        found = _RESULT_TAIL_RE.search(rest) or _RESULT_ALONE_RE.match(rest.strip())
        if found:
            results[pending] = found.group(1)
            pending = None
    signatures: Dict[str, str] = {}
    for block in re.split(r"^={50,}\n", stderr, flags=re.M)[1:]:
        head = re.match(r"(ERROR|FAIL): \w+ \(([\w.]+)\)", block)
        parts = block.split("-" * 70)
        if head and len(parts) > 1:
            # parts[1] is this test's traceback; anything after the next rule is the run trailer.
            tb = [ln for ln in parts[1].strip().splitlines() if ln.strip()]
            last = [ln for ln in tb if not ln.startswith((" ", "Traceback"))]
            signatures[head.group(2)] = (last[-1] if last else "").strip()[:200]
    ran = re.findall(r"^Ran (\d+) tests? in", stderr, re.M)
    return {"results": results, "signatures": signatures, "ran": int(ran[-1]) if ran else None}


def evaluate_selftest_module(module: str, parsed: Mapping[str, Any], returncode: Optional[int],
                             required_ok: Sequence[str], quarantine: Mapping[str, Any]) -> List[str]:
    """Strict rules: every error/failure must be a known, signature-matched quarantine entry; required
    tests must have run and passed (skips are not proof); a module that ran nothing is a failure."""
    problems: List[str] = []
    results, sigs = parsed["results"], parsed["signatures"]
    if returncode is None:
        return [f"{module}: timed out"]
    if not parsed["ran"]:
        problems.append(f"{module}: no tests ran")
    if parsed["ran"] is not None and parsed["ran"] != len(results):
        problems.append(f"{module}: could not attribute every test result ({len(results)} of {parsed['ran']})")
    known = quarantine.get("known_errors") or []
    signature = quarantine.get("signature_prefix") or ""
    for test_id, res in sorted(results.items()):
        if res in ("ERROR", "FAIL", "unexpected success"):
            if test_id not in known or res != "ERROR":
                problems.append(f"{module}: unexpected {res}: {test_id}")
            elif not sigs.get(test_id, "").startswith(signature):
                problems.append(f"{module}: known error {test_id} has a changed signature: {sigs.get(test_id)!r}")
    for test_id in known:
        if results.get(test_id) != "ERROR":
            problems.append(f"{module}: quarantined {test_id} no longer errors ({results.get(test_id)}); "
                            "update the trusted quarantine")
    for test_id in required_ok:
        if test_id.startswith(module + ".") and results.get(test_id) != "ok":
            problems.append(f"{module}: required test {test_id} did not pass ({results.get(test_id)})")
    if returncode != 0 and not known:
        problems.append(f"{module}: exit code {returncode}")
    return problems


def runtime_platform(platform: str = sys.platform) -> Dict[str, str]:
    """The host the gate or self-test actually ran on (recorded as evidence, never read from the event)."""
    return {"sys_platform": platform, "os": _platform.system(), "release": _platform.release(),
            "machine": _platform.machine(), "python": _platform.python_version()}


def quarantine_for_platform(entry: Any, platform: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """The trusted quarantine entry that applies on `platform` (a sys.platform value), and a policy problem.

    An entry must name its platforms explicitly; one that does not, or names another platform, grants no
    exception, so every error on this platform stays a failure (and an unexpected pass is just a pass)."""
    if not entry:
        return {}, None
    platforms = entry.get("platforms") if isinstance(entry, Mapping) else None
    if not (isinstance(platforms, list) and platforms and all(isinstance(p, str) and p for p in platforms)):
        return {}, "quarantine entry has no explicit 'platforms' list; no exception granted"
    return (dict(entry), None) if platform in platforms else ({}, None)


def run_selftest(policy: Mapping[str, Any], candidate_root: Path, out_dir: Path, python_exe: str,
                 platform: str = sys.platform, identity: Optional[Mapping[str, Any]] = None) -> int:
    cfg = policy.get("selftest") or {}
    modules = cfg.get("modules") or []
    required_ok = cfg.get("required_passing_tests") or []
    quarantine = cfg.get("quarantine") or {}
    if not modules or not all(isinstance(m, str) and _TEST_ID_RE.match(m) for m in modules):
        print("[brainfrog-ci] selftest: trusted policy defines no valid modules")
        return 1
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not re.search(r"TOKEN|SECRET|KEY|PASS|AUTH|CREDENTIAL", k.upper())
           and not k.upper().startswith(("GITHUB_", "ACTIONS_", "RUNNER_", "INPUT_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    summary: Dict[str, Any] = {"runtime_platform": runtime_platform(platform), "modules": {}, "problems": []}
    if identity is not None:
        summary["identity"] = {k: identity.get(k) for k in (
            "tested_sha", "head_sha", "base_sha", "trusted_verifier_revision", "trusted_base", "trusted_policy_digest")}
        if identity.get("problems"):
            # Results against an unverified trusted revision would mean nothing: do not run the candidate.
            summary["problems"] = [f"identity: {p}" for p in identity["problems"]]
            _write_json(out_dir / "selftest-summary.json", summary)
            for p in summary["problems"]:
                print(f"[brainfrog-ci] selftest problem: {p}")
            return 2
    timeout = float(cfg.get("module_timeout_seconds", 900))
    for mod in modules:
        try:
            proc = subprocess.run([python_exe, "-m", "unittest", mod, "-v"], cwd=str(candidate_root), env=env,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, shell=False)
            rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc, stdout, stderr = None, str(exc.stdout or ""), str(exc.stderr or "")
        (out_dir / f"{mod}.stderr.log").write_text(safe_scrub(stderr), encoding="utf-8")
        (out_dir / f"{mod}.stdout.log").write_text(safe_scrub(stdout), encoding="utf-8")
        parsed = parse_unittest_verbose(stderr)
        applicable, policy_problem = quarantine_for_platform(quarantine.get(mod), platform)
        problems = evaluate_selftest_module(mod, parsed, rc, required_ok, applicable)
        if policy_problem:
            problems.append(f"{mod}: {policy_problem}")
        counts: Dict[str, int] = {}
        for res in parsed["results"].values():
            counts[res] = counts.get(res, 0) + 1
        summary["modules"][mod] = {"exit_code": rc, "ran": parsed["ran"], "counts": counts,
                                   "skipped": sorted(t for t, r in parsed["results"].items() if r == "skipped")}
        summary["problems"].extend(problems)
        print(f"[brainfrog-ci] selftest {mod}: exit {rc}, ran {parsed['ran']}, {counts}", flush=True)
    for test_id in required_ok:
        if not any(test_id.startswith(m + ".") for m in modules):
            summary["problems"].append(f"required test {test_id} is not in any selftest module")
    _write_json(out_dir / "selftest-summary.json", summary)
    for p in summary["problems"]:
        print(f"[brainfrog-ci] selftest problem: {p}")
    return 1 if summary["problems"] else 0


# =============================================================================
# CLI
# =============================================================================

def _load_event(env: Mapping[str, str]) -> Dict[str, Any]:
    path = env.get("GITHUB_EVENT_PATH")
    if not path or not Path(path).is_file():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="brainfrog_ci", description="Trusted BrainFrog CI gate (Phase 1E)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_prov = sub.add_parser("provision")
    p_prov.add_argument("--trusted", required=True)
    p_prov.add_argument("--python-only", action="store_true")
    p_gate = sub.add_parser("gate")
    p_gate.add_argument("--trusted", required=True)
    p_gate.add_argument("--candidate", required=True)
    p_gate.add_argument("--base", required=True)
    p_gate.add_argument("--out", required=True)
    p_dig = sub.add_parser("digest")
    p_dig.add_argument("--out", required=True)
    p_enf = sub.add_parser("enforce")
    p_enf.add_argument("--out", required=True)
    p_self = sub.add_parser("selftest")
    p_self.add_argument("--trusted", required=True)
    p_self.add_argument("--candidate", required=True)
    p_self.add_argument("--base", required=True)
    p_self.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    env = dict(os.environ)

    if args.cmd == "enforce":
        return enforce(Path(args.out))
    if args.cmd == "digest":
        digest = manifest_digest(Path(args.out))
        if digest is None:
            print("[brainfrog-ci] no evidence manifest to digest")
            return 1
        print(f"[brainfrog-ci] evidence manifest sha256: {digest}")
        if env.get("GITHUB_OUTPUT"):
            with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                fh.write(f"manifest-sha256={digest}\n")
        return 0
    try:
        policy, policy_digest = load_policy(Path(args.trusted))
    except PolicyError as exc:
        if args.cmd != "gate":
            print(f"[brainfrog-ci] BLOCKED: trusted policy: {exc}")
            return 2
        policy = None
    if args.cmd == "provision":
        problems = provision_images(policy, Path(args.trusted), env, python_only=args.python_only)
        for p in problems:
            print(f"[brainfrog-ci] provisioning: {p}")
        return 2 if problems else 0
    if args.cmd == "selftest":
        identity = dict(collect_identity(_load_event(env), env, Path(args.trusted), Path(args.candidate),
                                         Path(args.base)), trusted_policy_digest=policy_digest)
        return run_selftest(policy, Path(args.candidate), Path(args.out), sys.executable, identity=identity)

    out_dir = Path(args.out)
    try:
        decision = run_gate(Path(args.trusted), Path(args.candidate), out_dir, _load_event(env), env,
                            base_root=Path(args.base))
    except Exception as exc:  # last-resort diagnostic: never green, still produces an uploadable bundle
        decision = {"schema": CI_METADATA_SCHEMA, "outcome": "blocked", "exit_code": 2,
                    "reasons": [f"gate crashed: {type(exc).__name__}: {safe_scrub(str(exc))[:300]}"]}
        build_evidence(out_dir, {"schema": CI_METADATA_SCHEMA, "generated_at": _now(), "crashed": True},
                       decision, [])
    write_step_summary(env.get("GITHUB_STEP_SUMMARY"), decision, out_dir)
    print(f"[brainfrog-ci] decision: {decision['outcome'].upper()}")
    for r in decision["reasons"]:
        print(f"[brainfrog-ci]   - {safe_scrub(str(r))[:500]}")
    return OUTCOME_EXIT_CODES[decision["outcome"]]
