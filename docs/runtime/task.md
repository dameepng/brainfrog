# BrainFrog Task Domain Model (Phase P1.4C)

## 1. Executive Summary & North Star

> **“BrainFrog — an agent runtime that turns intent into verified action.”**

Phase **P1.4C** introduces the canonical **Task Domain Model** (`core/runtime/task.py`).

A `Task` is the formal, descriptive domain representation of **“work that needs to be done”**. It provides an immutable, structured specification of user intent, requested outcomes, classification taxonomy, requested domain, skills, input parameters, and lineage.

Crucially, **P1.4C is a pure domain-model phase**. It does not perform task classification, routing, profile selection, delegation, scheduling, execution, approval, capability derivation, or authorization.

---

## 2. Core Architectural & Security Invariants

The fundamental architectural principle governing Phase P1.4C is strict separation of intent description from authorization:

```text
TASK ≠ AUTHORITY
TASK ≠ CAPABILITY
TASK ≠ APPROVAL
TASK ≠ EXECUTION CONTRACT
TASK ≠ DELEGATION CONTRACT
TASK ≠ AGENT PROFILE
CLASSIFICATION ≠ AUTHORIZATION
ROUTING ≠ AUTHORIZATION
```

### Invariant Rules
1. **Descriptive, Never Authoritative**: A `Task` may describe *what work is requested*. It MUST NOT grant, imply, encode, or derive permission to perform that work.
2. **Zero Capability / Execution Fields**: A `Task` has NO fields, methods, or properties for `capabilities`, `permissions`, `allowed_tools`, `allowed_commands`, `allowed_files`, `network_access`, `approval_status`, `approved`, `authorized`, `execution_contract`, `delegation_contract`, `actor_credentials`, `session_credentials`, `auth_tokens`, `secrets`, `executor`, `orchestrator`, or `shell_access`.
3. **Authority Smuggling Resistance**: Context and metadata dictionaries reject all authority-shaped keys (e.g. `capabilities`, `allow_shell`, `orchestrator`, `can_approve`) fail-closed with `TaskAuthorityViolationError`.
4. **Canonical Execution Boundary**: All execution authority remains governed exclusively by the canonical P1.3 chain:
   ```text
   Parent Capability -> DelegationContract -> ApprovedExecutionContract -> Transaction -> orchestrator.py
   ```
   `orchestrator.py` remains the SOLE execution engine.

---

## 3. Semantic Distinction from Existing Domain Models

BrainFrog maintains distinct domain abstractions for distinct lifecycle phases:

| Abstraction | Location | Semantics | Authority / Execution Role |
| :--- | :--- | :--- | :--- |
| **`Task`** | `core/runtime/task.py` | Canonical declarative unit of **requested work** (objective, taxonomy, domain, skills, inputs). | **Zero authority.** Purely descriptive intent. |
| **`Work`** | `core/runtime/work.py` | Stateful runtime **execution lifecycle** tracker (transitions: `CREATED` → `PLANNING` → `APPROVAL_REQUIRED` → `EXECUTING` → `VERIFYING` → `DONE`/`FAILED`). | Binds to transaction, approval, and channel incarnation. |
| **`PlanStep`** | `core/runtime/planning.py` | Ordered step in a generative **execution plan DAG** (dependencies, risk class, expected outcome). | Descriptive planning artifact; no execution capabilities. |
| **`TaskRoutingRequest`** | `core/runtime/subagent_routing.py` | Routing query binding **required capabilities** and target scopes to check profile compatibility. | Evaluation query for P1.3 subagent routing. |
| **`AgentProfile`** | `core/runtime/agent_profile.py` | Specialization profile describing **what work an agent is good at** (domains, skills, constraints). | **Zero authority.** Descriptive specialization metadata. |
| **`ApprovedExecutionContract`** | `core/runtime/contract.py` | Verified authorization contract binding approved targets, digest, channel, and capabilities. | **Authoritative execution boundary.** Required by orchestrator. |

---

## 4. Domain Model Specification (`core/runtime/task.py`)

### Fields

```python
@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    objective: str
    description: str = ""
    task_type: Optional[str] = None
    domain: Optional[str] = None
    requested_skills: Tuple[str, ...] = ()
    input_context: Mapping[str, Any] = field(default_factory=dict)
    parent_task_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = CURRENT_TASK_SCHEMA_VERSION
    created_at: float = field(default_factory=time.time)
    digest: str = ""
```

### Detailed Field Descriptions

