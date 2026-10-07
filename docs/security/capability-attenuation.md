# BrainFrog P1.3C — Capability Attenuation Security Specification

## Core Security Invariant

> **A delegated child may only receive a capability set that is equal to or strictly narrower than the parent's authority.**
>
> $$\text{CHILD} \subseteq \text{PARENT}$$

Under no circumstance can a delegated child expand, escalate, bypass, or renew any authority-bearing dimension beyond what was granted to its parent.

---

## 1. Threat Model

BrainFrog is a dual-system agent runtime wherein System 1 formulates intent and System 2 (via `orchestrator.py`) executes verified, approved work within transactions.

With the introduction of scoped subagent workers in P1.3, subagents act as subordinate task workers. Subagents are workers, not authorities. Without rigorous capability attenuation, multiple threat vectors arise:

1. **Horizontal Escalation**: A subagent attempting to access files, directories, or endpoints belonging to sibling components or other tenants.
2. **Vertical Escalation**: A subagent attempting to obtain higher privileges than its parent (e.g. escalating from `read_file` to `write_file` or `run_shell_command`, or escalating Git from read-only to remote push).
3. **Scope Confusion & Traversal**: Exploiting naive string prefixes (e.g. `src/` matching `src_evil/`), directory traversal (`..`, `.`), or drive/UNC path escapes.
4. **Time Confusion**: Extending expiration timestamps or attempting delegation from an expired parent.
5. **Cross-Session Hijacking**: Delegating to a subagent bound to a different actor, session, or stale session incarnation.
6. **Integrity Tampering**: Modifying declared authority post-issuance without detection.

---

## 2. Canonical Capability Representation

BrainFrog's capability domain is partitioned into canonical models:

- **`Capabilities`** (`core/runtime/capabilities.py`):
  - `filesystem: FilesystemPolicy`: Tuple of authorized exact `read` and `write` paths.
  - `shell: ShellPolicy`: Boolean `execute` flag.
  - `network: NetworkPolicy`: Boolean `access` flag and `scope` identifier (`"configured_model_api"`).
  - `git: GitPolicy`: Boolean `read`, `commit`, and `push` flags.

- **`DelegationContract`** (`core/runtime/delegation.py`):
  - Declarative scope specification binding parent work, child subagent, actor, and session incarnation.
  - Authority dimensions: `capabilities`, `target_scope`, `operation_scope`, `network_scope`, and `git_policy`.
  - Cryptographic SHA-256 digest calculated over canonical JSON of all authority-bearing fields.

---

## 3. Attenuation Rules Across Authority Dimensions

Attenuation is evaluated monotonically across every authority dimension:

$$\begin{aligned}
\text{child.filesystem} &\subseteq \text{parent.filesystem} \\
\text{child.target\_scope} &\subseteq \text{parent.target\_scope} \\
\text{child.network\_scope} &\subseteq \text{parent.network\_scope} \\
\text{child.operation\_scope} &\subseteq \text{parent.operation\_scope} \\
\text{child.shell.execute} &\le \text{parent.shell.execute} \\
\text{child.network.access} &\le \text{parent.network.access} \\
\text{child.git\_policy} &\le \text{parent.git\_policy} \\
\text{child.actor} &= \text{parent.actor} \\
\text{child.session\_id} &= \text{parent.session\_id} \\
\text{child.session\_incarnation\_id} &= \text{parent.session\_incarnation\_id} \\
\text{child.expires\_at} &\le \text{parent.expires\_at}
\end{aligned}$$

---

## 4. Filesystem Containment Semantics

Filesystem targets in BrainFrog are structural path hierarchies, not naive substrings.

### Normalization
1. Backslashes `\` are converted to forward slashes `/`.
2. Whitespace is trimmed.
3. Empty paths raise `DelegationValidationError`.
4. Traversal components (`..` and `.`) in any position raise `DelegationAttenuationError`.
5. Absolute paths (`/`, `\`), UNC paths (`//`, `\\`), and drive-letter paths (`C:`) raise `DelegationAttenuationError`.
6. Repeated separators (e.g. `src///auth`) are normalized to single slashes (`src/auth`).
7. Protected authorization roots (`.git`, `.brainfrog`, `.codex`, `.agents`, `.aws`) are prohibited and raise `DelegationAttenuationError`.

### Containment Logic (`is_path_subset(child, parent)`)
- **Exact Match**: `src/auth.py` matches `src/auth.py`.
- **Recursive Subtree (`dir/**`)**:
  - `src/**` permits `src/auth/**`, `src/auth/*`, `src/auth`, and `src/auth/login.py`.
  - Does NOT permit `src_evil/**` (sibling prefix confusion prevention).
  - Does NOT permit workspace-wide glob `**`.
- **Direct Children (`dir/*`)**:
  - `src/*` permits direct children e.g. `src/a.py` and `src/sub`.
  - Does NOT permit nested descendants e.g. `src/sub/nested.py` or recursive globs `src/**`.
- **Directory Scope (`dir/` or `dir`)**:
  - A directory scope permits descendant files inside that directory.
  - Exact file scopes do NOT permit descendant subpaths or sibling files.
  - When matching under a directory scope `p` (ending in `/`), child subpaths relative to the directory prefix cannot contain further wildcards (`*`), ensuring mathematical transitivity across wildcard directory scopes (e.g. `*/tools` $\subseteq$ `*/*` $\subseteq$ `*/`).
- **Inversion Defense**: A child requesting a parent directory when the parent only has a child directory or file is strictly rejected (`descendant -> parent` yields `False`).

---

## 5. Network Containment Semantics

