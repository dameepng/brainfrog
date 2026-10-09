"""Deterministic Task Classification Domain Model and Rule Engine.

BrainFrog P1.4D — Deterministic Classification.

Architectural Principles:
- CLASSIFICATION ≠ AUTHORIZATION
- CLASSIFICATION ≠ CAPABILITY
- CLASSIFICATION ≠ ROUTING
- PROFILE ≠ CAPABILITY
- ROUTING ≠ AUTHORIZATION
- RESOLUTION ≠ EXECUTION
- orchestrator.py remains the SOLE execution engine.

A TaskClassification is purely descriptive domain metadata:
- It describes WHAT KIND OF WORK a Task represents (task_type, domain, skills, confidence, evidence).
- It NEVER answers WHICH agent should execute it (that is P1.4E).
- It NEVER grants, derives, attenuates, or implies execution authority.
- It contains ZERO permissions, capabilities, execution contracts, or approval state.
- Pure domain layer: zero subprocesses, zero shell, zero network, zero LLM, zero side effects.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple, Union

from core.runtime.task import (
    MAX_DOMAIN_CHARS,
    MAX_REQUESTED_SKILLS_COUNT,
    MAX_SKILL_CHARS,
    MAX_TASK_ID_CHARS,
    MAX_TASK_TYPE_CHARS,
    Task,
    validate_task_id,
)

CURRENT_TASK_CLASSIFICATION_SCHEMA_VERSION = 1

# Explicit Resource Bounds
MAX_SKILLS_COUNT = MAX_REQUESTED_SKILLS_COUNT
MAX_EVIDENCE_COUNT = 64
MAX_EVIDENCE_CHARS = 128
MAX_TASK_CLASSIFICATION_SERIALIZED_BYTES = 16 * 1024  # 16 KB

# Identifier & Traversal Patterns
_INVALID_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\*\?\"'<>\|;:~]|(?:\.\.)")
_DRIVE_LETTER_PATTERN = re.compile(r"^[a-zA-Z]:")

# Credential and Secret Detection Patterns
_SECRET_KEY_NAMES: FrozenSet[str] = frozenset({
    "password",
    "passwd",
    "secret",
    "credential",
    "credentials",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "auth_token",
    "refresh_token",
    "private_key",
    "secret_key",
    "client_secret",
    "app_secret",
    "signing_secret",
    "jwt_secret",
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
})
_FORBIDDEN_SECRET_NAMES = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
})
_SECRET_VALUE_ASSIGN_PATTERN = re.compile(
    r"(?i)(?:password|api[_-]?key|access[_-]?token|secret|bearer)\s*[:=]\s*[^\s]+",
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY",
    re.IGNORECASE,
)
_TOKEN_PATTERNS = [
    re.compile(r"\b(?:sk-[a-zA-Z0-9_\-]{20,})\b"),  # OpenAI-style key
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,})\b"),  # GitHub token
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
    re.compile(r"(?i)\bbearer\s+[a-zA-Z0-9_\-\.]{12,}\b"),  # Bearer token
]

# Forbidden Authority-Shaped Keys and Tokens in Classification
FORBIDDEN_AUTHORITY_KEYS: FrozenSet[str] = frozenset({
    "capability",
    "capabilities",
    "permission",
    "permissions",
    "authority",
    "authorities",
    "allow_shell",
    "allow_network",
    "allow_filesystem",
    "allow_filesystem_read",
    "allow_filesystem_write",
    "allow_git",
    "allow_git_read",
    "allow_git_commit",
    "allow_git_push",
    "allowed_tools",
    "allowed_commands",
    "allowed_files",
    "can_approve",
    "can_execute",
    "can_mutate",
    "approval_status",
    "approved",
    "authorized",
    "approved_execution_contract",
    "execution_contract",
    "delegation_contract",
    "actor_credentials",
    "session_credentials",
    "auth_tokens",
    "secrets",
    "executor",
    "orchestrator",
    "orchestrator_authority",
    "transaction_id",
    "transaction_authority",
    "session_authority",
    "shell_access",
    "network_access",
    "actor",
    "session",
    "session_id",
    "incarnation",
})


# =============================================================================
# Exception Hierarchy
# =============================================================================


class TaskClassificationError(ValueError):
    """Base exception for all task classification domain errors."""
    pass


class TaskClassificationValidationError(TaskClassificationError):
    """Raised when classification validation, constraints, or bounds fail."""
    pass


class TaskClassificationAuthorityViolationError(TaskClassificationError, PermissionError):
    """Raised when authority-like data is passed to or detected in a classification."""
    pass


class TaskClassificationIntegrityError(TaskClassificationError):
    """Raised when classification digest verification or tampering detection fails."""
    pass


class TaskClassificationSecretExposureError(TaskClassificationError, PermissionError):
    """Raised when sensitive credentials or secrets are detected in a classification."""
    pass


# =============================================================================
# Validation and Security Sanitization Helpers
# =============================================================================


def _reject_string_secrets(val: str, context: str) -> None:
    """Scan string value for high-risk token signatures or secret assignments."""
    if _PRIVATE_KEY_PATTERN.search(val):
        raise TaskClassificationSecretExposureError(
            f"Private key header pattern detected in TaskClassification {context}"
        )
    for pat in _TOKEN_PATTERNS:
        if pat.search(val):
            raise TaskClassificationSecretExposureError(
                f"High-entropy token pattern detected in TaskClassification {context}"
            )
    if _SECRET_VALUE_ASSIGN_PATTERN.search(val):
        raise TaskClassificationSecretExposureError(
            f"Secret assignment pattern detected in TaskClassification {context}"
        )


def _check_authority_token(token: str, context: str) -> None:
    """Reject tokens that resemble execution capabilities, permissions, or authority."""
    clean_k = token.strip().lower()
    if clean_k in FORBIDDEN_AUTHORITY_KEYS:
        raise TaskClassificationAuthorityViolationError(
            f"Authority-like field '{token}' is forbidden in TaskClassification {context}"
        )
    for forbidden in ("allow_", "can_", "grant_"):
        if clean_k.startswith(forbidden):
            for suffix in ("shell", "network", "exec", "write", "read", "push", "commit", "approve", "delete", "tool", "command"):
                if suffix in clean_k:
                    raise TaskClassificationAuthorityViolationError(
                        f"Authority-like permission '{token}' is forbidden in TaskClassification {context}"
                    )
    for forbidden_word in ("capability", "capabilities", "permission", "permissions", "execution_contract", "delegation_contract"):
        if forbidden_word in clean_k:
            raise TaskClassificationAuthorityViolationError(
                f"Authority-like concept '{token}' is forbidden in TaskClassification {context}"
            )


# =============================================================================
# Confidence Enum
# =============================================================================


class ClassificationConfidence(str, Enum):
    """Discrete deterministic confidence in task classification.

    CRITICAL INVARIANT:
    Classification confidence describes taxonomic pattern match certainty.
    It NEVER conveys, implies, or grants authorization or permission confidence.
    """
    EXPLICIT = "explicit"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


def parse_confidence(val: Union[ClassificationConfidence, str]) -> ClassificationConfidence:
    """Parse and normalize ClassificationConfidence."""
    if isinstance(val, ClassificationConfidence):
        return val
    if type(val) is not str:
        raise TaskClassificationValidationError(
            f"confidence must be a string or ClassificationConfidence, got {type(val)}"
        )
    clean = val.strip().lower()
    try:
        return ClassificationConfidence(clean)
    except ValueError:
        raise TaskClassificationValidationError(
            f"Unknown confidence '{val}'. Supported: {[c.value for c in ClassificationConfidence]}"
        )


# =============================================================================
# Domain Model: TaskClassification
# =============================================================================


@dataclass(frozen=True, slots=True)
class TaskClassification:
    """Canonical domain representation of deterministic task classification.

    Architectural Invariant:
    CLASSIFICATION ≠ AUTHORIZATION.
    A TaskClassification describes WHAT KIND OF WORK a task represents.
    It NEVER grants, implies, or derives runtime execution authority, filesystem access,
    shell execution, network access, approval status, transaction boundaries, or capability contracts.
    It does NOT answer WHICH agent profile should execute the work (P1.4E routing).
    """

    task_id: str
    task_type: Optional[str] = None
    domain: Optional[str] = None
    skills: Tuple[str, ...] = ()
    confidence: ClassificationConfidence = ClassificationConfidence.UNKNOWN
    classifier_version: int = CURRENT_TASK_CLASSIFICATION_SCHEMA_VERSION
    evidence: Tuple[str, ...] = ()
    is_inferred: bool = False
    digest: str = ""

    def __post_init__(self) -> None:
        # 1. task_id
        clean_id = validate_task_id(self.task_id)
        object.__setattr__(self, "task_id", clean_id)

        # 2. classifier_version
        if type(self.classifier_version) is not int or self.classifier_version < 1:
            raise TaskClassificationValidationError("classifier_version must be a positive integer")

        # 3. confidence
        clean_conf = parse_confidence(self.confidence)
        object.__setattr__(self, "confidence", clean_conf)

        # 4. is_inferred
        if type(self.is_inferred) is not bool:
            raise TaskClassificationValidationError(
                f"is_inferred must be a boolean, got {type(self.is_inferred)}"
            )

        # 5. task_type
        if self.task_type is not None:
            if hasattr(self.task_type, "value"):
                tt_raw = str(getattr(self.task_type, "value"))
            elif type(self.task_type) is str:
                tt_raw = self.task_type
            else:
                raise TaskClassificationValidationError(
                    f"task_type must be a string or enum, got {type(self.task_type)}"
                )
            clean_tt = tt_raw.strip().lower()
            if not clean_tt:
                clean_tt = None
            else:
                _reject_string_secrets(clean_tt, "task_type")
                _check_authority_token(clean_tt, "task_type")
                if len(clean_tt) > MAX_TASK_TYPE_CHARS:
                    raise TaskClassificationValidationError(
                        f"task_type length ({len(clean_tt)}) exceeds limit ({MAX_TASK_TYPE_CHARS})"
                    )
                if re.search(r"[\x00-\x1f\x7f]", clean_tt):
                    raise TaskClassificationValidationError("Control characters forbidden in task_type")
            object.__setattr__(self, "task_type", clean_tt)

        # 6. domain
        if self.domain is not None:
            if type(self.domain) is not str:
                raise TaskClassificationValidationError(
                    f"domain must be a string, got {type(self.domain)}"
                )
            clean_dom = self.domain.strip().lower()
            if not clean_dom:
                clean_dom = None
            else:
                _reject_string_secrets(clean_dom, "domain")
                _check_authority_token(clean_dom, "domain")
                if len(clean_dom) > MAX_DOMAIN_CHARS:
                    raise TaskClassificationValidationError(
                        f"domain length ({len(clean_dom)}) exceeds limit ({MAX_DOMAIN_CHARS})"
                    )
                if re.search(r"[\x00-\x1f\x7f]", clean_dom):
                    raise TaskClassificationValidationError("Control characters forbidden in domain")
            object.__setattr__(self, "domain", clean_dom)

        # 7. skills
        if not isinstance(self.skills, (list, tuple, set, frozenset)):
            raise TaskClassificationValidationError(
                f"skills must be a sequence or set of strings, got {type(self.skills)}"
            )
        if len(self.skills) > MAX_SKILLS_COUNT:
            raise TaskClassificationValidationError(
                f"skills count ({len(self.skills)}) exceeds limit ({MAX_SKILLS_COUNT})"
            )
        clean_skills: Set[str] = set()
        for sk in self.skills:
            if type(sk) is not str:
                raise TaskClassificationValidationError(
                    f"skills items must be strings, got {type(sk)}"
                )
            csk = sk.strip().lower()
            if not csk:
                raise TaskClassificationValidationError("Skill item cannot be empty")
            _reject_string_secrets(csk, "skills[item]")
            _check_authority_token(csk, "skills[item]")
            if len(csk) > MAX_SKILL_CHARS:
                raise TaskClassificationValidationError(
                    f"Skill item length ({len(csk)}) exceeds limit ({MAX_SKILL_CHARS})"
                )
            if re.search(r"[\x00-\x1f\x7f]", csk):
                raise TaskClassificationValidationError("Control characters forbidden in skill")
            clean_skills.add(csk)
        object.__setattr__(self, "skills", tuple(sorted(clean_skills)))

        # 8. evidence
        if not isinstance(self.evidence, (list, tuple, set, frozenset)):
            raise TaskClassificationValidationError(
                f"evidence must be a sequence or set of strings, got {type(self.evidence)}"
            )
        if len(self.evidence) > MAX_EVIDENCE_COUNT:
            raise TaskClassificationValidationError(
                f"evidence count ({len(self.evidence)}) exceeds limit ({MAX_EVIDENCE_COUNT})"
            )
        clean_ev: Set[str] = set()
        for ev in self.evidence:
            if type(ev) is not str:
                raise TaskClassificationValidationError(
                    f"evidence items must be strings, got {type(ev)}"
                )
            cev = ev.strip()
            if not cev:
                raise TaskClassificationValidationError("evidence item cannot be empty")
            _reject_string_secrets(cev, "evidence[item]")
            _check_authority_token(cev, "evidence[item]")
            if len(cev) > MAX_EVIDENCE_CHARS:
                raise TaskClassificationValidationError(
                    f"evidence item length ({len(cev)}) exceeds limit ({MAX_EVIDENCE_CHARS})"
                )
            if re.search(r"[\x00-\x1f\x7f]", cev):
                raise TaskClassificationValidationError("Control characters forbidden in evidence")
            clean_ev.add(cev)
        object.__setattr__(self, "evidence", tuple(sorted(clean_ev)))

        # 9. Total serialized size check
        serialized_size = len(self._compute_canonical_json().encode("utf-8"))
        if serialized_size > MAX_TASK_CLASSIFICATION_SERIALIZED_BYTES:
            raise TaskClassificationValidationError(
                f"Serialized classification size ({serialized_size} bytes) exceeds limit ({MAX_TASK_CLASSIFICATION_SERIALIZED_BYTES} bytes)"
            )

        # 10. Digest computation & verification
        expected_digest = self.compute_digest()
        if not self.digest:
            object.__setattr__(self, "digest", expected_digest)
        elif self.digest != expected_digest:
            raise TaskClassificationIntegrityError(
                f"TaskClassification digest mismatch: expected '{expected_digest}', got '{self.digest}'"
            )

    def compute_digest(self) -> str:
        """Compute deterministic SHA-256 digest of semantic classification fields."""
        canonical_json = self._compute_canonical_json()
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def _compute_canonical_json(self) -> str:
        """Produce canonical JSON representation of semantic fields with sorted keys."""
        semantic_payload = {
            "classifier_version": self.classifier_version,
            "confidence": self.confidence.value,
            "domain": self.domain,
            "evidence": list(self.evidence),
            "is_inferred": self.is_inferred,
            "skills": list(self.skills),
            "task_id": self.task_id,
            "task_type": self.task_type,
        }
        return json.dumps(semantic_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def to_dict(self) -> Dict[str, Any]:
        """Convert TaskClassification to a JSON-compatible dictionary."""
        return {
            "classifier_version": self.classifier_version,
            "task_id": self.task_id,
            "task_type": self.task_type,
            "domain": self.domain,
            "skills": list(self.skills),
            "confidence": self.confidence.value,
            "evidence": list(self.evidence),
            "is_inferred": self.is_inferred,
            "digest": self.digest,
        }

    def to_json(self) -> str:
        """Serialize TaskClassification to a JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, ensure_ascii=True)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TaskClassification:
        """Reconstruct a TaskClassification from a dictionary, enforcing strict validation."""
        if type(data) is not dict:
            raise TaskClassificationValidationError(
                f"Expected dictionary for TaskClassification.from_dict, got {type(data)}"
            )

        for k in data.keys():
            _check_authority_token(k, "root")

        known_fields = {
            "classifier_version",
            "task_id",
            "task_type",
            "domain",
            "skills",
            "confidence",
            "evidence",
            "is_inferred",
            "digest",
        }
        unknown = set(data.keys()) - known_fields
        if unknown:
            raise TaskClassificationValidationError(
                f"Unrecognized fields in TaskClassification payload: {sorted(unknown)}"
            )

        task_id = data.get("task_id")
        if task_id is None:
            raise TaskClassificationValidationError("Missing required 'task_id' in from_dict payload")

        confidence_raw = data.get("confidence", ClassificationConfidence.UNKNOWN.value)
        confidence = parse_confidence(confidence_raw)

        skills_raw = data.get("skills", ())
        if not isinstance(skills_raw, (list, tuple, set, frozenset)):
            raise TaskClassificationValidationError("skills must be a sequence or set")

        evidence_raw = data.get("evidence", ())
        if not isinstance(evidence_raw, (list, tuple, set, frozenset)):
            raise TaskClassificationValidationError("evidence must be a sequence or set")

        return cls(
            task_id=str(task_id),
            task_type=data.get("task_type"),
            domain=data.get("domain"),
            skills=tuple(str(s) for s in skills_raw),
            confidence=confidence,
            classifier_version=data.get("classifier_version", CURRENT_TASK_CLASSIFICATION_SCHEMA_VERSION),
            evidence=tuple(str(e) for e in evidence_raw),
            is_inferred=bool(data.get("is_inferred", False)),
            digest=str(data.get("digest", "")),
        )


