# Phase 15A: capability-based execution contracts

An `ApprovedExecutionContract` is an immutable authorization snapshot for one
operation. It is not an executor or an independently redeemable bearer token.
The runtime consumes an approval once, checks the requester, channel, session,
incarnation and workspace, then passes the contract to the existing orchestrator
with explicit execution identity (actor, session_id, session_incarnation_id).
In this checkout that single engine is **`orchestrator.py` at the repository
root**; there is no `core/orchestrator.py`. No second engine was introduced.

## Vocabulary and scope

| Capability | Phase 15A behavior |
| --- | --- |
| `filesystem.read` | Explicit, exact workspace-relative paths |
| `filesystem.write` | Explicit, exact paths, additionally bounded by operation targets |
| `shell.execute` | Defaults to false; execution unsupported even if proposed true |
| `network.access` | Defaults to false; true permits only application-configured model API calls |
| `git.read`, `git.commit`, `git.push` | Default false; Git execution under contracts is unsupported |

`NetworkPolicy.scope` must be `configured_model_api`. General HTTP requests,
browser access and downloads have no execution path under this policy. Model
credentials stay in provider configuration and are never contract fields.
CLI-backed model providers are rejected because they execute subprocesses.
Application-injected provider factories remain trusted application code, not
capabilities supplied by users or models. Model HTTP requests prohibit
transport-level redirects (allow_redirects=False).

Frozen domain types and strict decoding reject unknown groups, unknown policy
fields, flat names such as `{"shell.execute": true}`, and non-boolean flags.
Unspecified capabilities are denied. An operation name or a target by itself
does not grant access. Shell/Git flags can be parsed and examined as proposals,
but the executable-policy validator rejects them.

Filesystem scopes are sorted, deduplicated exact paths, not globs or recursive
directory grants. A scope of `src/auth.py` does not authorize `src/other.py`.
`**` and `src/**` are deliberately unsupported. Validation reuses
`targets.py` and checks resolved workspace containment, including single-file
symlinks. Absolute, UNC, traversal, URL, version and ambiguous identifier paths
fail closed. Tool and authorization state (`.git`, `.brainfrog`, `.codex`,
`.agents`, `.aws`) cannot be scoped, including through symlink aliases.
Existing batch staging rejects the entire proposed write set before writing
any file. This is validation atomicity, not a filesystem transaction or sandbox
against another local process concurrently replacing paths.

## Issuance and integrity

The trusted runtime proposes exact read/write targets and model API access for
supported file-write operations **before** requesting human approval. The prompt
shows these capabilities. Incoming metadata, System 1 decisions and System 2
output cannot add grants. Direct `ApprovalService.create_request()` calls default
to an empty capability set; application callers must supply explicit grants.

Schema 2 preserves `operation_digest` for operation-substitution checks and adds
`authorization_digest`, using the same canonical JSON/SHA-256 implementation.
It covers the operation and parameters, capabilities and policies, requester,
channel, session, incarnation, workspace root, request identity, nonce and times.
An approval attestation also binds that digest to the approver and approval time.
Integrity is rechecked before approval and under the existing store lock before
consumption. The one-use lifecycle, cross-process locking, quotas and session
invalidation remain in the existing approval system.

Schema 1 records remain readable for inspection and cancellation but cannot be
approved or consumed. Missing integrity data never receives inferred grants.
Contracts store a canonical immutable JSON snapshot rather than a mutable
operation reference; the compatibility accessor returns a detached copy.
Contract decoding rejects unknown fields, altered bindings, invalid capabilities
and expiration. Serialization has no credential fields or arbitrary operation
metadata; credential-bearing keys, known token formats and configured provider
secrets are rejected rather than redacted into a different authorization.
Callers must not put arbitrary credentials in identity/path fields: no generic
string classifier can recognize every possible secret.

Digests provide consistency and substitution detection, not signatures against
an attacker who can rewrite both trusted approval records and their digests.
Approval storage and application/provider code remain trusted, as in Phase 14.

## Execution and compatibility

Contract-bound writes use the existing planning and write/staging methods with
only approved context. Workspace guidelines, skills, images, history, saved
plans and memory cannot expand context or authority. Generated paths are checked
before reads and writes. Filesystem access rechecks expiration and workspace.

After bounded writes, the engine returns `unverified`: no test command, Git
checkpoint, PR, browser verification or reflection is run. The runtime can report
the file operation completed, but its response explicitly says verification was
not authorized. A file approval therefore no longer implicitly creates a commit
or runs repository code. Git hooks and arbitrary test programs cannot currently
be confined to exact file scopes, which is why those powers remain unsupported.

Telegram and WhatsApp retain their approval commands and cannot push, even with
a proposed Git push grant or forged metadata. Trusted local CLI execution without
a contract keeps its existing behavior. Old programmatic contract constructors
must use the approval issuance path before performing side effects.

This phase adds no Work state machine, autonomous loop, rollback, subagents,
messaging UI, scheduler, provider or memory architecture.

## Validation

Run `python -m unittest tests.test_capability_contract` for the new boundary tests,
and `python -m unittest discover -s tests` for all regressions. CI also uses
`python -m compileall -q core security system1 system2 tests cli.py orchestrator.py`.
Run `git diff --check` before submitting changes.