- **`task_id`**: Deterministic, bounded (`<= 128` chars), path-safe identifier. Strictly an identifier, never a file path, module name, or command. Rejects path separators (`/`, `\`), traversal (`..`, `.`, `~`), drive letters (`C:`), injection characters, and control characters.
- **`objective`**: Primary non-empty statement of what needs to be done. Bounded (`<= 4096` chars). If `objective` is empty but `description` is provided, `description` is promoted to `objective`.
- **`description`**: Optional detailed instructions or background context. Bounded (`<= 8192` chars).
- **`task_type`**: Optional normalized lower-case classification token (e.g. `"implementation"`, `"research"`, `"testing"`, `"review"`, `"documentation"`, `"refactor"`, `"verification"`). Bounded (`<= 128` chars).
- **`domain`**: Optional normalized lower-case work domain (e.g. `"backend"`, `"frontend"`, `"security"`, `"infra"`). Bounded (`<= 128` chars).
- **`requested_skills`**: Immutable tuple of sorted, deduplicated, normalized skill tokens (e.g. `("fastapi", "python")`). Bounded (`<= 64` items, `<= 128` chars each).
- **`input_context`**: Deeply immutable `MappingProxyType` containing validated JSON-compatible inputs and parameters. Bounded in key count (`<= 64`) and nesting depth (`<= 5`). Actively scrubs/rejects secrets and authority-shaped keys.
- **`parent_task_id`**: Optional validated identifier of the parent task for decomposed task lineages. Must be path-safe and cannot be identical to `task_id`.
- **`metadata`**: Deeply immutable `MappingProxyType` containing descriptive metadata. Bounded in key count (`<= 64`) and depth (`<= 5`). Rejects authority-shaped keys and secrets.
- **`schema_version`**: Positive integer indicating the serialization schema version (currently `1`).
- **`created_at`**: Finite floating-point Unix timestamp.
- **`digest`**: Deterministic SHA-256 digest of semantic fields. Excludes volatile fields (`created_at`, `digest`).

---

## 5. Security & Validation Boundaries

### 5.1 Deep Immutability
`Task` instances are declared with `@dataclass(frozen=True, slots=True)`.
All collections are deeply frozen:
- Lists and sets are converted to immutable `tuple`.
- Mappings are recursively converted to `types.MappingProxyType`.
- Non-JSON-compatible types (functions, callables, file handles, arbitrary objects) are rejected at instantiation time.
- No mutation escape hatches exist.

### 5.2 Path Traversal & Identifier Confinement
`validate_task_id` enforces strict regex and prefix validation:
- Forbidden prefixes: `.`, `~`, `/`, `\`, `^[a-zA-Z]:`
- Forbidden characters: `[\x00-\x1f\x7f/\\*\?\"'<>\|;:~]|(?:\.\.)`
- Length bounds: `1 <= len(task_id) <= 128`

### 5.3 Secret Scrubbing & Rejection
`Task` enforces zero secret exposure across all attributes:
- Scans for private key headers (`-----BEGIN ... PRIVATE KEY-----`).
- Scans for high-entropy provider tokens (`sk-...`, `ghp_...`, Telegram bot tokens, Bearer tokens).
- Scans for secret assignment patterns and forbidden credential key names (`password`, `api_key`, `secret`, `authorization`, `cookie`).
- Violations raise `TaskSecretExposureError`.

### 5.4 Resource Caps & DoS Protection
Explicit limits protect memory and execution bounds:
- Maximum serialized payload: `64 KB` (`MAX_TASK_SERIALIZED_BYTES`).
- Maximum nesting depth: `5` (`MAX_NESTING_DEPTH`).
- Maximum string values in context/metadata: `4096` chars.

### 5.5 Canonical Digest & Tampering Resistance
`compute_digest()` produces a deterministic SHA-256 hash over canonical, key-sorted JSON:
```python
digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
```
If a `Task` is instantiated with a pre-set `digest` that does not match `compute_digest()`, instantiation fails closed with `TaskIntegrityError`.

---

## 6. Serialization

The model provides full round-trip JSON and dictionary serialization:

```python
from core.runtime.task import Task

task = Task(
    task_id="task_checkout_01",
    objective="Implement checkout flow payment validation",
    task_type="implementation",
    domain="fintech",
    requested_skills=("python", "stripe"),
    input_context={"currency": "USD", "retry_limit": 3},
    metadata={"priority": "high"},
)

# Convert to JSON
task_json = task.to_json()

# Reconstruct from JSON with strict validation
restored = Task.from_json(task_json)
assert restored.digest == task.digest
```

`Task.from_dict()` validates the root payload, rejecting:
1. Smuggled authority-shaped fields in root dictionary.
2. Unrecognized extra keys.
3. Malformed collection types.