# =============================================================================
# Deterministic Rule Engine
# =============================================================================


# Task Type Classification Rules in strict deterministic priority order
_TASK_TYPE_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("testing", (
        "unit test", "integration test", "smoke test", "regression test", "e2e test",
        "pytest", "unittest", "test suite", "tests", "test", "coverage", "mock", "assert",
    )),
    ("debugging", (
        "traceback", "stack trace", "segfault", "panic", "crash",
        "bug", "fix", "error", "exception", "fault", "remediate", "remediation", "patch", "hotfix", "issue",
    )),
    ("verification", (
        "verify", "verification", "validation", "validate", "compliance", "check invariants",
    )),
    ("review", (
        "code review", "security review", "audit", "critique", "inspect", "inspection", "lint", "linter",
    )),
    ("refactor", (
        "refactor", "refactoring", "clean up", "cleanup", "restructure", "reorganize", "decouple", "modernize", "modularize",
    )),
    ("documentation", (
        "documentation", "document", "readme", "docstring", "markdown", "user guide", "manual", "spec", "specification",
    )),
    ("research", (
        "research", "investigate", "investigation", "survey", "benchmark", "feasibility", "explore",
    )),
    ("analysis", (
        "analysis", "analyze", "profile", "profiling", "diagnostic", "diagnostics", "metrics", "metric",
    )),
    ("implementation", (
        "implement", "implementation", "create", "build", "develop", "add feature", "write code", "scaffold", "new feature",
    )),
)

