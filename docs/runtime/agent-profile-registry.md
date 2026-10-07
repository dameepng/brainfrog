# BrainFrog — P1.4B Agent Profile Registry & Resolution

## 1. North Star & Purpose

BrainFrog Phase **P1.4B: Agent Profile Registry & Resolution** implements the deterministic registry lifecycle and profile resolution engine for specialized agents.

```text
Agent Profile answers:
"What kind of work is this agent suited for?"

It does not answer:
"What is this agent allowed to do?"
```

Resolution allows task routing to deterministically answer:
> *"Which known agent profile should handle this task?"*

It must **NEVER** answer:
> *"What is this agent allowed to execute?"*

Execution authorization remains exclusively governed by the existing canonical capability, delegation, approval, and transaction pipeline.

---

## 2. Hard Security Invariants

```text
ROUTING ≠ AUTHORIZATION
PROFILE ≠ CAPABILITY
REGISTRY ≠ AUTHORITY
RESOLUTION ≠ EXECUTION
```

All execution authority flows through the immutable P1.3 chain:

```text
AgentProfile
      ↓
Profile Registry
      ↓
Profile Resolution
      ↓
Task Routing
      ↓
DelegationContract
      ↓
Capability Attenuation
      ↓
ApprovedExecutionContract
      ↓
Transaction
      ↓
orchestrator.py (sole execution engine)
      ↓
Verification
```

Neither `AgentProfileRegistry` nor `ProfileResolutionResult` can grant capabilities, authorize actions, generate approval contracts, or invoke execution.

---

## 3. What an Agent Profile Represents vs Does NOT Represent

### What an Agent Profile Represents:
- Specialization metadata (skills, domain tags, task types).
- Non-authoritative constraints and preferences (e.g. preferred programming languages or artifact formats).
- Diagnostic descriptive metadata.

### What an Agent Profile Explicitly Does NOT Represent:
- **Filesystem authority**: Cannot grant read, write, or delete permissions.
- **Shell authority**: Cannot permit command execution or subprocess spawning.
- **Network authority**: Cannot permit outbound or inbound socket connections.
- **Git authority**: Cannot permit commits, tags, pushes, or branch operations.
- **Approval authority**: Cannot approve actions or bypass approval gates.
- **Transaction authority**: Cannot create, commit, or rollback transactions.
- **Orchestrator authority**: Cannot spawn orchestrators or bypass `orchestrator.py`.

---

## 4. Registry Lifecycle (`AgentProfileRegistry`)

The `AgentProfileRegistry` is a deterministic, bounded, in-memory domain registry.

### Operations:
1. **`register(profile: AgentProfile) -> None`**:
   - Rejects non-`AgentProfile` objects.
   - Enforces uniqueness: duplicate `profile_id` raises `AgentProfileDuplicateError`.
   - Enforces capacity limit: exceeding `MAX_REGISTRY_CAPACITY` (500) raises `AgentProfileError`.
   - Preserves exact profile identity without mutation.
2. **`get(profile_id: str) -> Optional[AgentProfile]`**:
   - Looks up profile by string ID; returns `None` if missing.
3. **`require(profile_id: str) -> AgentProfile`**:
   - Returns profile or raises `AgentProfileNotFoundError`.
4. **`list() -> Tuple[AgentProfile, ...]`**:
   - Returns all registered profiles deterministically sorted by `profile_id`.
5. **`remove(profile_id: str) -> AgentProfile`**:
   - Deletes and returns profile or raises `AgentProfileNotFoundError`.
6. **`resolve(request: ProfileResolutionRequest) -> ProfileResolutionResult`**:
   - Deterministically scores and resolves candidates.
7. **`require_resolve(request: ProfileResolutionRequest) -> AgentProfile`**:
   - Returns winning `AgentProfile` if `MATCHED`; raises `NoMatchingProfileError` if `NO_MATCH`; raises `AmbiguousProfileResolutionError` if `AMBIGUOUS`.

