"""Trusted-base resolution for the BrainFrog pull_request gate (Phase 1E).

GitHub tests a synthetic merge commit (refs/pull/N/merge) whose first parent is the base branch tip at the
time the merge was computed and whose second parent is the PR head. The event's `pull_request.base.sha` is
not that parent: it is not refreshed when the base branch moves (T1 retest, run 39731332404, recorded a
stale base.sha while the merge was built on the new base). The trusted verifier and policy must therefore
come from the merge's verified first parent, never from the event's base.sha, the PR head or the merge.

Standard library only: the workflow runs this file directly (`python -I base/core/ci_trust.py resolve`)
from the base-branch checkout before any dependency is installed, and core.ci_gate imports the same
functions to re-verify the result from the trusted checkout. Every check fails closed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

TRUST_SCHEMA = "brainfrog.ci-trust/1e"
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}$")


def _safe_ref(ref: Any) -> bool:
    return (isinstance(ref, str) and bool(_REF_RE.match(ref)) and ".." not in ref and "//" not in ref
            and not ref.endswith(("/", ".", ".lock")) and "/." not in ref)


def _sha(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _SHA1_RE.match(value) else None


def git(repo: Path, *args: str) -> Optional[str]:
    """Run git in repo with hooks, fsmonitor and global/system config disabled. None on any failure."""
    env = {k: v for k, v in os.environ.items() if k.upper() in ("PATH", "SYSTEMROOT", "TEMP", "TMP")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                "HOME": tempfile.gettempdir()})
    argv = ["git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
            "-c", f"safe.directory={Path(repo).resolve().as_posix()}", "-C", str(repo), *args]
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env, shell=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def commit_parents(repo: Path, sha: str) -> Optional[List[str]]:
    """Parent SHAs from the commit object itself (works in a depth-1 checkout). None if unreadable."""
    raw = git(repo, "cat-file", "commit", sha)
    if raw is None:
        return None
    parents = []
    for line in raw.split("\n"):
        if not line:
            break
        if line.startswith("parent "):
            parents.append(line[len("parent "):])
    return parents if all(_SHA1_RE.match(p) for p in parents) else None


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> Optional[bool]:
    """True/False from `git merge-base --is-ancestor`; None if either commit is not in repo."""
    for sha in (ancestor, descendant):
        if git(repo, "cat-file", "-e", f"{sha}^{{commit}}") is None:
            return None
    return git(repo, "merge-base", "--is-ancestor", ancestor, descendant) is not None


def _normalized_url(url: str) -> str:
    url = url.strip().rstrip("/")
    return url[:-4] if url.endswith(".git") else url


def resolve_trusted_base(event: Mapping[str, Any], env: Mapping[str, str], candidate_root: Path,
                         base_root: Optional[Path]) -> Dict[str, Any]:
    """Verify the tested merge and return its first parent as the trusted revision.

    `trusted_base_sha` is set only when every check passed; otherwise `problems` says why (gate: BLOCKED)."""
    problems: List[str] = []
    pr = event.get("pull_request") if isinstance(event.get("pull_request"), dict) else {}
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    number = event.get("number", pr.get("number"))
    repository = env.get("GITHUB_REPOSITORY", "")
    github_sha, head_sha, event_base_sha = _sha(env.get("GITHUB_SHA")), _sha(head.get("sha")), _sha(base.get("sha"))
    base_ref = base.get("ref")

    if env.get("GITHUB_EVENT_NAME") != "pull_request":
        problems.append("not a pull_request run")
    if not _REPO_RE.match(repository):
        problems.append("GITHUB_REPOSITORY is missing or malformed")
    if base_repo.get("full_name") != repository:
        problems.append("pull_request.base.repo is not this repository")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        problems.append("pull request number is missing or malformed")
    elif env.get("GITHUB_REF") != f"refs/pull/{number}/merge":
        problems.append(f"GITHUB_REF is not refs/pull/{number}/merge: the run is not testing GitHub's PR merge")
    if github_sha is None:
        problems.append("GITHUB_SHA is missing or not a 40-character commit SHA")
    if head_sha is None:
        problems.append("pull_request.head.sha is missing or not a 40-character commit SHA")
    if not _safe_ref(base_ref):
        problems.append("pull_request.base.ref is missing or not a plain branch name")
        base_ref = None
    elif env.get("GITHUB_BASE_REF") not in (None, base_ref):
        problems.append("GITHUB_BASE_REF disagrees with pull_request.base.ref")

    tested_sha = _sha(git(candidate_root, "rev-parse", "--verify", "HEAD^{commit}"))
    parents: Optional[List[str]] = None
    first_parent: Optional[str] = None
    if tested_sha is None:
        problems.append("candidate checkout HEAD could not be read")
    else:
        if github_sha is not None and tested_sha != github_sha:
            problems.append(f"candidate checkout is at {tested_sha}, but the run is for GITHUB_SHA {github_sha}")
        parents = commit_parents(candidate_root, tested_sha)
        if parents is None:
            problems.append("tested merge commit could not be read")
        elif len(parents) != 2:
            problems.append(f"tested revision has {len(parents)} parent(s); a PR merge commit has exactly 2")
        else:
            first_parent = parents[0]
            if head_sha is not None and parents[1] != head_sha:
                problems.append(f"merge second parent {parents[1]} is not the PR head {head_sha}")

    base_tip: Optional[str] = None
    if base_root is None:
        problems.append("no base-branch checkout was provided; the merge base cannot be verified")
    else:
        base_tip = _sha(git(base_root, "rev-parse", "--verify", "HEAD^{commit}"))
        server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        origin = git(base_root, "config", "--get", "remote.origin.url")
        if base_tip is None:
            problems.append("base-branch checkout HEAD could not be read")
        elif origin is None or _normalized_url(origin) != f"{server}/{repository}":
            problems.append("base-branch checkout was not fetched from this repository")
        elif base_ref is not None:
            if git(base_root, "symbolic-ref", "-q", "HEAD") != f"refs/heads/{base_ref}":
                problems.append("base-branch checkout is not on the PR base branch")
            elif _sha(git(base_root, "rev-parse", "--verify", f"refs/remotes/origin/{base_ref}^{{commit}}")) != base_tip:
                problems.append("base-branch checkout does not match the fetched base branch")
            elif first_parent is not None:
                related = is_ancestor(base_root, first_parent, base_tip)
                if related is None:
                    problems.append(f"merge first parent {first_parent} is not a commit of the base branch history")
                elif not related:
                    problems.append(f"merge first parent {first_parent} is not on base branch '{base_ref}'")

    trusted = first_parent if not problems else None
    return {
        "schema": TRUST_SCHEMA,
        "tested_sha": tested_sha,
        "merge_parents": parents,
        "pr_head_sha": head_sha,
        "base_ref": base_ref,
        "base_branch_tip": base_tip,
        "trusted_base_sha": trusted,
        "trusted_base_is_branch_tip": trusted is not None and trusted == base_tip,
        # Diagnostic only: the event's base.sha is not used to choose the trusted revision.
        "event_base_sha": event_base_sha,
        "event_base_sha_is_stale": first_parent is not None and event_base_sha != first_parent,
        "problems": problems,
    }


def load_event(env: Mapping[str, str]) -> Dict[str, Any]:
    path = env.get("GITHUB_EVENT_PATH")
    if not path or not Path(path).is_file():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ci_trust", description="Resolve the trusted PR base (Phase 1E)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_res = sub.add_parser("resolve")
    p_res.add_argument("--candidate", required=True)
    p_res.add_argument("--base", required=True)
    args = parser.parse_args(argv)
    env = dict(os.environ)
    result = resolve_trusted_base(load_event(env), env, Path(args.candidate), Path(args.base))
    for key in ("tested_sha", "merge_parents", "pr_head_sha", "base_ref", "base_branch_tip", "trusted_base_sha",
                "event_base_sha", "event_base_sha_is_stale"):
        print(f"[brainfrog-ci] {key}: {json.dumps(result[key])}")
    if result["problems"]:
        print("[brainfrog-ci] BLOCKED: the trusted base revision could not be verified")
        for p in result["problems"]:
            print(f"[brainfrog-ci]   - {p}")
        return 2
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"trusted-sha={result['trusted_base_sha']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