# Domain Classification Rules in strict deterministic priority order
_DOMAIN_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("security", (
        "vulnerability", "cve", "owasp", "sanitization", "jwt", "oauth", "auth",
        "encryption", "cipher", "crypto", "security", "authenticator",
    )),
    ("database", (
        "postgresql", "postgres", "sqlite", "mysql", "nosql", "redis", "mongodb",
        "sql", "database", "migration", "schema", "query", "orm",
    )),
    ("frontend", (
        "react", "vue", "angular", "svelte", "tailwind", "css", "html",
        "frontend", "ui", "ux", "browser", "component", "web page", "dom",
    )),
    ("devops", (
        "kubernetes", "k8s", "docker", "dockerfile", "container", "ci/cd", "pipeline",
        "devops", "deploy", "deployment", "github action", "workflow",
    )),
    ("backend", (
        "fastapi", "flask", "django", "express", "graphql", "rest api", "endpoint",
        "backend", "server", "microservice", "route", "controller",
    )),
    ("qa", (
        "quality assurance", "qa", "test harness", "test runner", "assertion",
    )),
    ("system", (
        "operating system", "concurrency", "threading", "process", "socket", "ipc", "memory", "kernel",
    )),
)

# Known Programming Languages & Tooling Skills to detect
_KNOWN_SKILL_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("python", re.compile(r"\bpython\b", re.IGNORECASE)),
    ("typescript", re.compile(r"\btypescript\b|\bts\b", re.IGNORECASE)),
    ("javascript", re.compile(r"\bjavascript\b|\bjs\b", re.IGNORECASE)),
    ("pytest", re.compile(r"\bpytest\b", re.IGNORECASE)),
    ("fastapi", re.compile(r"\bfastapi\b", re.IGNORECASE)),
    ("react", re.compile(r"\breact\b", re.IGNORECASE)),
    ("docker", re.compile(r"\bdocker\b", re.IGNORECASE)),
    ("git", re.compile(r"\bgit\b", re.IGNORECASE)),
    ("sql", re.compile(r"\bsql\b", re.IGNORECASE)),
    ("html", re.compile(r"\bhtml\b", re.IGNORECASE)),
    ("css", re.compile(r"\bcss\b", re.IGNORECASE)),
    ("json", re.compile(r"\bjson\b", re.IGNORECASE)),
    ("markdown", re.compile(r"\bmarkdown\b", re.IGNORECASE)),
)