---

## 5. Resolution Semantics & Scoring Formula

Profile resolution is purely deterministic and scoring-based:

```python
score = task_type_score + domain_score + skill_score
```

- **`task_type` match (`TASK_TYPE_MATCH_WEIGHT = 10.0`)**:
  Matches if `request.task_type.lower() == profile_task_type.lower()`.
- **`domain` match (`DOMAIN_MATCH_WEIGHT = 5.0`)**:
  Matches if `request.domain.lower() == profile_domain.lower()`.
- **`skill` match (`SKILL_MATCH_WEIGHT = 1.0` per skill)**:
  Matches for each requested skill token matching `profile.skills` (case-insensitively).

### Normalization:
- **Whitespace**: Stripped from `task_type`, `domain`, and skill tokens.
- **Case**: Token comparisons are case-insensitive.
- **Duplicate Skills**: Deduplicated case-insensitively and sorted into immutable tuples.

---

## 6. Tie-Breaking & Ambiguity Handling

Resolution **never** makes arbitrary or non-deterministic choices:
- If multiple candidates achieve identical top scores:
  - Resolution status is set to `ProfileResolutionStatus.AMBIGUOUS`.
  - `profile_id` is set to `None`.
  - `candidate_ids` lists all tied candidate IDs deterministically sorted by `profile_id`.
  - Calling `registry.require_resolve(request)` fails closed by raising `AmbiguousProfileResolutionError`.
- No silent fallback to registration order, memory address, or random selection.

---

## 7. Resource Limits

All resolution structures enforce strict upper bounds to prevent memory or algorithmic complexity attacks:

| Constant | Limit | Description |
| :--- | :--- | :--- |
| `MAX_REGISTRY_CAPACITY` | 500 | Maximum registered profiles |
| `MAX_RESOLUTION_TASK_TYPE_CHARS` | 128 | Maximum length of requested task_type |
| `MAX_RESOLUTION_DOMAIN_CHARS` | 128 | Maximum length of requested domain |
| `MAX_RESOLUTION_SKILLS_COUNT` | 64 | Maximum number of requested skills |
| `MAX_RESOLUTION_SKILL_CHARS` | 128 | Maximum length of individual skill token |
| `MAX_RESOLUTION_CANDIDATE_IDS` | 64 | Maximum candidate IDs returned in diagnostics |
| `MAX_RESOLUTION_REASON_CHARS` | 1024 | Maximum diagnostic reason string length |
| `MAX_RESOLUTION_REQUEST_SERIALIZED_BYTES` | 16 KB | Serialized request byte limit |
| `MAX_RESOLUTION_RESULT_SERIALIZED_BYTES` | 32 KB | Serialized result byte limit |

Requests or results exceeding these bounds fail closed by raising `AgentProfileValidationError`.

---

## 8. Security Boundary & P1.3 Relationship

1. **Zero Execution Authority**:
   `core/runtime/agent_profile_registry.py` and `core/runtime/agent_profile.py` contain zero imports of `orchestrator`, `subprocess`, `os.system`, `Popen`, network clients, or LLM providers.
2. **Authority Smuggling Rejection**:
   Deserialization of `ProfileResolutionRequest` and `ProfileResolutionResult` actively scans for authority-shaped keys (e.g. `capabilities`, `permissions`, `allow_shell`, `orchestrator`, `approved_execution_contract`) and raises `AgentProfileAuthorityViolationError`.
3. **Secret Safety**:
   Credential patterns (API keys, GitHub tokens, Bearer tokens, Telegram bot tokens, private key headers, and password assignments) are actively rejected in resolution requests and scrubbed from resolution diagnostics.
4. **P1.3 Chain Intact**:
   Profile resolution merely suggests a profile ID. A subsequent routing layer (P1.4C/D) translates intent into a `DelegationContract`, and actual execution strictly requires `ApprovedExecutionContract`, `Transaction`, and `orchestrator.py`.
