"""Deterministic Target Model and Classification for BrainFrog.

Remediates M-02 (Heuristic Target Extraction) by establishing deterministic
predicates for workspace-relative filesystem targets:
- Rejects non-filesystem tokens (versions, numbers, URLs, issues, packages, identifiers).
- Rejects invalid / path-traversal tokens (.., absolute, UNC, drive letters).
- Rejects ambiguous bare tokens without guessing.
- Deterministically identifies valid workspace-relative files and directories.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


class TargetClassification(str, Enum):
    """Deterministic classification of a target candidate."""
    VALID_WORKSPACE_RELATIVE_FILE = "VALID_WORKSPACE_RELATIVE_FILE"
    VALID_WORKSPACE_RELATIVE_DIRECTORY = "VALID_WORKSPACE_RELATIVE_DIRECTORY"
    AMBIGUOUS = "AMBIGUOUS"
    NON_FILESYSTEM_TOKEN = "NON_FILESYSTEM_TOKEN"
    INVALID_PATH = "INVALID_PATH"


@dataclass(frozen=True)
class TargetCandidate:
    """Structured representation of an extracted target candidate."""
    raw: str
    normalized: str
    classification: TargetClassification
    reason: str

    @property
    def is_valid(self) -> bool:
        return self.classification in (
            TargetClassification.VALID_WORKSPACE_RELATIVE_FILE,
            TargetClassification.VALID_WORKSPACE_RELATIVE_DIRECTORY,
        )


KNOWN_NON_FS_IDENTIFIERS: Set[str] = {
    "JWT", "API", "SDK", "HTTP", "HTTPS", "OAUTH", "REST", "GRAPHQL", "SQL", "CLI",
    "GUI", "URL", "URI", "PR", "PRD", "IDE", "OS", "CPU", "RAM", "DOM", "CSS", "HTML",
    "JSON", "YAML", "XML", "TCP", "UDP", "IP", "DNS", "SSH", "SSL", "TLS",
}

KNOWN_PACKAGES: Set[str] = {
    "fastapi", "react-native", "requests", "pytest", "pydantic", "express", "lodash",
    "flask", "django", "uvicorn", "torch", "numpy", "pandas", "tailwindcss", "vite",
    "next", "react", "vue", "angular", "axios", "scipy", "scikit-learn", "celery",
    "redis", "postgres", "mysql", "mongodb", "sqlite",
}

KNOWN_EXTENSIONLESS_FILES: Set[str] = {
    "readme", "license", "makefile", "dockerfile", "procfile", "gemfile", "rakefile",
    "contributing", "changelog", "authors", "copying",
}

VALID_CODE_EXTENSIONS: Set[str] = {
    "py", "js", "ts", "tsx", "jsx", "json", "md", "txt", "yaml", "yml", "toml",
    "html", "css", "sql", "sh", "bat", "env", "lock", "cfg", "ini", "xml", "csv",
    "rst", "c", "cpp", "h", "hpp", "go", "rs", "java", "kt", "rb", "php", "proto",
    "graphql", "svg", "scss", "sass", "less", "gradle", "properties", "conf",
}

STOP_WORDS: Set[str] = {
    "modify", "edit", "update", "rewrite", "create", "write", "delete", "remove",
    "ubah", "hapus", "ganti", "file", "files", "the", "in", "to", "and", "or", "from",
    "for", "with", "please", "tolong", "dan", "di", "ke", "dari", "pada", "issue",
    "version", "ticket", "dependency", "upgrade", "bump", "downgrade", "change", "set",
    "fix", "use", "call", "is", "a", "an", "all", "project", "repo", "repository",
    "codebase", "timeout", "port", "retry", "count", "batch", "size", "handling",
    "config", "auth", "authentication", "backend", "frontend", "service", "routes",
    "schema", "model", "status", "build", "run", "execute", "jalankan", "eksekusi",
    "install", "pasang", "listen", "configure", "fetch", "handle", "close", "resolve",
    "address", "check", "periksa", "lihat", "buka", "tutup",
}

URL_SCHEMES = ("https://", "http://", "ftp://", "ftps://", "ws://", "wss://", "ssh://", "git://", "file://")

URL_DOMAIN_PATTERN = re.compile(
    r"^([a-zA-Z0-9\-]+\.)+(com|org|net|io|dev|ai|edu|gov|co|app|tech|me|info|biz|tv|cc|xyz|online)(:\d+)?(/.*)?$",
    re.IGNORECASE,
)

LOCAL_ENDPOINT_PATTERN = re.compile(
    r"^(localhost|\d{1,3}(\.\d{1,3}){3})(:\d+)?(/.*)?$",
    re.IGNORECASE,
)

EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]+@[a-zA-Z0-9_\-\.]+\.[a-zA-Z]{2,}$")
NPM_SCOPED_PACKAGE_PATTERN = re.compile(r"^@[a-zA-Z0-9_\-]+\/[a-zA-Z0-9_\-]+$")
SEMVER_PATTERN = re.compile(r"^v?\d+(\.\d+)+(-[a-zA-Z0-9_\-\.]+)?$", re.IGNORECASE)
VERSION_TAG_PATTERN = re.compile(r"^v\d+$", re.IGNORECASE)
PYTHON_VERSION_PATTERN = re.compile(r"^python\s*\d+(\.\d+)*$", re.IGNORECASE)
ISSUE_PATTERN = re.compile(r"^(#\d+|[a-zA-Z]{1,10}-\d+)$")
PURE_NUMERIC_PATTERN = re.compile(r"^\d+$")
DRIVE_LETTER_PATTERN = re.compile(r"^[a-zA-Z]:")
UNC_PATH_PATTERN = re.compile(r"^(\\\\|//)")


def classify_target_candidate(token: str, repo_dir: Optional[Path] = None) -> TargetCandidate:
    """Deterministically classify a single token candidate against filesystem rules."""
    clean = token.strip().strip("'\"`")
    if not clean:
        return TargetCandidate(
            raw=token,
            normalized="",
            classification=TargetClassification.AMBIGUOUS,
            reason="Empty token",
        )

    # 1. Illegal characters & Path traversal & Absolute paths (FAIL CLOSED -> INVALID_PATH)
    if any(c in clean for c in ('\0', '\r', '\n', '<', '>', '|', '?', '*')):
        return TargetCandidate(
            raw=token,
            normalized="",
            classification=TargetClassification.INVALID_PATH,
            reason="Illegal characters in path",
        )

    if DRIVE_LETTER_PATTERN.match(clean) or UNC_PATH_PATTERN.match(clean) or clean.startswith(("/", "\\")):
        return TargetCandidate(
            raw=token,
            normalized="",
            classification=TargetClassification.INVALID_PATH,
            reason="Absolute or UNC path prohibited",
        )

    posix_path = clean.replace("\\", "/")
    segments = posix_path.split("/")
    if any(seg in ("..", ".") for seg in segments):
        return TargetCandidate(
            raw=token,
            normalized="",
            classification=TargetClassification.INVALID_PATH,
            reason="Path traversal segment detected",
        )

    # 2. URLs, endpoints, emails (NON_FILESYSTEM_TOKEN)
    if clean.lower().startswith(URL_SCHEMES) or clean.lower().startswith("www."):
        return TargetCandidate(
            raw=token,
            normalized="",
            classification=TargetClassification.NON_FILESYSTEM_TOKEN,
            reason="URL or URI scheme",
        )

    if EMAIL_PATTERN.match(clean):
        return TargetCandidate(
            raw=token,
            normalized="",
            classification=TargetClassification.NON_FILESYSTEM_TOKEN,
            reason="Email address",
        )

    if LOCAL_ENDPOINT_PATTERN.match(clean) or URL_DOMAIN_PATTERN.match(clean):
        return TargetCandidate(
            raw=token,
            normalized="",
            classification=TargetClassification.NON_FILESYSTEM_TOKEN,
            reason="Web domain or localhost endpoint",
        )

    if NPM_SCOPED_PACKAGE_PATTERN.match(clean):
        return TargetCandidate(
            raw=token,
            normalized="",
            classification=TargetClassification.NON_FILESYSTEM_TOKEN,
            reason="Scoped package identifier",
        )

    # 3. Version numbers & Pure Numeric (NON_FILESYSTEM_TOKEN)
    if SEMVER_PATTERN.match(clean) or VERSION_TAG_PATTERN.match(clean) or PYTHON_VERSION_PATTERN.match(clean):
        return TargetCandidate(
            raw=token,
            normalized="",
            classification=TargetClassification.NON_FILESYSTEM_TOKEN,
            reason="Version number or semantic version",
        )

    if PURE_NUMERIC_PATTERN.match(clean):
        return TargetCandidate(
            raw=token,
            normalized="",
            classification=TargetClassification.NON_FILESYSTEM_TOKEN,
            reason="Numeric value or port number",
        )

    # 4. Issue references (NON_FILESYSTEM_TOKEN)
    if ISSUE_PATTERN.match(clean):
        return TargetCandidate(
            raw=token,
            normalized="",
            classification=TargetClassification.NON_FILESYSTEM_TOKEN,
            reason="Issue or ticket reference",
        )

    # 5. Technical identifiers & package names (without file extension)
    clean_lower = clean.lower()
    if "/" not in posix_path and "\\" not in clean:
        if clean.upper() in KNOWN_NON_FS_IDENTIFIERS:
            return TargetCandidate(
                raw=token,
                normalized="",
                classification=TargetClassification.NON_FILESYSTEM_TOKEN,
                reason="Technical acronym or identifier",
            )

        if clean_lower in KNOWN_PACKAGES:
            return TargetCandidate(
                raw=token,
                normalized="",
                classification=TargetClassification.NON_FILESYSTEM_TOKEN,
                reason="Package or dependency name",
            )

        # 6. Check single-segment file
        if clean_lower in KNOWN_EXTENSIONLESS_FILES:
            return TargetCandidate(
                raw=token,
                normalized=clean,
                classification=TargetClassification.VALID_WORKSPACE_RELATIVE_FILE,
                reason="Known extensionless file (e.g. README, LICENSE)",
            )

        # Has dot? Check extension
        if "." in clean:
            parts = clean.rsplit(".", 1)
            ext = parts[1].lower()
            name_part = parts[0]
            if ext.isdigit():
                return TargetCandidate(
                    raw=token,
                    normalized="",
                    classification=TargetClassification.NON_FILESYSTEM_TOKEN,
                    reason="Numeric version extension",
                )
            if ext in VALID_CODE_EXTENSIONS and name_part and re.match(r"^[a-zA-Z0-9_\-\.]+$", name_part):
                return TargetCandidate(
                    raw=token,
                    normalized=clean,
                    classification=TargetClassification.VALID_WORKSPACE_RELATIVE_FILE,
                    reason="Valid single-segment file with recognized extension",
                )
            return TargetCandidate(
                raw=token,
                normalized="",
                classification=TargetClassification.AMBIGUOUS,
                reason="Unrecognized file extension",
            )

        # Bare word without slash and without extension -> AMBIGUOUS
        return TargetCandidate(
            raw=token,
            normalized="",
            classification=TargetClassification.AMBIGUOUS,
            reason="Ambiguous bare word without path or file extension",
        )

    # 7. Multi-segment path
    non_empty_segs = [s for s in segments if s]
    if not non_empty_segs:
        return TargetCandidate(
            raw=token,
            normalized="",
            classification=TargetClassification.AMBIGUOUS,
            reason="Empty path",
        )

    for s in non_empty_segs:
        if not re.match(r"^[a-zA-Z0-9_\-\.]+$", s):
            return TargetCandidate(
                raw=token,
                normalized="",
                classification=TargetClassification.INVALID_PATH,
                reason=f"Invalid segment characters: {s}",
            )

    normalized = "/".join(non_empty_segs)
    if clean.endswith("/"):
        return TargetCandidate(
            raw=token,
            normalized=normalized + "/",
            classification=TargetClassification.VALID_WORKSPACE_RELATIVE_DIRECTORY,
            reason="Valid workspace-relative directory",
        )

    if repo_dir:
        try:
            cand = (repo_dir / normalized).resolve()
            if not cand.is_relative_to(repo_dir.resolve()):
                return TargetCandidate(
                    raw=token,
                    normalized="",
                    classification=TargetClassification.INVALID_PATH,
                    reason="Path escapes workspace root",
                )
        except Exception:
            return TargetCandidate(
                raw=token,
                normalized="",
                classification=TargetClassification.INVALID_PATH,
                reason="Path resolution failed",
            )

    return TargetCandidate(
        raw=token,
        normalized=normalized,
        classification=TargetClassification.VALID_WORKSPACE_RELATIVE_FILE,
        reason="Valid workspace-relative multi-segment path",
    )


def extract_deterministic_targets(
    text: str,
    repo_dir: Optional[Path] = None,
) -> Tuple[List[str], List[TargetCandidate]]:
    """Deterministically extract valid filesystem targets from natural language text.

    Returns:
        (valid_targets: List[str], all_candidates: List[TargetCandidate])
    """
    raw_words = text.strip().split()
    candidates: List[TargetCandidate] = []
    seen_raw = set()

    for word in raw_words:
        cleaned = word.strip("()[]{}<>`'\",;:*?!")
        if cleaned.endswith(".") and not cleaned.endswith("..") and cleaned.count(".") == 1 and not re.match(r"^\d+\.\d+$", cleaned):
            cleaned = cleaned.rstrip(".")

        if not cleaned or cleaned in seen_raw:
            continue
        seen_raw.add(cleaned)

        # Skip common stop words if they are single words without slash/dot
        if cleaned.lower() in STOP_WORDS and "/" not in cleaned and "\\" not in cleaned and "." not in cleaned:
            continue

        cand = classify_target_candidate(cleaned, repo_dir=repo_dir)
        candidates.append(cand)

    valid_targets: List[str] = []
    seen_targets = set()
    for c in candidates:
        if c.is_valid and c.normalized not in seen_targets:
            seen_targets.add(c.normalized)
            valid_targets.append(c.normalized)

    return valid_targets, candidates