def _compile_rule_patterns(
    rules: Sequence[Tuple[str, Sequence[str]]]
) -> Tuple[Tuple[str, Tuple[re.Pattern, ...]], ...]:
    compiled: List[Tuple[str, Tuple[re.Pattern, ...]]] = []
    for category, keywords in rules:
        pats = tuple(re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE) for kw in keywords)
        compiled.append((category, pats))
    return tuple(compiled)


_COMPILED_TASK_TYPE_RULES = _compile_rule_patterns(_TASK_TYPE_RULES)
_COMPILED_DOMAIN_RULES = _compile_rule_patterns(_DOMAIN_RULES)


class DeterministicTaskClassifier:
    """Pure, deterministic, stateless task classifier.

    Architectural Principles:
    - Pure domain layer: zero execution dependencies, zero subprocesses, zero network, zero LLM.
    - Deterministic: same Task + same classifier_version -> identical TaskClassification.
    - CLASSIFICATION ≠ AUTHORIZATION: Output describes taxonomy, never permissions.
    - Respects explicit Task declarations (task_type, domain, requested_skills).
    - If evidence is insufficient, falls back to UNKNOWN / None rather than guessing.
    """

    def __init__(self, classifier_version: int = CURRENT_TASK_CLASSIFICATION_SCHEMA_VERSION) -> None:
        if type(classifier_version) is not int or classifier_version < 1:
            raise TaskClassificationValidationError("classifier_version must be a positive integer")
        self._classifier_version = classifier_version

    @property
    def classifier_version(self) -> int:
        return self._classifier_version

    def classify(self, task: Task) -> TaskClassification:
        """Deterministically classify a Task into an immutable TaskClassification.

        Pure function of Task state and classifier version.
        """
        if not isinstance(task, Task):
            raise TaskClassificationValidationError(f"Expected Task instance, got {type(task)}")

        evidence: List[str] = []
        is_inferred = False

        # ---------------------------------------------------------------------
        # 1. Resolve task_type (explicit vs inferred)
        # ---------------------------------------------------------------------
        task_type: Optional[str] = None
        task_type_explicit = False
        if task.task_type:
            if isinstance(task.task_type, Enum):
                raw_tt = str(task.task_type.value)
            elif isinstance(task.task_type, str):
                raw_tt = task.task_type
            else:
                raw_tt = str(task.task_type)
            task_type = raw_tt.strip().lower()
            task_type_explicit = True
            evidence.append(f"explicit:task_type:{task_type}")

        # ---------------------------------------------------------------------
        # 2. Resolve domain (explicit vs inferred)
        # ---------------------------------------------------------------------
        domain: Optional[str] = None
        domain_explicit = False
        if task.domain:
            domain = task.domain.strip().lower()
            domain_explicit = True
            evidence.append(f"explicit:domain:{domain}")

        # ---------------------------------------------------------------------
        # 3. Resolve skills (explicit requested_skills)
        # ---------------------------------------------------------------------
        skills_set: Set[str] = set()
        for sk in task.requested_skills:
            clean_sk = sk.strip().lower()
            skills_set.add(clean_sk)
            evidence.append(f"explicit:skill:{clean_sk}")

        # ---------------------------------------------------------------------
        # 4. Deterministic Text Inspection (objective + description)
        # ---------------------------------------------------------------------
        text_corpus = f"{task.objective} {task.description}".lower()

        # If task_type is missing, infer deterministically
        task_type_matches = 0
        if not task_type:
            best_type: Optional[str] = None
            max_score = 0
            for tt_name, patterns in _COMPILED_TASK_TYPE_RULES:
                score = sum(1 for p in patterns if p.search(text_corpus))
                if score > max_score:
                    max_score = score
                    best_type = tt_name
            if best_type and max_score > 0:
                task_type = best_type
                task_type_matches = max_score
                is_inferred = True
                evidence.append(f"inferred:task_type:{task_type}")
            else:
                evidence.append("inferred:task_type:unknown")

        # If domain is missing, infer deterministically
        domain_matches = 0
        if not domain:
            best_dom: Optional[str] = None
            max_dom_score = 0
            for dom_name, patterns in _COMPILED_DOMAIN_RULES:
                score = sum(1 for p in patterns if p.search(text_corpus))
                if score > max_dom_score:
                    max_dom_score = score
                    best_dom = dom_name
            if best_dom and max_dom_score > 0:
                domain = best_dom
                domain_matches = max_dom_score
                is_inferred = True
                evidence.append(f"inferred:domain:{domain}")
            else:
                evidence.append("inferred:domain:unknown")

        # Infer additional skills from text corpus
        for skill_name, pattern in _KNOWN_SKILL_PATTERNS:
            if pattern.search(text_corpus):
                if skill_name not in skills_set:
                    skills_set.add(skill_name)
                    is_inferred = True
                    evidence.append(f"inferred:skill:{skill_name}")

        # ---------------------------------------------------------------------
        # 5. Calculate Confidence
        # ---------------------------------------------------------------------
        if task_type_explicit and domain_explicit:
            confidence = ClassificationConfidence.EXPLICIT
        elif task_type_explicit:
            if domain is not None or len(skills_set) > 0:
                confidence = ClassificationConfidence.HIGH
            else:
                confidence = ClassificationConfidence.MEDIUM
        elif domain_explicit:
            if task_type is not None:
                confidence = ClassificationConfidence.HIGH
            else:
                confidence = ClassificationConfidence.MEDIUM
        else:
            # Fully inferred
            if task_type is not None and domain is not None and task_type_matches >= 2:
                confidence = ClassificationConfidence.HIGH
            elif task_type is not None:
                confidence = ClassificationConfidence.MEDIUM
            elif domain is not None or len(skills_set) > 0:
                confidence = ClassificationConfidence.LOW
            else:
                confidence = ClassificationConfidence.UNKNOWN

        return TaskClassification(
            task_id=task.task_id,
            task_type=task_type,
            domain=domain,
            skills=tuple(sorted(skills_set)),
            confidence=confidence,
            classifier_version=self._classifier_version,
            evidence=tuple(sorted(evidence)),
            is_inferred=is_inferred,
        )


def classify_task(
    task: Task,
    *,
    classifier_version: int = CURRENT_TASK_CLASSIFICATION_SCHEMA_VERSION,
) -> TaskClassification:
    """Convenience functional wrapper around DeterministicTaskClassifier."""
    classifier = DeterministicTaskClassifier(classifier_version=classifier_version)
    return classifier.classify(task)
