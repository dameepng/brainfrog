"""BrainFrog Phase 3 — Verification-First Autonomous Coding Workflow.

Defines the core task execution contract, planning representations,
verification gates, and autonomous workflow execution pipeline:

    TaskRequest -> TaskPlan -> TaskStep -> Execution -> VerificationGate -> TaskResult

Product Thesis:
    "Don't just trust the agent. Verify it."
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from core.runtime.messages import IncomingMessage, OutgoingMessage
from core.runtime.permissions import ChannelTrustLevel, PermissionAction
from core.runtime.process import HardenedProcessResult
from core.runtime.workspace_lock import is_pid_running
from core.runtime.transaction import (
    FileTransactionStore,
    TransactionCoordinator,
    TransactionResult,
    TransactionStatus,
)
from core.runtime.proof import (
    ChangeRecord,
    ChangesEvidence,
    FileProofStore,
    GitProvenance,
    ProcessEvidence,
    ProofAlreadyExistsError,
    ProofArtifact,
    ProofVerdict,
    ReproducibilityEvidence,
    TaskEvidence,
    VerificationEvidence,
    build_change_records,
    capture_bounded_output,
    capture_git_provenance,
    compute_proof_verdict,
    normalize_workspace_relative_path,
    redact_secrets,
    sample_git_state,
)
from core.runtime.proof_markdown import render_proof_markdown


@dataclass(frozen=True)
class TaskStep:
    """Minimal structured representation of a single planned step."""

    id: str
    description: str
    scope: Sequence[str] = field(default_factory=tuple)  # Allowed files or directory areas
    risk: str = "medium"
    expected_change: str = ""
    verification: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("TaskStep id must not be empty.")
        if not self.description:
            raise ValueError("TaskStep description must not be empty.")


@dataclass(frozen=True)
class TaskPlan:
    """Explicit, pre-execution plan constraining autonomous mutation."""

    goal: str
    steps: Sequence[TaskStep]
    risk: str = "medium"
    files_or_areas: Sequence[str] = field(default_factory=tuple)
    verification_commands: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.goal:
            raise ValueError("TaskPlan goal must not be empty.")
        if not self.steps:
            raise ValueError("TaskPlan must contain at least one step.")


@dataclass(frozen=True)
class TaskRequest:
    """Structured request for an autonomous coding task."""

    task_id: str
    user_request: str
    workspace: Path
    channel: str = "cli"
    session_id: Optional[str] = None
    allowed_scope: Optional[Sequence[str]] = None
    test_command: Optional[Sequence[str]] = None
    max_retries: int = 2
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("TaskRequest task_id must not be empty.")
        if not self.user_request:
            raise ValueError("TaskRequest user_request must not be empty.")
        if not isinstance(self.workspace, Path):
            object.__setattr__(self, "workspace", Path(self.workspace))


@dataclass
class TaskResult:
    """Structured, verifiable result of an autonomous coding execution.

    Distinguishes 'success' and 'verified'.
    Strict Invariant: success=True requires verified=True.
    """

    task_id: str
    user_request: str
    success: bool
    verified: bool
    status: str  # "COMMITTED", "ROLLED_BACK", "CANCELLED", "FAILED", "REJECTED", "LOCKED"
    summary: str
    plan: Optional[TaskPlan] = None
    tests_passed: bool = False
    test_output: str = ""
    changed_files: List[str] = field(default_factory=list)
    transaction_id: Optional[str] = None
    transaction_status: Optional[str] = None
    failure_reason: Optional[str] = None
    recovery_required: bool = False
    verification_details: Dict[str, Any] = field(default_factory=dict)
    proof_path: Optional[str] = None
    proof_markdown_path: Optional[str] = None
    proof_artifact: Optional[Any] = None

    def __post_init__(self) -> None:
        # Core Invariant: An autonomous task can NEVER claim success without verification
        if self.success and not self.verified:
            raise ValueError(
                "Invariant Violation: Autonomous coding task cannot report success=True when verified=False."
            )

    def format_report(self) -> str:
        """Produce clean, structured observability output."""
        lines: List[str] = [
            f"Task: {self.user_request}",
            "",
            f"Status: {'VERIFIED' if self.verified else self.status}",
            "",
        ]

        if self.plan and self.plan.steps:
            lines.append("Plan:")
            for s in self.plan.steps:
                lines.append(f"{s.id}. {s.description}")
            lines.append("")

        if self.changed_files:
            lines.append("Changed:")
            for cf in self.changed_files:
                lines.append(f"- {cf}")
            lines.append("")
        elif self.status == "COMMITTED":
            lines.append("Changed:\n(none)\n")

        lines.append("Tests:")
        if self.tests_passed:
            lines.append("✓ passed")
        else:
            lines.append("✗ failed or unexecuted")
        lines.append("")

        lines.append("Verification:")
        v_details = self.verification_details or {}
        scope_ok = v_details.get("workspace_scope", self.verified)
        tx_ok = v_details.get("transaction_integrity", self.verified or self.status == "ROLLED_BACK")
        tests_ok = v_details.get("test_suite", self.tests_passed)
        lines.append(f"{'✓' if scope_ok else '✗'} workspace scope")
        lines.append(f"{'✓' if tx_ok else '✗'} transaction integrity")
        lines.append(f"{'✓' if tests_ok else '✗'} test suite")
        lines.append("")

        lines.append(f"Transaction:\n{self.transaction_status or self.status}")

        if self.failure_reason:
            lines.append(f"\nFailure Reason:\n{self.failure_reason}")

        if self.recovery_required:
            lines.append("\n⚠️ RECOVERY REQUIRED: Manual inspection recommended.")

        if self.proof_path:
            lines.append(f"\nProof Artifact: {self.proof_path}")

        return "\n".join(lines)


class VerificationGate:
    """First-class verification gate enforcing post-execution trust criteria.

    Evaluates:
    A. Mutation integrity: only expected/planned workspace changes exist.
    B. Tests: relevant checks pass exit 0 without timeout.
    C. Transaction integrity: correct terminal state (COMMITTED or ROLLED_BACK).
    D. Execution integrity: zero lingering/hung subprocesses.
    E. Trust integrity: execution respected channel and capability boundaries.
    """

    @staticmethod
    def verify(
        workspace: Path,
        expected_files: Sequence[str],
        actual_files: Sequence[str],
        test_proc: Optional[HardenedProcessResult],
        tx_res: Optional[TransactionResult],
        tracked_pids: Optional[Sequence[int]] = None,
        channel: str = "cli",
    ) -> Tuple[bool, Dict[str, bool], List[str]]:
        """Evaluate all verification criteria and return (verified, details, errors)."""
        errors: List[str] = []
        details: Dict[str, bool] = {
            "workspace_scope": True,
            "test_suite": True,
            "transaction_integrity": True,
            "execution_integrity": True,
            "trust_integrity": True,
        }

        # A. Mutation Integrity: check for out-of-scope files
        ws_res = workspace.resolve()
        if expected_files:
            allowed_norm = {
                Path(f).as_posix().lstrip("./") for f in expected_files
            }
            for act in actual_files:
                act_norm = Path(act).as_posix().lstrip("./")
                # Allowed if matches an allowed file or is within an allowed directory
                is_allowed = (
                    act_norm in allowed_norm
                    or any(act_norm.startswith(f"{prefix}/") for prefix in allowed_norm)
                )
                if not is_allowed:
                    details["workspace_scope"] = False
                    errors.append(f"Mutation integrity violated: unexpected file '{act}' touched.")

        # B. Tests: must exist, have valid non-empty command, returncode 0, no timeout
        if test_proc is not None:
            cmd_args = getattr(test_proc, "args", None)
            is_empty_cmd = (
                cmd_args is None
                or (isinstance(cmd_args, (list, tuple)) and not cmd_args)
                or (isinstance(cmd_args, str) and not cmd_args.strip())
            )
            if is_empty_cmd:
                details["test_suite"] = False
                errors.append("Test suite command was empty; verification requires a real test process.")
            elif test_proc.returncode != 0:
                details["test_suite"] = False
                errors.append(f"Domain test suite failed with exit code {test_proc.returncode}.")
            if test_proc.is_timeout:
                details["test_suite"] = False
                errors.append("Domain test suite timed out.")
        else:
            # If tests were not provided or not run
            details["test_suite"] = False
            errors.append("No test result provided for verification.")

        # C. Transaction Integrity: must be committed cleanly
        if tx_res is not None:
            if tx_res.status != TransactionStatus.COMMITTED or not tx_res.success:
                details["transaction_integrity"] = False
                errors.append(
                    f"Transaction integrity violated: transaction in state '{tx_res.status.value}' (success={tx_res.success})."
                )
        else:
            details["transaction_integrity"] = False
            errors.append("No transaction result available.")

        # D. Execution Integrity: verify no tracked PIDs are still running
        if tracked_pids:
            lingering = [pid for pid in tracked_pids if is_pid_running(pid)]
            if lingering:
                details["execution_integrity"] = False
                errors.append(f"Execution integrity violated: lingering processes detected: {lingering}.")

        # E. Trust Integrity: CLI is local, remote channels cannot execute arbitrary code
        if channel not in ("cli", "local", "terminal"):
            details["trust_integrity"] = False
            errors.append(f"Trust integrity violated: channel '{channel}' is not trusted for autonomous execution.")

        all_passed = all(details.values()) and len(errors) == 0
        return all_passed, details, errors


def execute_autonomous_task(
    runtime: Any,
    request: TaskRequest,
    event_listener: Optional[Any] = None,
) -> TaskResult:
    """Execute an autonomous task end-to-end through the canonical runtime boundary.

    Workflow:
        TaskRequest -> IncomingMessage -> BrainFrogRuntime -> Trust Policy ->
        Plan -> Transaction -> Subprocess Tests -> VerificationGate -> TaskResult
    """
    workspace = request.workspace.resolve()
    if not workspace.exists() or not workspace.is_dir():
        return TaskResult(
            task_id=request.task_id,
            user_request=request.user_request,
            success=False,
            verified=False,
            status="FAILED",
            summary=f"Workspace does not exist: {workspace}",
            failure_reason="Workspace directory not found",
        )

    t0_start = time.time()
    created_at_iso = datetime.now(timezone.utc).isoformat()

    active_listener = event_listener or (request.metadata.get("event_listener") if request.metadata else None)
    session_id_str = request.session_id or f"session_{request.task_id}"

    def emit_event(
        event_type: str,
        *,
        transaction_id: Optional[str] = None,
        process_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not active_listener:
            return
        try:
            from brainfrog.sdk.events import EventType, create_sdk_event
            ev_type = EventType(event_type) if isinstance(event_type, str) else event_type
            ev = create_sdk_event(
                event_type=ev_type,
                task_id=request.task_id,
                session_id=session_id_str,
                transaction_id=transaction_id,
                process_id=process_id,
                payload=payload or {},
            )
            active_listener(ev)
        except Exception:
            pass

    emit_event("TASK_CREATED", payload={"task": request.user_request})

    proof_store = FileProofStore(workspace)
    if proof_store.exists(request.task_id):
        emit_event("TASK_FAILED", payload={"status": "REJECTED", "reason": f"Task '{request.task_id}' has already been executed with an authoritative proof."})
        return TaskResult(
            task_id=request.task_id,
            user_request=request.user_request,
            success=False,
            verified=False,
            status="REJECTED",
            summary=f"Task '{request.task_id}' has already been executed with an authoritative proof. Duplicate task submissions are rejected to prevent proof tampering.",
            failure_reason="Duplicate task ID already proven",
        )

    from core.runtime.process import _CURRENT_TASK_ID, _CURRENT_CANCEL_EVENT
    cancel_ev = request.metadata.get("cancel_event") if request.metadata else None
    if cancel_ev and cancel_ev.is_set():
        emit_event("TASK_CANCELLED", payload={"status": "CANCELLED", "reason": "Task cancelled before execution started."})
        return TaskResult(
            task_id=request.task_id,
            user_request=request.user_request,
            success=False,
            verified=False,
            status="CANCELLED",
            summary="⚠️ Task cancelled before execution.",
            failure_reason="Execution cancelled by cancellation token",
        )

    token_task = _CURRENT_TASK_ID.set(request.task_id)
    token_cancel = _CURRENT_CANCEL_EVENT.set(cancel_ev)

    # 1. Enter via canonical Phase 1 message facade
    msg_metadata: Dict[str, Any] = {
        "repo_dir": str(workspace),
        "channel": request.channel,
        "max_retries": request.max_retries,
        "catch_cancellation": True,
        "task_id": request.task_id,
    }
    if request.allowed_scope:
        msg_metadata["allowed_scope"] = list(request.allowed_scope)
    if request.test_command:
        msg_metadata["test_cmd"] = list(request.test_command)
    if request.metadata:
        msg_metadata.update(request.metadata)

    # Intercept log_fn to capture step lifecycle
    orig_log_fn = msg_metadata.get("log_fn")
    def sdk_log_fn(msg: str) -> None:
        if cancel_ev and cancel_ev.is_set():
            raise KeyboardInterrupt("Execution cancelled by cancellation token")
        clean = msg.strip()
        if clean.startswith("=== Step") and clean.endswith("==="):
            step_name = clean.strip("= ").strip()
            emit_event("STEP_STARTED", payload={"step": step_name})
        if orig_log_fn and callable(orig_log_fn):
            try:
                orig_log_fn(msg)
            except Exception:
                pass
    msg_metadata["log_fn"] = sdk_log_fn

    incoming = IncomingMessage(
        id=request.task_id,
        channel=request.channel,
        user_id=request.metadata.get("user_id", "local"),
        conversation_id=session_id_str,
        text=request.user_request,
        metadata=msg_metadata,
    )

    init_head_sha, init_was_dirty = sample_git_state(workspace)

    try:
        return _do_execute_autonomous_task(
            runtime=runtime,
            request=request,
            workspace=workspace,
            incoming=incoming,
            t0_start=t0_start,
            created_at_iso=created_at_iso,
            session_id_str=session_id_str,
            emit_event=emit_event,
            init_head_sha=init_head_sha,
            init_was_dirty=init_was_dirty,
        )
    finally:
        _CURRENT_TASK_ID.reset(token_task)
        _CURRENT_CANCEL_EVENT.reset(token_cancel)


def _do_execute_autonomous_task(
    runtime: Any,
    request: TaskRequest,
    workspace: Path,
    incoming: IncomingMessage,
    t0_start: float,
    created_at_iso: str,
    session_id_str: str,
    emit_event: Callable[..., None],
    init_head_sha: Optional[str] = None,
    init_was_dirty: bool = False,
) -> TaskResult:

    # 2. Execute via BrainFrogRuntime
    cancel_ev = request.metadata.get("cancel_event") if request.metadata else None
    if cancel_ev and cancel_ev.is_set():
        raise KeyboardInterrupt("Execution cancelled by cancellation token")

    try:
        outgoing: OutgoingMessage = runtime.handle_message(incoming)
    except KeyboardInterrupt:
        t1_end = time.time()
        completed_at_iso = datetime.now(timezone.utc).isoformat()
        dur_ms = int((t1_end - t0_start) * 1000)
        c_verdict = ProofVerdict(status="CANCELLED", reason="Execution cancelled by KeyboardInterrupt")
        c_task = TaskEvidence(
            task_id=request.task_id,
            session_id=request.session_id or f"session_{request.task_id}",
            description=request.user_request,
            created_at=created_at_iso,
            completed_at=completed_at_iso,
            duration_ms=dur_ms,
        )
        c_proof = ProofArtifact(
            task=c_task,
            verdict=c_verdict,
            provenance=capture_git_provenance(
                workspace,
                initial_head_sha=init_head_sha,
                was_dirty_before=init_was_dirty,
            ),
            verification=VerificationEvidence(
                test_command=" ".join(request.test_command) if request.test_command else "",
                exit_code=None,
                stdout_summary="",
                stderr_summary="",
                gate_checks={"execution_integrity": False},
            ),
            changes=ChangesEvidence(transaction_id=None, status="ROLLED_BACK", files=[]),
            reproducibility=ReproducibilityEvidence(
                command=" ".join(request.test_command) if request.test_command else ""
            ),
        )
        proof_path = None
        proof_md = None
        try:
            store = FileProofStore(workspace)
            pj, pm = store.save(c_proof, render_proof_markdown(c_proof))
            proof_path = str(pj)
            proof_md = str(pm) if pm else None
        except Exception:
            pass
        emit_event("TASK_CANCELLED", payload={"status": "CANCELLED", "reason": "Execution cancelled by KeyboardInterrupt"})
        return TaskResult(
            task_id=request.task_id,
            user_request=request.user_request,
            success=False,
            verified=False,
            status="CANCELLED",
            summary="⚠️ Task cancelled by user.",
            failure_reason="Execution cancelled by KeyboardInterrupt",
            proof_path=proof_path,
            proof_markdown_path=proof_md,
            proof_artifact=c_proof,
        )

    # 3. Handle early policy / lock rejections
    if outgoing.status in ("rejected", "permission_denied", "pending_approval"):
        fail_r = outgoing.error or "Action denied by trust policy"
        emit_event("TASK_FAILED", payload={"status": "REJECTED", "reason": fail_r})
        return TaskResult(
            task_id=request.task_id,
            user_request=request.user_request,
            success=False,
            verified=False,
            status="REJECTED",
            summary=outgoing.text,
            failure_reason=fail_r,
        )
    if outgoing.status == "locked":
        fail_l = outgoing.error or "Workspace locked"
        emit_event("TASK_FAILED", payload={"status": "LOCKED", "reason": fail_l})
        return TaskResult(
            task_id=request.task_id,
            user_request=request.user_request,
            success=False,
            verified=False,
            status="LOCKED",
            summary=outgoing.text,
            failure_reason=fail_l,
        )
    if outgoing.status == "cancelled":
        t1_end = time.time()
        completed_at_iso = datetime.now(timezone.utc).isoformat()
        dur_ms = int((t1_end - t0_start) * 1000)
        c_verdict = ProofVerdict(status="CANCELLED", reason="Execution cancelled by user/runtime")
        c_task = TaskEvidence(
            task_id=request.task_id,
            session_id=request.session_id or f"session_{request.task_id}",
            description=request.user_request,
            created_at=created_at_iso,
            completed_at=completed_at_iso,
            duration_ms=dur_ms,
        )
        c_proof = ProofArtifact(
            task=c_task,
            verdict=c_verdict,
            provenance=capture_git_provenance(
                workspace,
                initial_head_sha=init_head_sha,
                was_dirty_before=init_was_dirty,
            ),
            verification=VerificationEvidence(
                test_command=" ".join(request.test_command) if request.test_command else "",
                exit_code=None,
                stdout_summary="",
                stderr_summary="",
                gate_checks={"execution_integrity": False},
            ),
            changes=ChangesEvidence(transaction_id=None, status="ROLLED_BACK", files=[]),
            reproducibility=ReproducibilityEvidence(
                command=" ".join(request.test_command) if request.test_command else ""
            ),
        )
        proof_path = None
        proof_md = None
        try:
            store = FileProofStore(workspace)
            pj, pm = store.save(c_proof, render_proof_markdown(c_proof))
            proof_path = str(pj)
            proof_md = str(pm) if pm else None
        except Exception:
            pass
        emit_event("TASK_CANCELLED", payload={"status": "CANCELLED", "reason": "Execution cancelled by user/runtime"})
        return TaskResult(
            task_id=request.task_id,
            user_request=request.user_request,
            success=False,
            verified=False,
            status="CANCELLED",
            summary=outgoing.text,
            failure_reason="Execution cancelled by user/runtime",
            proof_path=proof_path,
            proof_markdown_path=proof_md,
            proof_artifact=c_proof,
        )

    # 4. Extract execution metadata
    t1_end = time.time()
    completed_at_iso = datetime.now(timezone.utc).isoformat()
    duration_ms = int((t1_end - t0_start) * 1000)

    out_meta = outgoing.metadata or {}
    results = out_meta.get("results") or []
    tx_status = out_meta.get("transaction_status")
    tx_id = out_meta.get("transaction_id")
    tx_success = bool(out_meta.get("transaction_success", False))
    tx_res = out_meta.get("transaction_result")
    test_proc: Optional[HardenedProcessResult] = out_meta.get("test_result")

    # Construct TaskPlan from executed steps
    task_steps: List[TaskStep] = []
    changed_files: List[str] = []
    tests_passed = False
    test_output = ""

    for r in results:
        step_obj = getattr(r, "step", None)
        if step_obj:
            s_id = getattr(step_obj, "id", "1")
            s_desc = getattr(step_obj, "description", "")
            s_files = getattr(step_obj, "files", [])
            task_steps.append(TaskStep(id=str(s_id), description=s_desc, scope=s_files))
            if getattr(r, "outcome", None) in ("opened_pr", "drafted_pr", "verified"):
                changed_files.extend(s_files)

    plan = TaskPlan(
        goal=request.user_request,
        steps=task_steps if task_steps else [TaskStep(id="1", description="Execute task")],
        files_or_areas=list(request.allowed_scope or changed_files),
        verification_commands=list(request.test_command or []),
    )
    emit_event("PLAN_CREATED", transaction_id=tx_id, payload={"goal": plan.goal, "steps_count": len(plan.steps)})

    if tx_id:
        emit_event("TRANSACTION_STARTED", transaction_id=tx_id, payload={"transaction_id": tx_id, "status": tx_status})

    # Authoritative test pass evaluation:
    # A model outcome (such as opened_pr, drafted_pr, verified) has ZERO authority
    # to attest that tests passed. Tests only pass if an actual test process executed and exited 0.
    if test_proc is not None:
        cmd_args = getattr(test_proc, "args", None)
        is_empty_cmd = (
            cmd_args is None
            or (isinstance(cmd_args, (list, tuple)) and not cmd_args)
            or (isinstance(cmd_args, str) and not cmd_args.strip())
        )
        tests_passed = (test_proc.returncode == 0 and not is_empty_cmd)
        test_output = (test_proc.stdout or "") + (test_proc.stderr or "")
    else:
        tests_passed = False
        test_output = ""

    is_tx_committed = (tx_status == "committed" and tx_success)
    is_tx_rolled_back = (tx_status == "rolled_back")
    recovery_req = (tx_status == "failed" or (tx_res is not None and getattr(tx_res, "is_recovery_required", False)))

    # Fallback synthesize tx_res for transaction metadata if needed
    if tx_res is None and tx_status:
        tx_res = TransactionResult(
            transaction_id=tx_id or "tx_unknown",
            status=TransactionStatus(tx_status) if tx_status in [s.value for s in TransactionStatus] else TransactionStatus.COMMITTED,
            committed=is_tx_committed,
            rolled_back=is_tx_rolled_back,
            operations=tuple(),
        )

    if request.test_command:
        emit_event("TEST_STARTED", transaction_id=tx_id, payload={"command": " ".join(request.test_command)})
    if test_proc is not None:
        emit_event("TEST_COMPLETED", transaction_id=tx_id, payload={"returncode": test_proc.returncode})

    # 5. Evaluate Verification Gate
    emit_event("VERIFICATION_STARTED", transaction_id=tx_id)
    gate_passed, v_details, gate_errors = VerificationGate.verify(
        workspace=workspace,
        expected_files=list(request.allowed_scope or changed_files),
        actual_files=changed_files,
        test_proc=test_proc,
        tx_res=tx_res,
        tracked_pids=None,
        channel=request.channel,
    )
    emit_event("VERIFICATION_COMPLETED", transaction_id=tx_id, payload={"gate_passed": gate_passed, "details": v_details})

    # Authoritative Verdict Computation (Pure runtime logic)
    proc_stat = getattr(test_proc, "status", "success")
    rollback_err = getattr(tx_res, "rollback_error", None)
    proof_verdict = compute_proof_verdict(
        tx_status=tx_status or ("committed" if is_tx_committed else "rolled_back"),
        tests_passed=tests_passed,
        gate_passed=gate_passed and outgoing.success,
        process_status=proc_stat,
        rollback_error=rollback_err,
        is_recovery_required=recovery_req,
        gate_details=v_details,
    )

    # 6. Build Proof Artifact & Persist
    tx_ops = getattr(tx_res, "operations", []) if tx_res else []
    change_records = build_change_records(tx_ops, workspace)
    if not change_records and is_tx_committed and changed_files:
        for cf in changed_files:
            try:
                cf_rel = normalize_workspace_relative_path(cf, workspace)
                cf_disk = workspace / cf_rel
                cf_hash = hashlib.sha256(cf_disk.read_bytes()).hexdigest() if cf_disk.exists() else None
                change_records.append(
                    ChangeRecord(
                        path=cf_rel,
                        operation="MODIFY",
                        before_sha256=None,
                        after_sha256=cf_hash,
                        byte_count_delta=0,
                    )
                )
            except Exception:
                pass

    task_evidence = TaskEvidence(
        task_id=request.task_id,
        session_id=request.session_id or f"session_{request.task_id}",
        description=redact_secrets(request.user_request),
        created_at=created_at_iso,
        completed_at=completed_at_iso,
        duration_ms=duration_ms,
    )

    raw_stdout = getattr(test_proc, "stdout", "") or test_output or ""
    raw_stderr = getattr(test_proc, "stderr", "") or ""
    stdout_bounded, stdout_trunc = capture_bounded_output(raw_stdout)
    stderr_bounded, stderr_trunc = capture_bounded_output(raw_stderr)

    test_cmd_str = " ".join(request.test_command) if request.test_command else (
        getattr(test_proc, "args", "") if isinstance(getattr(test_proc, "args", None), str) else " ".join(getattr(test_proc, "args", []) or [])
    )

    proc_evidence = None
    if test_proc is not None:
        p_start_raw = getattr(test_proc, "started_at", None)
        p_comp_raw = getattr(test_proc, "completed_at", None)
        p_start_iso = datetime.fromtimestamp(p_start_raw, tz=timezone.utc).isoformat() if isinstance(p_start_raw, (int, float)) else ""
        p_comp_iso = datetime.fromtimestamp(p_comp_raw, tz=timezone.utc).isoformat() if isinstance(p_comp_raw, (int, float)) else ""
        p_stat_val = getattr(test_proc.status, "value", str(test_proc.status))
        proc_evidence = ProcessEvidence(
            task_id=request.task_id,
            transaction_id=tx_id,
            command=test_cmd_str,
            started_at=p_start_iso,
            completed_at=p_comp_iso,
            duration_ms=getattr(test_proc, "duration_ms", 0) or 0,
            returncode=test_proc.returncode,
            status=str(p_stat_val),
            timed_out=getattr(test_proc, "is_timeout", False),
            cancelled=getattr(test_proc, "is_cancelled", False),
            truncated=getattr(test_proc, "is_truncated", False) or stdout_trunc or stderr_trunc,
        )

    v_evidence = VerificationEvidence(
        test_command=test_cmd_str,
        exit_code=test_proc.returncode if test_proc else None,
        stdout_summary=stdout_bounded,
        stderr_summary=stderr_bounded,
        stdout_truncated=stdout_trunc,
        stderr_truncated=stderr_trunc,
        gate_checks=v_details,
        process_evidence=proc_evidence,
    )

    changes_evidence = ChangesEvidence(
        transaction_id=tx_id,
        status=(tx_status or ("COMMITTED" if is_tx_committed else ("ROLLED_BACK" if is_tx_rolled_back else "FAILED"))).upper(),
        files=change_records,
    )

    reproducibility = ReproducibilityEvidence(
        command=test_cmd_str,
        expected_exit_code=0,
        expected_file_hashes={r.path: r.after_sha256 for r in change_records if r.after_sha256},
    )

    from core.runtime.task_finalization import TaskFinalizationCoordinator
    intent_record = TaskFinalizationCoordinator(workspace).get_intent(request.task_id)
    commit_sha = intent_record.commit_sha if intent_record else None

    proof_artifact = ProofArtifact(
        version="1.0.0",
        task=task_evidence,
        verdict=proof_verdict,
        provenance=capture_git_provenance(
            workspace,
            initial_head_sha=init_head_sha,
            was_dirty_before=init_was_dirty,
            commit_sha=commit_sha,
        ),
        verification=v_evidence,
        changes=changes_evidence,
        reproducibility=reproducibility,
    )

    proof_path_str: Optional[str] = None
    proof_md_path_str: Optional[str] = None
    proof_store = FileProofStore(workspace)
    try:
        md_text = render_proof_markdown(proof_artifact)
        pj, pm = proof_store.save(proof_artifact, md_text)
        proof_path_str = str(pj)
        proof_md_path_str = str(pm) if pm else None
        emit_event("PROOF_CREATED", transaction_id=tx_id, payload={"proof_path": proof_path_str, "status": proof_verdict.status})

        # Durable finalization completed: clear unresolved commit intent
        from core.runtime.task_finalization import TaskFinalizationCoordinator
        TaskFinalizationCoordinator(workspace).finalize_proof(request.task_id)
    except Exception as store_exc:
        # Core Invariant: If proof cannot be durably persisted for an otherwise VERIFIED task, FAIL CLOSED:
        emit_event("VERIFICATION_FAILED", transaction_id=tx_id, payload={"error": f"Proof persistence failed: {store_exc}"})
        return TaskResult(
            task_id=request.task_id,
            user_request=request.user_request,
            success=False,
            verified=False,
            status="FAILED",
            summary=f"Task execution succeeded and committed, but proof persistence failed: {store_exc}. Recovery required.",
            failure_reason=f"Proof persistence error: {store_exc}",
            recovery_required=True,
            tests_passed=tests_passed,
            test_output=test_output,
            changed_files=list(dict.fromkeys(changed_files)),
            transaction_id=tx_id,
            transaction_status=tx_status,
            verification_details=v_details,
            proof_path=None,
            proof_markdown_path=None,
            proof_artifact=proof_artifact,
        )

    # 7. Finalize TaskResult & Apply Monotonic Cancellation Cutoff
    is_durable_verified = (proof_verdict.status == "VERIFIED" and proof_path_str is not None)

    if cancel_ev and cancel_ev.is_set():
        if is_durable_verified:
            # Irreversible cutoff crossed: task was already verified, committed, and proven durably.
            # Must not contradict the durable proof and committed repository state.
            cancellation_note = " (Cancellation request arrived after durable task finalization; commit and proof retained.)"
            emit_event("TASK_COMPLETED", transaction_id=tx_id, payload={"status": "COMMITTED", "verified": True, "cancellation_ignored_after_finalization": True})
            return TaskResult(
                task_id=request.task_id,
                user_request=request.user_request,
                success=True,
                verified=True,
                status="COMMITTED",
                summary=(outgoing.text or "") + cancellation_note,
                plan=plan,
                tests_passed=True,
                test_output=test_output,
                changed_files=list(dict.fromkeys(changed_files)),
                transaction_id=tx_id,
                transaction_status=tx_status,
                verification_details=v_details,
                proof_path=proof_path_str,
                proof_markdown_path=proof_md_path_str,
                proof_artifact=proof_artifact,
            )
        else:
            emit_event("TASK_CANCELLED", transaction_id=tx_id, payload={"status": "CANCELLED", "reason": "Task was cancelled"})
            return TaskResult(
                task_id=request.task_id,
                user_request=request.user_request,
                success=False,
                verified=False,
                status="CANCELLED",
                summary="⚠️ Task cancelled by user.",
                failure_reason="Execution cancelled by cancellation token",
                verification_details=v_details,
                proof_path=proof_path_str,
                proof_markdown_path=proof_md_path_str,
                proof_artifact=proof_artifact,
            )

    if proof_verdict.status == "VERIFIED":
        emit_event("TASK_COMPLETED", transaction_id=tx_id, payload={"status": "COMMITTED", "verified": True})
        return TaskResult(
            task_id=request.task_id,
            user_request=request.user_request,
            success=True,
            verified=True,
            status="COMMITTED",
            summary=outgoing.text,
            plan=plan,
            tests_passed=True,
            test_output=test_output,
            changed_files=list(dict.fromkeys(changed_files)),
            transaction_id=tx_id,
            transaction_status=tx_status,
            verification_details=v_details,
            proof_path=proof_path_str,
            proof_markdown_path=proof_md_path_str,
            proof_artifact=proof_artifact,
        )

    # Failed path: fail-closed
    is_recovery = (proof_verdict.status == "RECOVERY_REQUIRED" or recovery_req or (is_tx_committed and not tests_passed))
    final_status = (
        "ROLLED_BACK"
        if is_tx_rolled_back
        else ("RECOVERY_REQUIRED" if is_recovery else "FAILED")
    )
    fail_reason = outgoing.error or (
        "Execution recovery required."
        if is_recovery
        else (
            "Verification or finalization failed after transaction commit; changes retained, recovery required."
            if is_tx_committed
            else "Verification or tests failed; mutations rolled back."
        )
    )
    if is_recovery:
        emit_event("RECOVERY_REQUIRED", transaction_id=tx_id, payload={"status": final_status, "reason": fail_reason})
    else:
        emit_event("TASK_FAILED", transaction_id=tx_id, payload={"status": final_status, "reason": fail_reason})
    return TaskResult(
        task_id=request.task_id,
        user_request=request.user_request,
        success=False,
        verified=False,
        status=final_status,
        summary=outgoing.text,
        plan=plan,
        tests_passed=tests_passed,
        test_output=test_output,
        changed_files=[] if is_tx_rolled_back else list(dict.fromkeys(changed_files)),
        transaction_id=tx_id,
        transaction_status=tx_status,
        failure_reason=fail_reason,
        recovery_required=is_recovery,
        verification_details=v_details,
        proof_path=proof_path_str,
        proof_markdown_path=proof_md_path_str,
        proof_artifact=proof_artifact,
    )
