# BrainFrog — P1.3J Task Routing & Subagent Specialization

## 1. Purpose

Phases P1.3A through P1.3I established the scoped subagent execution runtime:
- **P1.3A/B**: `Subagent`, `DelegationContract`, and capability attenuation (`Capabilities`).
- **P1.3C**: `ChildWork` bound to canonical `Work`.
- **P1.3D/E**: `DelegationRuntime` with sequential and parallel scheduling DAGs.
- **P1.3F**: `ResultAggregator` and deterministic `ChildResult` composition.
- **P1.3G**: `propagate_failure` and `propagate_child_cancellation`.
- **P1.3I**: `SubagentExecutionCoordinator` bridging ChildWork to `orchestrator.py`.

Prior to P1.3J, delegating tasks required manual or ad-hoc assignment of subagent parameters.

**P1.3J adds a deterministic routing layer that maps a planned task to an eligible subagent profile based on task requirements and available capabilities:**

```text
Task Requirements
      ↓
Subagent Profile Selection
      ↓
DelegationContract
      ↓
ChildWork
      ↓
DelegationRuntime
      ↓
Approval / Execution Contract
      ↓
Transaction
      ↓
orchestrator.py (sole execution engine)
      ↓
Verification
      ↓
ChildResult
      ↓
ResultAggregator
      ↓
Parent Work
```

P1.3J makes subagent profile selection explicit, reproducible, and verifiable without introducing an autonomous multi-agent planner.

---

## 2. Hard Architectural Invariants

The following invariants are strictly preserved:

1. **`orchestrator.py` remains the SOLE execution engine.**
2. **The router has ZERO execution authority:**
   - The router MUST NOT execute shell commands or subprocesses.
   - The router MUST NOT mutate the filesystem or repository.
   - The router MUST NOT invoke LLMs or perform heuristic/natural-language guessing.
   - The router MUST NOT grant or elevate capabilities.
   - The router MUST NOT mutate `Work` records upon routing failure.
3. **The router only selects among pre-registered, eligible profiles.**
4. **DelegationContract remains the authority boundary.**
5. **Capability attenuation remains mandatory:**
   $$\text{Child Authority} = \text{Parent Authority} \cap \text{Profile Capabilities} \cap \text{Task Requirements}$$
   A subagent profile can never expand a parent's granted capabilities.
6. **Fail closed on ambiguity or absence:** If no registered profile satisfies all requirements, the router raises `NoEligibleSubagentError` immediately. It never silently falls back to an over-privileged or ill-suited profile.
7. **Deterministic output:** The router produces identical profile selections for identical inputs across runs, processes, and architectures.

---

## 3. Task Type Taxonomy

P1.3J defines a compact, deterministic task type taxonomy via `TaskType(str, Enum)`:

| TaskType | Semantic Meaning | Primary Operations |
|---|---|---|
| `RESEARCH` | Read-only codebase inspection, search, diagnosis, planning | `read_code`, `diagnose`, `plan` |
| `IMPLEMENTATION` | Code authoring, feature additions, bug fixes | `write_code`, `write_files`, `read_code` |
| `TESTING` | Test authoring, test execution, regression verification | `run_tests`, `read_code`, `write_code` |
| `REVIEW` | Read-only code review, architectural compliance checks | `read_code`, `diagnose` |
| `DOCUMENTATION` | Documentation updates, markdown guides, specs | `write_code`, `write_files`, `read_code` |
| `REFACTOR` | Code restructuring, cleanup, interface alignment | `write_code`, `write_files`, `read_code` |
| `VERIFICATION` | Safety checks, lint checks, test suite verification | `read_code`, `run_tests`, `diagnose` |

Any unknown or malformed task type fails closed with `TaskRoutingValidationError` or `UnknownTaskTypeError`.

---

## 4. Domain Models

All P1.3J domain models reside in `core/runtime/subagent_routing.py` and are immutable dataclasses with strict validation.

### 4.1 SubagentProfile

`SubagentProfile` is a pure declarative description of a worker's role and boundaries:

```python
@dataclass(frozen=True)
class SubagentProfile:
    profile_id: str                          # Lowercase alphanumeric/underscore identifier
    name: str                                # Human-readable name
    description: str                         # Concise role description
    required_capabilities: Capabilities      # Maximum capabilities permitted for this profile
    supported_operations: Tuple[str, ...]    # Allowed operation names (e.g. "write_code")
    supported_task_types: Tuple[TaskType, ...]# Allowed TaskTypes
    max_risk_class: RiskClass = RiskClass.LOW# Ceiling on permissible risk level
    network_scope: Tuple[str, ...] = ()      # Permitted network domains/endpoints
    filesystem_scope: Tuple[str, ...] = ()   # Permitted filesystem paths/globs (e.g. "src/**")
    git_policy: GitPolicy = GitPolicy()      # Permitted git operations
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
```

