# BrainFrog — P1.4A Agent Profile Domain & Registry

## 1. Purpose

BrainFrog Phase **P1.4A: Agent Profile Domain & Registry** establishes the formal domain model for specialized agents and a deterministic in-memory registry.

P1.4A is a **pure domain-model phase**. It defines:
- **`AgentProfile`**: An immutable specification describing what an agent profile is specialized in (skills, domains, task types, preferences, constraints, metadata).
- **`AgentProfileRegistry`**: A deterministic in-memory registry answering "Which agent profiles exist?"

---

## 2. Fundamental Invariants

### 1. `AGENT PROFILE ≠ AUTHORIZATION`
An `AgentProfile` describes specialization. It does **NOT** describe or confer execution authority.

```text
Specialization Description (AgentProfile)
       ≠
Execution Authorization (ApprovedExecutionContract)
```

A profile may describe:
- `skills`
- `domains`
- `task_types`
- `constraints` (preferences, supported artifact types)
- `metadata`

A profile must **NEVER** grant:
- Filesystem authority
- Shell authority
- Network authority
- Git authority
- Approval authority
- Transaction authority
- Orchestrator authority
- Session authority

All execution authority remains strictly governed by the canonical P1.3 security chain:

```text
Parent Capability
       ↓
Delegation Contract
       ↓
ApprovedExecutionContract
       ↓
Transaction
       ↓
orchestrator.py (sole execution engine)
       ↓
Verification
```

### 2. `REGISTRY ≠ AUTHORIZATION`
The `AgentProfileRegistry` answers exclusively:
> *"Which agent profile exists?"*

It never answers:
> *"What is this agent allowed to execute?"*

The registry contains no methods for authorization, approval, capability granting, or execution.

---

## 3. Domain Model Architecture

### Fields

| Field | Type | Description | Invariants |
| :--- | :--- | :--- | :--- |
| `profile_id` | `str` | Unique stable identifier | Non-empty, bounded (≤128 chars), no path traversal, no separator (`/`, `\`), no null/control characters, no secrets. |
| `schema_version` | `int` | Explicit schema version | Positive integer (defaults to `1`). |
| `display_name` | `str` | Human-readable name | Non-empty, bounded (≤256 chars), no control characters, no secrets. |
| `description` | `str` | Short specialization description | Descriptive only, bounded (≤4096 chars), no secrets. |
| `task_types` | `Tuple[str, ...]` | Specialized task-type tokens | Bounded count (≤64) and length (≤128 chars each), sorted unique tokens. |
| `domains` | `Tuple[str, ...]` | Specialization domains | Bounded count (≤32) and length (≤128 chars each), sorted unique tokens. |
| `skills` | `Tuple[str, ...]` | Specialization skills | Bounded count (≤128) and length (≤128 chars each), sorted unique tokens. |
| `constraints` | `Mapping[str, Any]` | Specialization preferences | Deeply immutable `MappingProxyType`, bounded (≤64 keys), strictly rejects authority-like keys. |
| `metadata` | `Mapping[str, Any]` | JSON-serializable metadata | Deeply immutable `MappingProxyType`, bounded (≤64 keys, max depth 5), strictly rejects authority-like keys. |
| `created_at` | `float` | Creation timestamp | Finite float. Excluded from semantic digest calculation. |
| `digest` | `str` | SHA-256 semantic digest | Deterministic hash over canonical JSON representation of semantic fields. |

### Immutability

`AgentProfile` is deeply immutable post-construction:
- Top-level dataclass is `@dataclass(frozen=True)`.
- Collections (`task_types`, `domains`, `skills`) are frozen into sorted tuples.
- Mappings (`constraints`, `metadata`) and nested mappings/sequences are deeply frozen into `MappingProxyType` and tuples.
- External mutations of input objects before or after construction have zero effect on profile state.

### Deterministic Cryptographic Digest

```python
profile.compute_digest()
```

- Computed as SHA-256 over canonical JSON serialization (`json.dumps(payload, sort_keys=True, separators=(',', ':'))`).
- Excludes volatile non-semantic fields (`created_at`, memory address, object identity).
- Equivalent semantic profiles always produce identical digests.
- Changing any semantic field (profile ID, display name, description, task types, domains, skills, constraints, metadata) alters the digest.
- Explicitly provided digests that do not match computed values raise `AgentProfileIntegrityError`.

---

## 4. Secret Safety & Authority Rejection

### Secret Rejection
Profile fields strictly reject credentials and secret material:
- API keys (e.g. OpenAI `sk-...`)
- GitHub tokens (`ghp_...`, `gho_...`, `ghu_...`, `ghs_...`, `ghr_...`)
- Private key headers (`-----BEGIN ... PRIVATE KEY`)
- Bearer tokens (`Bearer eyJ...`)
- Telegram bot tokens (`123456789:ABC...`)
- Password/credential assignments (`password: ...`, `api_key = ...`)
- Credential keys in mappings (`password`, `secret`, `credential`, `api_key`, `token`, `authorization`, `cookie`)

### Authority-Shaped Data Rejection
Attempting to specify execution-authority keys in `constraints` or `metadata` raises `AgentProfileAuthorityViolationError`:
- `capabilities`, `capability`, `permissions`, `permission`, `authority`
- `allow_shell`, `allow_network`, `allow_filesystem`, `allow_filesystem_write`
- `allow_git_push`, `allow_git_commit`
- `can_approve`, `can_execute`, `can_mutate`
- `approved_execution_contract`, `execution_contract`
- `transaction_id`, `transaction_authority`
- `orchestrator`, `orchestrator_authority`, `session_authority`

---

## 5. In-Memory Registry (`AgentProfileRegistry`)

The `AgentProfileRegistry` provides deterministic in-memory management:

```python
registry = AgentProfileRegistry()
registry.register(profile)
retrieved = registry.get("backend_engineer")
required = registry.require("backend_engineer")
all_profiles = registry.list()
removed = registry.remove("backend_engineer")
```

### Invariants
1. **Duplicate Prevention**: Registering an already-registered `profile_id` raises `AgentProfileDuplicateError`.
2. **Deterministic Ordering**: `list()` returns profiles deterministically sorted by `profile_id`.
3. **Bounded Capacity**: Rejects registrations exceeding `MAX_REGISTRY_CAPACITY` (500 profiles).
4. **Isolated Memory**: No filesystem I/O, no network calls, no persistence layer.
5. **Zero Authority**: Exposes zero methods for execution, approval, or capability delegation.

---

## 6. Static Architecture and Dependency Invariants

`core/runtime/agent_profile.py` is a pure domain module:
- **Zero orchestrator imports**: Does not import `orchestrator.py` or create orchestrators.
- **Zero subprocess / shell calls**: Does not import `subprocess` or invoke `os.system` / `Popen`.
- **Zero filesystem mutation**: Purely in-memory dataclass and registry.
- **Zero network / LLM clients**: Does not import `requests`, `urllib`, `openai`, or `anthropic`.
- **Zero execution dependencies**: Does not import `transaction`, `approval`, `contract`, or `work`.
