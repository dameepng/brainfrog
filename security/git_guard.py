"""Git Secret Guard — deterministic pre-commit and pre-push secret scanner.

Implements OWASP Secrets Management & Zero Secret Exposure guidelines:
- Scans staged files and added diff lines before `git commit` or `git push`
- Detects encryption keys, private keys, API keys, tokens, and sensitive files (.env, .pem, etc.)
- Automatically redacts secret values in log messages and alerts
- Blocks commit and push immediately if secrets are detected
- Ensures `.env` and sensitive patterns are protected in `.gitignore`
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class SecretFinding:
    rule: str
    file_path: str
    line_number: Optional[int]
    redacted_snippet: str
    severity: str = "CRITICAL"


@dataclass
class ScanResult:
    is_clean: bool
    findings: List[SecretFinding] = field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Detection Rules & Signatures
# ---------------------------------------------------------------------------

SECRET_PATTERNS: List[Tuple[str, re.Pattern]] = [
    # 1. Private keys & Certificates
    (
        "Private Key Header",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "PGP Private Key Block",
        re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----"),
    ),
    # 2. Generic Encryption & Crypto Keys (common GitGuardian trigger)
    # Supports JSON quotes ("encryptionKey": "..."), JS objects, camelCase, snake_case
    (
        "Generic Encryption Key",
        re.compile(
            r"""(?i)['"]?(?:encryption|crypto|cipher|aes|des)[_-]?(?:key|secret)['"]?\s*[:=]\s*['"]([a-zA-Z0-9_\-\+/=]{16,})['"]"""
        ),
    ),
    (
        "Secret Key Assignment",
        re.compile(
            r"""(?i)['"]?(?:secret[_-]?key|jwt[_-]?secret|session[_-]?secret|auth[_-]?secret|app[_-]?secret|api[_-]?secret|signing[_-]?secret|client[_-]?secret)['"]?\s*[:=]\s*['"]([a-zA-Z0-9_\-\+/=]{16,})['"]"""
        ),
    ),
    (
        "Generic High-Entropy Secret / Token",
        re.compile(
            r"""(?i)['"]?(?:access[_-]?token|auth[_-]?token|private[_-]?key|master[_-]?key|signing[_-]?key|secret[_-]?token)['"]?\s*[:=]\s*['"]([a-zA-Z0-9_\-\+/=]{16,})['"]"""
        ),
    ),
    (
        "Environment Fallback Secret",
        re.compile(
            r"""(?i)(?:process\.env\.[A-Z0-9_]+|os\.environ(?:\[.+?\]|\.get\(.+?\)))\s*(?:\|\||\?\?|,)\s*['"]([a-zA-Z0-9_\-\+/=]{16,})['"]"""
        ),
    ),
    # 3. Cloud & AI Provider Keys
    (
        "Anthropic API Key",
        re.compile(r"sk-ant-[a-zA-Z0-9_\-]{20,}"),
    ),
    (
        "OpenAI API Key",
        re.compile(r"sk-(?:proj-)?[a-zA-Z0-9_\-]{20,}"),
    ),
    (
        "GitHub Personal Access Token",
        re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[a-zA-Z0-9]{36,}|github_pat_[a-zA-Z0-9_]{50,}"),
    ),
    (
        "Google API Key",
        re.compile(r"AIza[0-9A-Za-z\\-_]{35}"),
    ),
    (
        "AWS Access Key ID",
        re.compile(r"(?<![A-Z0-9])(?:AKIA|ABIA|ACCA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    ),
    (
        "AWS Secret Access Key",
        re.compile(r"""(?i)['"]?aws[_-]?secret[_-]?access[_-]?key['"]?\s*[:=]\s*['"]([0-9a-zA-Z/+=]{40})['"]"""),
    ),
    (
        "Slack Token",
        re.compile(r"xox[baprs]-[0-9a-zA-Z]{10,48}"),
    ),
    (
        "Stripe Secret Key",
        re.compile(r"(?:sk|rk)_(?:live|test)_[0-9a-zA-Z]{24,}"),
    ),
    (
        "SendGrid API Key",
        re.compile(r"SG\.[a-zA-Z0-9_\-]{22}\.[a-zA-Z0-9_\-]{43}"),
    ),
    (
        "Generic High-Entropy API Key",
        re.compile(r"""(?i)['"]?api[_-]?key['"]?\s*[:=]\s*['"]([a-zA-Z0-9_\-]{20,})['"]"""),
    ),
]

# Sensitive file path patterns that must never be committed
SENSITIVE_FILE_PATTERNS: List[re.Pattern] = [
    re.compile(r"(^|[/\\])\.env(\.[a-zA-Z0-9_\-]+)?$", re.IGNORECASE),
    re.compile(r"\.(pem|key|pkcs12|p12|pfx)$", re.IGNORECASE),
    re.compile(r"(^|[/\\])id_(rsa|ed25519|ecdsa|dsa)$", re.IGNORECASE),
    re.compile(r"(service[-_]?account|client[-_]?secret).*\.json$", re.IGNORECASE),
    # Build artifacts, caches, and manifests containing generated secrets
    re.compile(r"(^|[/\\])\.next([/\\]|$)", re.IGNORECASE),
    re.compile(r"(^|[/\\])node_modules([/\\]|$)", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.brainfrog[/\\]scratch([/\\]|$)", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.brainfrog[/\\]sessions([/\\]|$)", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.brainfrog[/\\]approvals([/\\]|$)", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.brainfrog[/\\]transactions([/\\]|$)", re.IGNORECASE),
    re.compile(r"server-reference-manifest\.json$", re.IGNORECASE),
    re.compile(r"(^|[/\\])(dist|build|out)[/\\]", re.IGNORECASE),
]

SAFE_FILE_EXCLUSIONS: List[re.Pattern] = [
    re.compile(r"\.example(\.|$)", re.IGNORECASE),
    re.compile(r"\.sample(\.|$)", re.IGNORECASE),
    re.compile(r"\.template(\.|$)", re.IGNORECASE),
    re.compile(r"\.md$", re.IGNORECASE),
    re.compile(r"(^|[/\\])git_guard\.py$", re.IGNORECASE),
    re.compile(r"(^|[/\\])test_git_guard\.py$", re.IGNORECASE),
]

# Values that are clearly non-secrets (placeholders, env lookups, mocks)
SAFE_VALUE_SUBSTRINGS: List[str] = [
    "placeholder", "your_", "your-", "your.", "<your", "[your",
    "dummy", "test", "example", "mock", "fake", "xxxx", "replace_me",
    "change_me", "process.env", "os.environ", "os.getenv", "env.",
    "system.getenv", "config(", "settings.",
]


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def redact(secret_val: str) -> str:
    """Mask secret value safely so it never appears in cleartext in logs/screen."""
    if len(secret_val) <= 8:
        return "***"
    return f"{secret_val[:4]}...{secret_val[-4:]}"


def is_safe_value(val: str) -> bool:
    """Return True if the matched string is a benign placeholder or variable lookup."""
    val_lower = val.lower()
    return any(sub in val_lower for sub in SAFE_VALUE_SUBSTRINGS)


def scan_file_path(path_str: str) -> Optional[SecretFinding]:
    """Check if the filename itself matches a sensitive file pattern."""
    if any(p.search(path_str) for p in SAFE_FILE_EXCLUSIONS):
        return None
    for pattern in SENSITIVE_FILE_PATTERNS:
        if pattern.search(path_str):
            return SecretFinding(
                rule="Sensitive File Staged",
                file_path=path_str,
                line_number=None,
                redacted_snippet=f"Attempting to commit sensitive file: {path_str}",
            )
    return None


def scan_text_content(file_path: str, content: str) -> List[SecretFinding]:
    """Scan code content line-by-line for leaked secrets."""
    if any(p.search(file_path) for p in SAFE_FILE_EXCLUSIONS):
        return []

    findings: List[SecretFinding] = []
    lines = content.splitlines()
    for idx, line in enumerate(lines, 1):
        clean_line = line.strip()
        # Skip pure comments
        if clean_line.startswith(("#", "//", "/*", "*")):
            continue

        for rule_name, pattern in SECRET_PATTERNS:
            match = pattern.search(clean_line)
            if match:
                matched_val = match.group(1) if match.groups() else match.group(0)
                if is_safe_value(matched_val):
                    continue
                redacted_line = clean_line.replace(matched_val, redact(matched_val))
                findings.append(
                    SecretFinding(
                        rule=rule_name,
                        file_path=file_path,
                        line_number=idx,
                        redacted_snippet=redacted_line[:140],
                    )
                )
                break
    return findings


# ---------------------------------------------------------------------------
# Git Staged Scanner
# ---------------------------------------------------------------------------

def scan_staged_changes(repo_dir: Path) -> ScanResult:
    """Scan all currently staged files and diffs in the given Git repo.

    Returns ScanResult with is_clean=True if safe, or is_clean=False with findings.
    """
    findings: List[SecretFinding] = []

    # 1. Check staged file paths (excluding deleted files via --diff-filter=AMCR)
    proc_files = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=AMCR"],
        cwd=repo_dir,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc_files.returncode != 0:
        return ScanResult(
            is_clean=False,
            findings=[
                SecretFinding(
                    rule="Git Execution Error",
                    file_path="git diff --cached --name-only",
                    line_number=None,
                    redacted_snippet=f"git diff failed: {proc_files.stderr.strip()[:140]}",
                    severity="CRITICAL",
                )
            ],
            summary="Git execution error while inspecting staged files.",
        )
    if proc_files.stdout:
        staged_files = [f.strip() for f in proc_files.stdout.splitlines() if f.strip()]
        for file_path in staged_files:
            finding = scan_file_path(file_path)
            if finding:
                findings.append(finding)

    # 2. Check added lines in diff
    proc_diff = subprocess.run(
        ["git", "diff", "--cached", "-U0"],
        cwd=repo_dir,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc_diff.returncode != 0:
        return ScanResult(
            is_clean=False,
            findings=[
                SecretFinding(
                    rule="Git Execution Error",
                    file_path="git diff --cached -U0",
                    line_number=None,
                    redacted_snippet=f"git diff failed: {proc_diff.stderr.strip()[:140]}",
                    severity="CRITICAL",
                )
            ],
            summary="Git execution error while inspecting staged diff.",
        )
    if proc_diff.stdout and proc_diff.stdout.strip():
        current_file = ""
        line_num = 0
        for line in proc_diff.stdout.splitlines():
            if line.startswith("+++ b/"):
                current_file = line[6:].strip()
                line_num = 0
                continue
            if line.startswith("@@"):
                # Parse chunk header e.g. @@ -1,4 +1,6 @@
                m = re.search(r"\+(\d+)", line)
                if m:
                    line_num = int(m.group(1)) - 1
                continue
            if line.startswith("+") and not line.startswith("+++"):
                line_num += 1
                added_content = line[1:].strip()
                if not added_content:
                    continue
                # Skip comments
                if added_content.startswith(("#", "//", "/*", "*")):
                    continue
                # Skip excluded safe files
                if any(p.search(current_file) for p in SAFE_FILE_EXCLUSIONS):
                    continue

                for rule_name, pattern in SECRET_PATTERNS:
                    match = pattern.search(added_content)
                    if match:
                        matched_val = match.group(1) if match.groups() else match.group(0)
                        if is_safe_value(matched_val):
                            continue
                        redacted_line = added_content.replace(matched_val, redact(matched_val))
                        findings.append(
                            SecretFinding(
                                rule=rule_name,
                                file_path=current_file,
                                line_number=line_num if line_num > 0 else None,
                                redacted_snippet=redacted_line[:140],
                            )
                        )
                        break

    if findings:
        summary = f"{len(findings)} secret incident(s) detected: " + ", ".join(
            f"{f.rule} in {f.file_path}" for f in findings[:3]
        )
        if len(findings) > 3:
            summary += f" and {len(findings) - 3} more"
        return ScanResult(is_clean=False, findings=findings, summary=summary)

    return ScanResult(is_clean=True, findings=[], summary="No secrets detected in staged changes.")


def scan_dict_files(files: Dict[str, str]) -> ScanResult:
    """Scan in-memory dict of {file_path: content} before writing to disk."""
    findings: List[SecretFinding] = []
    for fpath, content in files.items():
        path_finding = scan_file_path(fpath)
        if path_finding:
            findings.append(path_finding)
        content_findings = scan_text_content(fpath, content)
        findings.extend(content_findings)

    if findings:
        summary = f"{len(findings)} secret incident(s) detected in generated files: " + ", ".join(
            f"{f.rule} in {f.file_path}" for f in findings[:3]
        )
        return ScanResult(is_clean=False, findings=findings, summary=summary)
    return ScanResult(is_clean=True, findings=[], summary="Generated files clean.")


def unstage_staged_changes(repo_dir: Path) -> None:
    """Unstage all staged files to safely protect the workspace."""
    subprocess.run(["git", "reset"], cwd=repo_dir, capture_output=True, encoding="utf-8", errors="replace")


def ensure_gitignore_security(repo_dir: Path) -> bool:
    """Ensure .env, build directories, and sensitive patterns are included in .gitignore."""
    gitignore_path = repo_dir / ".gitignore"
    required_entries = [
        ".env",
        ".env.*",
        "!.env.example",
        "*.pem",
        "*.key",
        ".next/",
        ".brainfrog/scratch/",
        ".brainfrog/sessions/",
        ".brainfrog/approvals/",
        ".brainfrog/transactions/",
        "node_modules/",
        "dist/",
        "build/",
        "out/",
    ]
    existing_content = ""
    if gitignore_path.exists():
        try:
            existing_content = gitignore_path.read_text(encoding="utf-8")
        except Exception:
            return False

    missing = [entry for entry in required_entries if entry not in existing_content]
    if not missing:
        return False

    addition = "\n# Security: protect credentials & secrets & build caches\n" + "\n".join(missing) + "\n"
    try:
        with gitignore_path.open("a", encoding="utf-8") as f:
            f.write(addition)
        return True
    except Exception:
        return False


def purge_tracked_sensitive_files(repo_dir: Path) -> List[str]:
    """Untrack known sensitive or build directories (.next, .brainfrog/scratch, node_modules) if tracked in Git index."""
    patterns_to_check = [".next", ".brainfrog/scratch", ".brainfrog/sessions", ".brainfrog/approvals", ".brainfrog/transactions", "node_modules"]
    untracked: List[str] = []
    for pattern in patterns_to_check:
        proc = subprocess.run(
            ["git", "ls-files", pattern],
            cwd=repo_dir,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 0 and proc.stdout and proc.stdout.strip():
            rm_proc = subprocess.run(
                ["git", "rm", "-r", "--cached", "--quiet", pattern],
                cwd=repo_dir,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
            if rm_proc.returncode == 0:
                untracked.append(pattern)
    return untracked


def scan_commit_range(repo_dir: Path, rev_range: str) -> ScanResult:
    """Scan all files and diff lines across a Git revision range (e.g. origin/main..HEAD).

    Returns ScanResult with is_clean=True if safe, or is_clean=False with findings.
    """
    findings: List[SecretFinding] = []

    # 1. Check touched file paths for sensitive file names
    proc_files = subprocess.run(
        ["git", "diff", rev_range, "--name-only", "--diff-filter=AMCR"],
        cwd=repo_dir,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc_files.returncode != 0:
        return ScanResult(
            is_clean=False,
            findings=[
                SecretFinding(
                    rule="Git Execution Error",
                    file_path=rev_range,
                    line_number=None,
                    redacted_snippet=f"git diff --name-only failed: {proc_files.stderr.strip()[:140]}",
                    severity="CRITICAL",
                )
            ],
            summary=f"Git execution error while scanning revision range {rev_range}",
        )
    if proc_files.stdout:
        touched_files = [f.strip() for f in proc_files.stdout.splitlines() if f.strip()]
        for file_path in touched_files:
            finding = scan_file_path(file_path)
            if finding:
                findings.append(finding)

    # 2. Check added lines in diff
    proc_diff = subprocess.run(
        ["git", "diff", rev_range, "-U0"],
        cwd=repo_dir,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc_diff.returncode != 0:
        return ScanResult(
            is_clean=False,
            findings=[
                SecretFinding(
                    rule="Git Execution Error",
                    file_path=rev_range,
                    line_number=None,
                    redacted_snippet=f"git diff failed: {proc_diff.stderr.strip()[:140]}",
                    severity="CRITICAL",
                )
            ],
            summary=f"Git execution error while scanning revision range {rev_range}",
        )
    if proc_diff.stdout and proc_diff.stdout.strip():
        current_file = ""
        line_num = 0
        for line in proc_diff.stdout.splitlines():
            if line.startswith("+++ b/"):
                current_file = line[6:].strip()
                line_num = 0
                continue
            if line.startswith("@@"):
                m = re.search(r"\+(\d+)", line)
                if m:
                    line_num = int(m.group(1)) - 1
                continue
            if line.startswith("+") and not line.startswith("+++"):
                line_num += 1
                added_content = line[1:].strip()
                if not added_content:
                    continue
                if added_content.startswith(("#", "//", "/*", "*")):
                    continue
                if any(p.search(current_file) for p in SAFE_FILE_EXCLUSIONS):
                    continue

                for rule_name, pattern in SECRET_PATTERNS:
                    match = pattern.search(added_content)
                    if match:
                        matched_val = match.group(1) if match.groups() else match.group(0)
                        if is_safe_value(matched_val):
                            continue
                        redacted_line = added_content.replace(matched_val, redact(matched_val))
                        findings.append(
                            SecretFinding(
                                rule=rule_name,
                                file_path=current_file,
                                line_number=line_num if line_num > 0 else None,
                                redacted_snippet=redacted_line[:140],
                            )
                        )
                        break

    if findings:
        summary = f"{len(findings)} secret incident(s) detected in range {rev_range}: " + ", ".join(
            f"{f.rule} in {f.file_path}" for f in findings[:3]
        )
        if len(findings) > 3:
            summary += f" and {len(findings) - 3} more"
        return ScanResult(is_clean=False, findings=findings, summary=summary)

    return ScanResult(is_clean=True, findings=[], summary=f"No secrets detected in revision range {rev_range}.")


def scan_pre_push(
    repo_dir: Path,
    remote_name: str,
    remote_url: str,
    stdin_content: Optional[str] = None,
) -> ScanResult:
    """Scan outgoing refs and commits according to Git's pre-push hook stdin protocol.

    Git provides stdin lines of the format:
        <local ref> <local sha> <remote ref> <remote sha>
    """
    import sys

    if stdin_content is None:
        if sys.stdin.isatty():
            return ScanResult(is_clean=True, findings=[], summary="Interactive terminal: no push stdin provided.")
        try:
            stdin_content = sys.stdin.read()
        except Exception as e:
            return ScanResult(
                is_clean=False,
                findings=[
                    SecretFinding(
                        rule="Pre-Push Hook Read Error",
                        file_path="stdin",
                        line_number=None,
                        redacted_snippet=f"Failed to read pre-push stdin: {str(e)[:140]}",
                        severity="CRITICAL",
                    )
                ],
                summary="Pre-push hook stdin read error (failing closed).",
            )

    lines = [ln.strip() for ln in (stdin_content or "").splitlines() if ln.strip()]
    if not lines:
        return ScanResult(is_clean=True, findings=[], summary="No refs pushed; scan skipped.")

    is_zero = lambda sha: not sha or set(sha) == {"0"}
    all_findings: List[SecretFinding] = []

    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts

        # 1. Ref deletion: local_sha is all zeros (e.g. git push origin --delete branch)
        if is_zero(local_sha):
            continue

        # 2. Existing branch update: remote_sha is non-zero
        if not is_zero(remote_sha):
            if local_sha == remote_sha:
                continue
            rev_range = f"{remote_sha}..{local_sha}"
            res = scan_commit_range(repo_dir, rev_range)
            if not res.is_clean:
                all_findings.extend(res.findings)
            continue

        # 3. New branch or ref creation: remote_sha is all zeros
        # Inspect commits reachable from local_sha not already present on remote
        has_remotes_proc = subprocess.run(
            ["git", "for-each-ref", "--count=1", f"refs/remotes/{remote_name}"],
            cwd=repo_dir,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        if has_remotes_proc.returncode != 0:
            return ScanResult(
                is_clean=False,
                findings=[
                    SecretFinding(
                        rule="Git Inspection Error",
                        file_path=remote_name,
                        line_number=None,
                        redacted_snippet=f"git for-each-ref failed: {has_remotes_proc.stderr.strip()[:140]}",
                        severity="CRITICAL",
                    )
                ],
                summary=f"Failed to inspect remotes for {remote_name} (failing closed).",
            )

        if has_remotes_proc.stdout.strip():
            rev_list_cmd = ["git", "rev-list", local_sha, "--not", f"--remotes={remote_name}"]
        else:
            rev_list_cmd = ["git", "rev-list", local_sha]

        proc_revs = subprocess.run(
            rev_list_cmd,
            cwd=repo_dir,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc_revs.returncode != 0:
            return ScanResult(
                is_clean=False,
                findings=[
                    SecretFinding(
                        rule="Git Revision List Error",
                        file_path=local_ref,
                        line_number=None,
                        redacted_snippet=f"git rev-list failed: {proc_revs.stderr.strip()[:140]}",
                        severity="CRITICAL",
                    )
                ],
                summary=f"Failed to list outgoing commits for {local_ref} (failing closed).",
            )

        new_commits = [c.strip() for c in proc_revs.stdout.splitlines() if c.strip()]
        for commit in new_commits:
            # Check commit parents
            proc_p = subprocess.run(
                ["git", "rev-parse", f"{commit}^@"],
                cwd=repo_dir,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc_p.returncode != 0:
                return ScanResult(
                    is_clean=False,
                    findings=[
                        SecretFinding(
                            rule="Git Commit Inspection Error",
                            file_path=commit,
                            line_number=None,
                            redacted_snippet=f"git rev-parse failed for {commit}: {proc_p.stderr.strip()[:140]}",
                            severity="CRITICAL",
                        )
                    ],
                    summary=f"Failed to inspect commit {commit} (failing closed).",
                )

            parents = [p.strip() for p in proc_p.stdout.splitlines() if p.strip()]
            if not parents:
                c_range = f"4b825dc642cb6eb9a060e54bf8d69288fbee4904..{commit}"
            else:
                c_range = f"{commit}^!"

            c_scan = scan_commit_range(repo_dir, c_range)
            if not c_scan.is_clean:
                all_findings.extend(c_scan.findings)

    if all_findings:
        summary = f"{len(all_findings)} secret incident(s) detected in outgoing push: " + ", ".join(
            f"{f.rule} in {f.file_path}" for f in all_findings[:3]
        )
        if len(all_findings) > 3:
            summary += f" and {len(all_findings) - 3} more"
        return ScanResult(is_clean=False, findings=all_findings, summary=summary)

    return ScanResult(is_clean=True, findings=[], summary="Outgoing push is clean.")


def _safe_print(msg: str, file=None) -> None:
    """Print message safely across all platforms and console encodings (e.g. Windows cp1252)."""
    import sys
    target = file or sys.stdout
    try:
        target.write(msg + "\n")
        target.flush()
    except UnicodeEncodeError:
        safe_msg = msg.encode("ascii", errors="replace").decode("ascii")
        target.write(safe_msg + "\n")
        target.flush()


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint for git-guard pre-commit, pre-push, and revision range scanning."""
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Git Secret Guard — pre-commit and pre-push secret scanner")
    parser.add_argument("--staged", action="store_true", help="Scan staged changes (default mode)")
    parser.add_argument("--range", dest="rev_range", type=str, default=None, help="Scan a git revision range (e.g. origin/main..HEAD)")
    parser.add_argument("--file", dest="file_path", type=str, default=None, help="Scan a single file on disk")
    parser.add_argument("--pre-push", nargs=2, metavar=("REMOTE", "URL"), help="Scan outgoing push commits via pre-push stdin")
    parser.add_argument("--install-hook", action="store_true", help="Configure Git to use .githooks directory")

    args = parser.parse_args(argv)
    repo_dir = Path.cwd()

    if args.install_hook:
        hooks_dir = repo_dir / ".githooks"
        if not hooks_dir.exists():
            _safe_print("[GitGuard] Error: .githooks directory not found in current repository.", file=sys.stderr)
            return 1
        res = subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=repo_dir)
        if res.returncode == 0:
            _safe_print("[GitGuard] Successfully configured git core.hooksPath to .githooks")
            return 0
        _safe_print("[GitGuard] Error: Failed to set git config core.hooksPath.", file=sys.stderr)
        return 1

    if args.file_path:
        fpath = Path(args.file_path)
        if not fpath.exists():
            _safe_print(f"[GitGuard] Error: File not found: {args.file_path}", file=sys.stderr)
            return 1
        content = fpath.read_text(encoding="utf-8", errors="replace")
        findings = scan_text_content(str(fpath), content)
        if findings:
            _safe_print(f"[GitGuard] 🚨 Found {len(findings)} secret incident(s) in {args.file_path}:", file=sys.stderr)
            for f in findings:
                loc = f"{f.file_path}:{f.line_number}" if f.line_number else f.file_path
                _safe_print(f"  • [{f.severity}] {f.rule} at {loc}", file=sys.stderr)
                _safe_print(f"    Snippet: {f.redacted_snippet}", file=sys.stderr)
            return 1
        _safe_print(f"[GitGuard] ✅ File {args.file_path} is clean of secrets.")
        return 0

    if args.pre_push:
        remote_name, remote_url = args.pre_push
        scan = scan_pre_push(repo_dir, remote_name, remote_url)
    elif args.rev_range:
        scan = scan_commit_range(repo_dir, args.rev_range)
    else:
        # Default is --staged
        scan = scan_staged_changes(repo_dir)

    if not scan.is_clean:
        _safe_print(f"[GitGuard] 🚨 Secret leak detected: {scan.summary}", file=sys.stderr)
        for finding in scan.findings:
            loc = f"{finding.file_path}:{finding.line_number}" if finding.line_number else finding.file_path
            _safe_print(f"  • [{finding.severity}] {finding.rule} in {loc}", file=sys.stderr)
            _safe_print(f"    Snippet: {finding.redacted_snippet}", file=sys.stderr)
        return 1

    _safe_print(f"[GitGuard] ✅ {scan.summary}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