Network attenuation governs hostnames, schemes, and ports.

### Rules (`is_network_subset(child, parent)`)
1. **Wildcard Containment**:
   - Parent `*` permits any endpoint.
   - Child requesting `*` when parent is restricted is strictly DENIED.
   - Wildcard subdomain: parent `*.example.com` permits `api.example.com` and `sub.api.example.com`.
   - Wildcard subdomain `*.example.com` does **NOT** authorize the apex domain `example.com` (conforming to RFC 6125 §6.4.3). Child requesting apex or unrelated sibling domains under `*.example.com` is DENIED.
2. **Scheme Security**:
   - HTTPS cannot be downgraded to HTTP.
   - If parent specifies HTTPS, child cannot omit the scheme to imply arbitrary/insecure protocols.
   - Non-HTTP/HTTPS schemes (e.g. `ftp://`, `ws://`) are rejected unless explicitly matched.
3. **Port Attenuation**:
   - If parent restricts to an explicit port (e.g. `:8443`), child must match that exact port.
   - Child cannot omit the port to widen access to default ports.
   - Effective standard ports (443 for HTTPS, 80 for HTTP) are preserved and compared.
4. **Delimiter and Parser Confusion Defense**:
   - Endpoints containing fragments (`#`), queries (`?`), backslashes (`\`), or paths (`/`) are parsed strictly, isolating the host component.
   - Query strings and fragments cannot masquerade as host components or wildcard domain suffixes (e.g. `evil.com#api.example.com` and `evil.com?api.example.com` are strictly rejected).
   - Endpoints containing userinfo credentials (`user:password@host`) or whitespace/control characters are rejected unconditionally.
5. **Configured Model API**:
   - The token `"configured_model_api"` is a closed scope and cannot be escalated to external internet hosts or wildcards.

---

## 6. Operation Hierarchy

Operations represent privilege levels within BrainFrog and are validated against `KNOWN_OPERATIONS`:

```python
KNOWN_OPERATIONS = {
    "read_file", "read_files", "read_code",
    "write_file", "write_files", "write_code",
    "edit_file", "delete_file",
    "run_shell_command", "shell_execution",
    "deploy_project", "deployment",
    "git_read", "git_commit", "git_push",
}
```

- **Set Inclusion**: Every requested child operation must exist in the parent's operation scope.
- **Unknown Operations**: Any operation outside `KNOWN_OPERATIONS` fails closed (`DelegationAttenuationError`).
- **Empty Operations**: If parent has no operations, child can obtain no operations. If child requests empty operations `()`, it receives 0 execution operations.

---

## 7. Git Policy Hierarchy

Git policy is strictly monotonic:

| Parent Policy | Child Requested | Result | Reason |
| :--- | :--- | :--- | :--- |
| `read=True, commit=False, push=False` | `read=True, commit=False, push=False` | ALLOW | Identical read-only |
| `read=True, commit=False, push=False` | `read=True, commit=True, push=False` | DENY | Commit escalation |
| `read=True, commit=True, push=False` | `read=True, commit=True, push=True` | DENY | Push escalation |
| `read=True, commit=True, push=True` | `read=True, commit=False, push=False` | ALLOW | Monotonic attenuation |

---

## 8. Identity and Time Constraints

- **Actor Binding**: `child.actor == parent.actor`. Delegation cannot cross human user boundaries.
- **Session Binding**: `child.session_id == parent.session_id`. Delegation cannot cross sessions.
- **Incarnation Binding**: `child.session_incarnation_id == parent.session_incarnation_id`. Stale session incarnations are rejected.
- **Parent Work Binding**: Child is explicitly bound to `parent_work_id`.
- **Expiration Bound**: `child.expires_at <= parent.expires_at`. A child cannot outlive its parent, and child expiration must be strictly greater than creation time. An expired parent cannot delegate.

---

## 9. Integrity Guarantees

Each `DelegationContract` computes an immutable SHA-256 digest over the canonical JSON representation of:
- `schema_version`
- `delegation_id`
- `parent_work_id`
- `child_subagent_id`
- `actor`
- `session_id`
- `session_incarnation_id`
- `capabilities`
- `target_scope`
- `operation_scope`
- `network_scope`
- `git_policy`
- `created_at`
- `expires_at`
- `parent_delegation_id`

Any modification to authority-bearing fields invalidates the digest, failing verification via `DelegationIntegrityError`.

---

## 10. Fail-Closed Behavior

Ambiguity, null values, or missing fields fail closed:
- `None` or `[]` does not grant unrestricted access.
- Unknown capability fields or malformed dictionaries raise exceptions.
- Ill-formed target or endpoint strings raise validation/attenuation errors.

---

## 11. Composition and Transitivity

Capability attenuation is transitive across multi-tier delegation chains:

$$\text{Grandchild} \subseteq \text{Child} \subseteq \text{Parent} \implies \text{Grandchild} \subseteq \text{Parent}$$

If a child has been attenuated to a narrower scope, a grandchild deriving from that child cannot re-gain authorities possessed by the parent but denied to the child.

---

## 12. Explicit Scope Boundary: What P1.3C Does NOT Implement

P1.3C is strictly a capability and security domain phase:
- It does **NOT** spawn workers or child processes.
- It does **NOT** execute subprocesses, multiprocessing, or asynchronous threads.
- It does **NOT** mutate the filesystem, execute Git commands, or initiate network connections.
- It does **NOT** create a second orchestrator or execution engine (`orchestrator.py` remains sole engine).
- It does **NOT** introduce new approval stores, transaction managers, or session backends.