**Profiles are pure data.** A profile MUST NOT contain:
- Executable callables or callbacks
- Shell/subprocess strings or commands
- LLM prompts with authority
- Approval bypasses or transaction handles
- Orchestrator references

### 4.2 TaskRoutingRequest

`TaskRoutingRequest` captures what a planned task requires without executing anything:

```python
@dataclass(frozen=True)
class TaskRoutingRequest:
    task_id: str                             # Identifier of the task
    task_type: TaskType                      # Standardized task type
    required_capabilities: Capabilities      # Required capabilities
    operation: str                           # Operation to perform (e.g. "write_code")
    risk_class: RiskClass = RiskClass.LOW    # Assessed risk class of the task
    target_scope: Tuple[str, ...] = ()       # Target filesystem paths (relative, normalized)
    network_requirements: Tuple[str, ...] = ()# Required network endpoints
    filesystem_requirements: Tuple[str, ...] = ()
    git_requirements: GitPolicy = GitPolicy()
    parent_capabilities: Optional[Capabilities] = None # Optional parent upper bound
    metadata: Dict[str, Any] = field(default_factory=dict)
```

### 4.3 SubagentRoutingResult

`SubagentRoutingResult` records the deterministic decision and its audit digest:

```python
@dataclass(frozen=True)
class SubagentRoutingResult:
    task_id: str                             # Matched task ID
    selected_profile_id: str                 # ID of chosen profile
    matched_capabilities: Capabilities       # Profile capabilities matched to request
    routing_reason: str                      # Explanation of why profile was chosen
    candidate_count: int                     # Number of eligible candidates considered
    routing_digest: str                      # SHA-256 cryptographic digest of the decision
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
```

The `routing_digest` is computed via canonical JSON serialization of the task ID, selected profile ID, matched capabilities, and candidate count. It is reproducible across environments and safe to persist in Work metadata.

---

## 5. SubagentProfileRegistry

`SubagentProfileRegistry` provides an immutable/read-only registry with conservative resource limits:
- Maximum profiles: `MAX_PROFILES = 64`
- Profile IDs must be unique lowercase identifiers `[a-z0-9_-]+`
- Rejects duplicate profile registrations (`DuplicateProfileError`)
- Rejects registration beyond capacity (`ProfileRegistryLimitError`)

### Standard Built-In Profiles

`create_default_registry()` supplies the canonical BrainFrog worker profiles:

| Profile ID | Role | Capabilities | Operations | Task Types | Max Risk |
|---|---|---|---|---|---|
| `researcher` | Codebase Researcher | Read-only across `src/`, `tests/`, `docs/`, `ui/` | `read_code`, `diagnose`, `plan` | `RESEARCH`, `REVIEW` | `LOW` |
| `backend_worker` | Backend Implementation | Read `src/`, `tests/`, `docs/`; Write `src/` | `write_code`, `write_files`, `read_code`, `diagnose` | `IMPLEMENTATION`, `REFACTOR` | `MEDIUM` |
| `frontend_worker` | Frontend Implementation | Read `src/`, `ui/`; Write `ui/` | `write_code`, `write_files`, `read_code` | `IMPLEMENTATION`, `REFACTOR` | `MEDIUM` |
| `test_runner` | Test & Verification | Read `src/`, `tests/`; Write `tests/` | `run_tests`, `read_code`, `write_code`, `write_files` | `TESTING`, `VERIFICATION` | `MEDIUM` |
| `reviewer` | Safety & Architecture Reviewer | Read-only across all source directories | `read_code`, `diagnose` | `REVIEW`, `VERIFICATION` | `LOW` |
| `documentation_worker`| Technical Documentation | Read `src/`, `docs/`; Write `docs/` | `write_code`, `write_files`, `read_code` | `DOCUMENTATION` | `LOW` |

---

## 6. Eligibility Evaluation Rules

When matching a `TaskRoutingRequest` against candidate profiles in `is_profile_eligible(profile, request)`:

1. **Task Type Compatibility:**
   `request.task_type in profile.supported_task_types`
2. **Operation Compatibility:**
   `request.operation in profile.supported_operations`
3. **Risk Class Ceiling:**
   `request.risk_class <= profile.max_risk_class`
4. **Capability Containment ($\text{Required} \subseteq \text{Profile}$):**
   - Shell: `request.shell.execute <= profile.shell.execute`
   - Network: `request.network.access <= profile.network.access`
   - Git: `request.git <= profile.git` (read, commit, push)
   - Filesystem Read: all requested read paths must be covered by `profile.required_capabilities.filesystem.read`
   - Filesystem Write: all requested write paths must be covered by `profile.required_capabilities.filesystem.write`
