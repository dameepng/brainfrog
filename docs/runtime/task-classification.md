# BrainFrog Deterministic Task Classification (Phase P1.4D)

## 1. Executive Summary & North Star

> **“BrainFrog — an agent runtime that turns intent into verified action.”**

Phase **P1.4D** introduces the canonical **Deterministic Task Classification Layer** (`core/runtime/task_classification.py`).

P1.4D answers the foundational taxonomic question:
> **“What kind of work is this Task?”**

Crucially, P1.4D does **NOT** answer:
> “Which agent should execute it?”

Agent selection and profile binding belong exclusively to **Phase P1.4E (Routing)**.

P1.4D is a **pure domain classification layer**. It contains zero execution machinery, zero subprocesses, zero network access, zero LLM calls, zero scheduling, zero persistence, and zero authorization semantics.

---

## 2. Core Architectural & Security Invariants

The fundamental architectural principle governing Phase P1.4D is strict separation of descriptive classification from authorization and execution:

```text
CLASSIFICATION ≠ AUTHORIZATION
CLASSIFICATION ≠ CAPABILITY
CLASSIFICATION ≠ ROUTING
PROFILE ≠ CAPABILITY
ROUTING ≠ AUTHORIZATION
RESOLUTION ≠ EXECUTION
```

`orchestrator.py` remains the **sole execution engine** in the BrainFrog runtime.

### Invariant Rules
1. **Descriptive, Never Authoritative**: A `TaskClassification` describes taxonomy, domain, skills, evidence, and confidence. It MUST NOT grant, imply, encode, or derive permission to perform that work.
   - Example: `task_type = "debugging"` means the work appears to be debugging; it does NOT grant permission to debug or execute fixes.
   - Example: `skills = ("python", "redis")` describes needed skills; it does NOT grant filesystem access to Python scripts or network access to Redis.
2. **Zero Capability / Execution Fields**: A `TaskClassification` has NO fields, methods, or properties for `capabilities`, `permissions`, `allowed_tools`, `allowed_commands`, `allowed_files`, `network_access`, `approval_status`, `approved`, `authorized`, `execution_contract`, `delegation_contract`, `actor_credentials`, `session_credentials`, `auth_tokens`, `secrets`, `executor`, `orchestrator`, or `shell_access`.
3. **Pure Determinism**: Given the same `Task` and the same `classifier_version`, `DeterministicTaskClassifier` always yields the exact same `TaskClassification` and semantic digest.
   - No randomness.
   - No timestamps in semantic identity.
   - No environment-dependent behavior.
   - No model calls.
   - No external state.
   - No filesystem or network probing.
4. **Canonical Execution Chain Preserved**: All execution authority remains governed exclusively by the canonical P1.3 chain:
   ```text
   Parent Capability -> DelegationContract -> ApprovedExecutionContract -> Transaction -> orchestrator.py
   ```

---

## 3. Explicit vs Inferred Classifications

`DeterministicTaskClassifier` strictly prioritizes author-declared intent over heuristic inference:

1. **Explicit Values Respected**:
   - If `Task.task_type` is declared, the classifier preserves and normalizes it without overriding. Evidence records `"explicit:task_type:<value>"`.
   - If `Task.domain` is declared, the classifier preserves and normalizes it without overriding. Evidence records `"explicit:domain:<value>"`.
   - If `Task.requested_skills` are declared, they are preserved, normalized, and included. Evidence records `"explicit:skill:<skill>"` for each skill.
2. **Deterministic Inference Fallback**:
   - If `task_type` is omitted, the rule engine inspects `Task.objective` and `Task.description` using compiled word-boundary keyword patterns.
   - If `domain` is omitted, domain rules inspect `objective` and `description`.
   - Detects mentioned technologies (e.g., `python`, `typescript`, `docker`, `pytest`) and merges them into `skills`.
3. **Fail-Closed on Insufficient Evidence**:
   - The classifier **never guesses**. If the text lacks recognizable keyword patterns, `task_type` and `domain` remain `None`, `skills` remains empty, and confidence is set to `ClassificationConfidence.UNKNOWN`.

---

## 4. Confidence Levels

Classification confidence describes taxonomic pattern match certainty. It **never** conveys authorization or permission confidence:

| Level | Value | Meaning |
| :--- | :--- | :--- |
| **`EXPLICIT`** | `"explicit"` | Both `task_type` and `domain` were explicitly declared on the Task. |
| **`HIGH`** | `"high"` | At least one field was explicitly declared, or multiple strong keyword rule matches were identified. |
| **`MEDIUM`** | `"medium"` | Exactly one clear keyword match signal identified for `task_type`. |
| **`LOW`** | `"low"` | Weak signal; only domain or skills inferred without a recognizable `task_type`. |
| **`UNKNOWN`** | `"unknown"` | Insufficient evidence to classify; task is too vague or uninformative. |

---

## 5. Domain Model Specification (`core/runtime/task_classification.py`)

### `TaskClassification` Dataclass

```python
@dataclass(frozen=True, slots=True)
class TaskClassification:
    task_id: str
    task_type: Optional[str] = None
    domain: Optional[str] = None
    skills: Tuple[str, ...] = ()
    confidence: ClassificationConfidence = ClassificationConfidence.UNKNOWN
    classifier_version: int = CURRENT_TASK_CLASSIFICATION_SCHEMA_VERSION
    evidence: Tuple[str, ...] = ()
    is_inferred: bool = False
    digest: str = ""
```

### Resource Bounds
- `MAX_TASK_CLASSIFICATION_SERIALIZED_BYTES = 16384` (16 KB)
- `MAX_TASK_ID_CHARS = 128`
- `MAX_TASK_TYPE_CHARS = 128`
- `MAX_DOMAIN_CHARS = 128`
- `MAX_SKILLS_COUNT = 64`
- `MAX_SKILL_CHARS = 128`
- `MAX_EVIDENCE_COUNT = 64`
- `MAX_EVIDENCE_CHARS = 128`

### Semantic Digest
- Calculated via `compute_digest() -> str` using SHA-256 over canonical JSON.
- Strictly excludes non-semantic volatile attributes.
- Serialized dictionary sorted deterministically.

---

## 6. Relationship to Future Phases

```text
       Task (P1.4C)
            ↓
TaskClassification (P1.4D)  <-- "What kind of work is this?" (Descriptive only)
            ↓
  Routing Layer (P1.4E)     <-- "Which agent profile fits this classification?"
            ↓
  AgentProfile (P1.4A/B)    <-- Descriptive agent specialization
            ↓
Execution Boundary (P1.3)   <-- ApprovedExecutionContract -> Transaction -> orchestrator.py
```

Neither P1.4D classification nor P1.4E routing can grant capabilities, approve transactions, or execute work.
