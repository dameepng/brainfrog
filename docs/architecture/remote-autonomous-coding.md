# Remote Autonomous Coding (Phase 15F)

Phase 15F connects authenticated Telegram and WhatsApp requests to BrainFrog's existing planning, approval, capability, orchestration, and transaction components. It adds coordination and lifecycle reporting; it does not add another execution engine.

## End-to-end flow

```text
Telegram / WhatsApp
  -> IncomingMessage and authenticated session identity
  -> channel policy and canonical operation extraction
  -> Work(CREATED -> PLANNING)
  -> deterministic Plan
  -> Work(APPROVAL_REQUIRED)
  -> approval request
  -> ApprovedExecutionContract
  -> Work(EXECUTING)
  -> orchestrator.py
  -> System 2 proposes bounded file contents
  -> Transaction stages and journals mutations
  -> verifier
  -> commit or reverse rollback
  -> Work(VERIFYING -> DONE) or Work(FAILED)
  -> structured remote result
```

`RemoteWorkCoordinator` owns only the descriptive Work/Plan correlation. It cannot mutate files, invoke providers, approve requests, issue contracts, run shell commands, or perform Git operations. `orchestrator.py` remains the sole execution engine. The transaction coordinator remains an atomic mutation primitive called by the orchestrator.

## Identity and approval boundary

Remote work records the channel, authenticated actor, session ID, and session incarnation. These values come from the channel adapter and session manager, not from user-controlled message metadata. Approval consumption verifies the request digest and the same identity tuple. A consumed approval is replay-resistant.

Every remote code or file write requires approval and a valid `ApprovedExecutionContract`, including deployments whose custom channel policy otherwise permits code edits. The contract binds the approved operation, workspace, targets, actor, channel, session, incarnation, expiry, and capabilities. Generated output cannot expand the approved target set.

Conversation history may help classify a request before approval, but execution uses the approved canonical operation and the original correlated remote intent. Prior messages cannot add targets or capabilities after approval.

## Transaction and verification behavior

System 2 returns proposed file contents to the orchestrator. The orchestrator creates a transaction before touching the workspace, stages each bounded mutation, captures its pre-state, persists the journal, and executes it. The configured `TransactionVerifier` runs before commit. The default filesystem verifier checks the expected post-state for every staged operation.

A verification or execution failure triggers reverse-order rollback. The remote Work becomes `FAILED`; it becomes `DONE` only after the transaction reaches `COMMITTED`. Startup recovery scans durable transaction journals, rolls back incomplete work when it can do so unambiguously, and reconciles linked nonterminal Work records to `FAILED`. See [Transactional Execution Engine](transaction-engine.md) and [Transaction Recovery](transaction-recovery.md).

## Result correlation

Remote responses expose stable correlation fields in metadata:

- `work_id`
- `plan_id`
- `approval_request_id`
- `transaction_id` once execution begins
- lifecycle `status`
- scrubbed failure text when execution fails

These fields let a channel adapter show pending, approved, completed, rolled-back, or failed outcomes without receiving authority to perform the work itself.

## Security and resource limits

- Only Telegram and WhatsApp enter the remote coding coordinator.
- Empty, oversized, ambiguous, absolute, traversing, or secret-bearing intents fail closed.
- Filesystem scope is workspace-relative and contract-checked again while staging.
- Transaction operation count, file snapshot size, serialized journal size, and startup recovery count are bounded.
- Remote Git push remains disabled. A successful transaction changes the working tree only; it does not commit or push.
- Secrets are scrubbed from persisted metadata, diagnostics, and remote failure messages.

## Validation

`tests/test_remote_autonomous_coding.py` exercises both remote channels, the real approval-to-contract path, Work/Plan transitions, transactional commit, verification rollback, target expansion rejection, history isolation, cancellation, replay and cross-channel rejection, input bounds, Git isolation, and startup recovery reconciliation. External model providers are mocked; the internal runtime, approval, contract, orchestrator, and transaction components are real.