5. **Scope Compatibility ($\text{Target Scope} \subseteq \text{Profile Scope}$):**
   - All targets in `request.target_scope` and `request.filesystem_requirements` must match `profile.filesystem_scope`
   - If the task is mutating (`write_code`, `write_files`, or write capabilities requested), all targets must also be covered by `profile.required_capabilities.filesystem.write`
   - If the task is read-only, all targets must be covered by `profile.required_capabilities.filesystem.read`
6. **Network Scope Compatibility:**
   - All endpoints in `request.network_requirements` must be covered by `profile.network_scope`
7. **Parent Upper Bound:**
   - If `request.parent_capabilities` is provided:
     `profile.required_capabilities` must be attenuatable within `parent_capabilities` without violating the parent boundary.

If any check fails, the profile is rejected with an explicit, non-throwing reason string.

---

## 7. Deterministic Ranking

When multiple profiles satisfy all eligibility criteria, `SubagentRouter` applies strict deterministic ranking:

1. **Exact Task-Type Match:** Profiles listing the requested `TaskType` as their primary role score highest.
2. **Smallest Sufficient Capability Surface:** Profiles with narrower granted capability surfaces (fewer read/write scopes, no shell, no network) are preferred over broader profiles (principle of least privilege).
3. **Smallest Sufficient Scope Surface:** Profiles with narrower filesystem scope definitions score higher than universal profiles.
4. **Lowest Max Risk Class:** Profiles with lower risk ceilings are preferred.
5. **Stable Profile ID Tie-Breaker:** Lexicographical sort on `profile_id` ensures deterministic outcomes across runs.

No randomness (`random.choice()`), system clock timestamps, or Python's process-dependent `hash()` are used in ranking.

---

## 8. Capability Attenuation & Delegation Integration

Routing produces a profile, which then constrains the creation of `DelegationContract` and `ChildWork`:

```python
# 1. Route task to profile
router = SubagentRouter(registry)
routing_result = router.route(task_request)
profile = registry.get(routing_result.selected_profile_id)

# 2. Construct attenuated delegation
contract = create_routed_delegation(
    parent_work=parent_work,
    profile=profile,
    request=task_request,
    child_subagent_id=subagent.id,
    delegation_id="del_001",
    expires_in_seconds=300.0,
)

# 3. Construct canonical ChildWork
child_work = create_routed_child_work(
    parent_work=parent_work,
    profile=profile,
    request=task_request,
    child_work_id="work_child_001",
    expires_in_seconds=300.0,
)
```

The resulting delegation capability is computed via:
```python
final_caps = attenuate_capabilities(parent_caps, requested_caps)
```
Where `requested_caps` is already constrained to the intersection of the profile capabilities and task requirements. **Under no circumstances can the subagent receive authority exceeding the parent Work.**

---

## 9. Failure and Lifecycle Semantics

- **Routing Failure:** If no profile is eligible, `SubagentRouter.route()` raises `NoEligibleSubagentError`.
  - The router does NOT mutate the parent `Work` or mark tasks as `FAILED`.
  - The caller (planner or coordinator) handles the exception according to standard Work lifecycle rules.
- **P1.3G Invariants Preserved:**
  - A failed child transitions to `WorkStatus.FAILED`.
  - Downstream dependents become `CoordinationState.BLOCKED` while retaining non-terminal `WorkStatus`.
  - Cancelled children transition to `WorkStatus.CANCELLED`.
  - Blocked children are never executed.

---

## 10. Concrete End-to-End Example

```python
from core.runtime.capabilities import Capabilities, FilesystemPolicy
from core.runtime.approval import RiskClass
from core.runtime.subagent_routing import (
    TaskType,
    TaskRoutingRequest,
    SubagentRouter,
    create_default_registry,
    create_routed_child_work,
)

# Setup registry and router
registry = create_default_registry()
router = SubagentRouter(registry)

# Define task routing request
request = TaskRoutingRequest(
    task_id="task_backend_auth",
    task_type=TaskType.IMPLEMENTATION,
    required_capabilities=Capabilities(
        filesystem=FilesystemPolicy(read=("src/auth/",), write=("src/auth/",))
    ),
    operation="write_code",
    risk_class=RiskClass.MEDIUM,
    target_scope=("src/auth/session.py",),
    parent_capabilities=parent_work.capabilities,
)

# Deterministic routing
result = router.route(request)
# result.selected_profile_id == "backend_worker"
# result.routing_digest contains reproducible SHA-256 hash

# Construct validated ChildWork
child_work = create_routed_child_work(
    parent_work=parent_work,
    profile=registry.get(result.selected_profile_id),
    request=request,
    child_work_id="work_child_auth_01",
)

# Execution proceeds via P1.3I SubagentExecutionCoordinator to orchestrator.py
```
