"""Shared fixtures for the Phase 1E CI gate tests: disposable git repos, synthetic GitHub event payloads,
hand-built report bundles and a strict YAML-subset loader for the workflow file.

Event payloads are synthetic. Hostile strings in them (branch names, titles) are inert test data and are
never executed; tests assert that they stay inert.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "brainfrog-verify.yml"
POLICY_PATH = REPO_ROOT / ".github" / "brainfrog" / "policy.json"
EVENT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ci_events"

# Files the trusted side of a simulated CI run needs (policy, spec, recipe, workflow).
TRUSTED_FILES = (
    ".github/workflows/brainfrog-verify.yml",
    ".github/brainfrog/policy.json",
    ".github/brainfrog/requirements-verifier.txt",
    ".github/brainfrog/specs/fixture-login-app.md",
    "docker/browser-runner/Dockerfile",
    "docker/browser-runner/requirements.lock",
)


def git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", "-c", "user.name=CI Tester", "-c", "user.email=ci@test.local",
                          "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
                         cwd=repo, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    return path


def commit_all(repo: Path, message: str = "fixture") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def copy_repo_files(dest: Path, rels: Iterable[str]) -> None:
    for rel in rels:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, dest / rel)


def make_trusted_and_candidate(base: Path, with_fixture: Optional[str] = "correct") -> Tuple[Path, Path, str, str]:
    """A trusted repo (policy/spec/recipe/workflow) and a candidate repo carrying identical trusted files
    plus the fixture app at the policy target path. Returns (trusted, candidate, base_sha, tested_sha)."""
    trusted = init_repo(base / "trusted")
    copy_repo_files(trusted, TRUSTED_FILES)
    base_sha = commit_all(trusted, "trusted base")
    candidate = init_repo(base / "candidate")
    copy_repo_files(candidate, TRUSTED_FILES)
    if with_fixture:
        shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "browser_app" / with_fixture,
                        candidate / "tests" / "fixtures" / "browser_app" / "correct")
    tested_sha = commit_all(candidate, "candidate")
    return trusted, candidate, base_sha, tested_sha


def load_event(name: str, **overrides: Any) -> Dict[str, Any]:
    data = json.loads((EVENT_FIXTURES / name).read_text(encoding="utf-8"))
    pr = data["pull_request"]
    for key, value in overrides.items():
        if key == "head_sha":
            pr["head"]["sha"] = value
        elif key == "base_sha":
            pr["base"]["sha"] = value
        else:
            raise KeyError(key)
    return data


def ci_env(github_sha: str, run_attempt: int = 1, event_name: str = "pull_request",
           repository: str = "dameepng/brainfrog") -> Dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_REPOSITORY": repository,
        "GITHUB_SHA": github_sha,
        "GITHUB_RUN_ID": "987654321",
        "GITHUB_RUN_ATTEMPT": str(run_attempt),
        "GITHUB_WORKFLOW_REF": "dameepng/brainfrog/.github/workflows/brainfrog-verify.yml@refs/pull/7/merge",
        "GITHUB_WORKFLOW_SHA": github_sha,
    }


def load_policy_dict() -> Dict[str, Any]:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def write_policy(trusted: Path, policy: Dict[str, Any]) -> None:
    (trusted / ".github" / "brainfrog" / "policy.json").write_text(json.dumps(policy, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# Synthetic report bundles (structure produced by core.verifier, hashes recomputed here)
# ---------------------------------------------------------------------------------------------

SNAPSHOT_DIGEST = "ab" * 32


def make_report_bundle(reports_root: Path, target: str, statuses: Dict[str, str],
                       spec_path: str = ".github/brainfrog/specs/fixture-login-app.md",
                       overall: Optional[str] = None, exit_code: Optional[int] = None,
                       extra_text: str = "") -> Tuple[Path, str]:
    """Write reports_root/<target>/<run_id>/ with one stdout.log per criterion. Returns (dir, run_id)."""
    run_id = "verify_20261021_120000_abcdef_123456"
    rdir = reports_root / target / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    criteria = []
    for cid, status in statuses.items():
        log = f"{cid} {status}\n{extra_text}".encode("utf-8")
        rel = f"evidence/{cid}/stdout.log"
        (rdir / rel).parent.mkdir(parents=True, exist_ok=True)
        (rdir / rel).write_bytes(log)
        criteria.append({
            "criterion_id": cid, "title": cid, "status": status, "verification_type": "test",
            "execution_backend": "oci_container_docker",
            "materialized_source_snapshot_digest": SNAPSHOT_DIGEST,
            "observed_source_snapshot_digest": SNAPSHOT_DIGEST,
            "evidence_files": {"stdout.log": rel},
            "evidence_hashes": {"stdout.log": hashlib.sha256(log).hexdigest()},
        })
    statuses_list = list(statuses.values())
    if overall is None:
        overall = ("FAIL" if "FAIL" in statuses_list else
                   "PASS" if statuses_list and all(s == "PASS" for s in statuses_list) else "BLOCKED")
    if exit_code is None:
        exit_code = {"PASS": 0, "FAIL": 1}.get(overall, 2)
    report = {
        "run_id": run_id, "report_schema": "brainfrog.verification-report/1d", "spec_path": spec_path,
        "spec_sha256": "cd" * 32, "overall_verdict": overall, "exit_code": exit_code,
        "execution_backend": "oci_container_docker",
        "snapshot_identity": {"materialized_source_snapshot_digest": SNAPSHOT_DIGEST},
        "run_evidence_files": {}, "run_evidence_hashes": {}, "criteria": criteria,
    }
    (rdir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return rdir, run_id


def rehash_report(rdir: Path) -> None:
    """Recompute every evidence hash in report.json (simulates a consistent forger)."""
    report = json.loads((rdir / "report.json").read_text(encoding="utf-8"))
    for crit in report["criteria"]:
        for name, rel in crit["evidence_files"].items():
            crit["evidence_hashes"][name] = hashlib.sha256((rdir / rel).read_bytes()).hexdigest()
    (rdir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# Strict YAML-subset loader (no PyYAML in the locked dependency set)
# ---------------------------------------------------------------------------------------------

class YamlSubsetError(ValueError):
    pass


_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+):(?: (.*))?$")


def _strip_comment(text: str) -> str:
    out: List[str] = []
    quote: Optional[str] = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'" and (i == 0 or text[i - 1] in " [,:"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] == " "):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(text: str) -> Any:
    s = text.strip()
    if not s or s in ("null", "~"):
        return None
    if s[0] in "&*!{>%@`":
        raise YamlSubsetError(f"unsupported YAML construct: {s[:40]!r}")
    if s[0] == '"':
        if not s.endswith('"') or len(s) < 2:
            raise YamlSubsetError(f"unterminated string: {s[:40]!r}")
        return json.loads(s)
    if s[0] == "'":
        if not s.endswith("'") or len(s) < 2:
            raise YamlSubsetError(f"unterminated string: {s[:40]!r}")
        return s[1:-1].replace("''", "'")
    if s[0] == "[":
        if not s.endswith("]") or "[" in s[1:-1] or "{" in s:
            raise YamlSubsetError(f"only flat flow sequences are supported: {s[:40]!r}")
        inner = s[1:-1].strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    if s in ("true", "false"):
        return s == "true"
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


class _Loader:
    def __init__(self, text: str) -> None:
        if "\t" in text:
            raise YamlSubsetError("tabs are not allowed")
        self.lines = text.splitlines()
        self.pos = 0

    @staticmethod
    def _indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def _next(self) -> Optional[int]:
        i = self.pos
        while i < len(self.lines):
            stripped = self.lines[i].strip()
            if stripped and not stripped.startswith("#"):
                return i
            i += 1
        return None

    def node(self, indent: int) -> Any:
        i = self._next()
        if i is None:
            return None
        content = self.lines[i].strip()
        if content == "-" or content.startswith("- "):
            return self.seq(self._indent(self.lines[i]))
        return self.mapping(self._indent(self.lines[i]))

    def block_scalar(self, parent_indent: int) -> str:
        collected: List[str] = []
        block_indent: Optional[int] = None
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.strip() == "":
                collected.append("")
                self.pos += 1
                continue
            ind = self._indent(line)
            if ind <= parent_indent:
                break
            block_indent = ind if block_indent is None else block_indent
            if ind < block_indent:
                break
            collected.append(line[block_indent:])
            self.pos += 1
        while collected and collected[-1] == "":
            collected.pop()
        return "\n".join(collected) + "\n"

    def mapping(self, indent: int) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        while True:
            i = self._next()
            if i is None:
                return result
            line = self.lines[i]
            ind = self._indent(line)
            if ind < indent:
                return result
            if ind > indent:
                raise YamlSubsetError(f"line {i + 1}: unexpected indentation")
            content = _strip_comment(line.strip())
            if content.startswith("-"):
                return result
            m = _KEY_RE.match(content)
            if not m:
                raise YamlSubsetError(f"line {i + 1}: expected 'key: value': {content[:60]!r}")
            key, rest = m.group(1), (m.group(2) or "").strip()
            if key in result:
                raise YamlSubsetError(f"line {i + 1}: duplicate key {key!r}")
            self.pos = i + 1
            if rest == "|":
                result[key] = self.block_scalar(indent)
            elif rest in (">", "|-", "|+", ">-"):
                raise YamlSubsetError(f"line {i + 1}: unsupported block scalar style {rest!r}")
            elif rest == "":
                j = self._next()
                if j is not None and self._indent(self.lines[j]) > indent:
                    result[key] = self.node(self._indent(self.lines[j]))
                elif j is not None and self._indent(self.lines[j]) == indent and self.lines[j].strip().startswith("- "):
                    result[key] = self.seq(indent)
                else:
                    result[key] = None
            else:
                result[key] = _scalar(rest)

    def seq(self, indent: int) -> List[Any]:
        items: List[Any] = []
        while True:
            i = self._next()
            if i is None:
                return items
            line = self.lines[i]
            ind = self._indent(line)
            content = line.strip()
            if ind < indent or not (content == "-" or content.startswith("- ")):
                return items
            if ind > indent:
                raise YamlSubsetError(f"line {i + 1}: unexpected indentation in sequence")
            after = content[1:].lstrip(" ")
            if not after:
                self.pos = i + 1
                j = self._next()
                items.append(self.node(self._indent(self.lines[j])) if j is not None else None)
                continue
            item_indent = ind + (len(content) - len(after))
            if _KEY_RE.match(_strip_comment(after)):
                self.lines[i] = " " * item_indent + after  # re-read the item as a mapping at its own column
                items.append(self.mapping(item_indent))
            else:
                self.pos = i + 1
                items.append(_scalar(_strip_comment(after)))


def load_yaml_subset(text: str) -> Any:
    loader = _Loader(text)
    data = loader.node(0)
    if loader._next() is not None:
        raise YamlSubsetError(f"line {loader._next() + 1}: trailing content not parsed")
    return data


def workflow() -> Dict[str, Any]:
    return load_yaml_subset(WORKFLOW_PATH.read_text(encoding="utf-8"))


def deep(data: Any) -> Any:
    return copy.deepcopy(data)
