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
    if proc_files.returncode == 0 and proc_files.stdout:
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
    if proc_diff.returncode == 0 and proc_diff.stdout and proc_diff.stdout.strip():
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
    patterns_to_check = [".next", ".brainfrog/scratch", ".brainfrog/sessions", "node_modules"]
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

