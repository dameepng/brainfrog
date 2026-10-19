"""Canonical approval -> contract -> execution helpers for Phase 1.6 tests.

Rationale (R1 remediation):
    Build-mode execution without an ApprovedExecutionContract fails closed at the
    orchestrator boundary. Tests that legitimately exercise the execution pipeline
    must authorize the exact canonical operation first:

        1. request  -> the runtime queues an approval request (pending_approval)
        2. approve  -> a distinct operator identity approves it (two-man rule)
        3. execute  -> the operation is resubmitted bound to the approval id;
                       the runtime consumes the approval and mints the contract

    These helpers drive that dance through public entry points only
    (runtime.handle_message / execute_autonomous_task, SDK Client), never by
    minting contracts directly.
"""
from __future__ import annotations

import dataclasses
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.runtime import BrainFrogRuntime, IncomingMessage, TaskRequest, TaskResult
from core.runtime.approval import ApprovalRequest, ApprovalStatus

# Distinct local operator identity. Must differ from the requester (two-man rule).
APPROVER_ID = "operator"
# Default local requester identity recognized by the cli channel anti-spoofing gate.
REQUESTER_ID = "local"


def approval_metadata(
    *,
    target: Optional[str] = None,
    targets: Optional[Sequence[str]] = None,
    test_command: Optional[Sequence[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Metadata that pins the canonical operation and demands approval."""
    meta: Dict[str, Any] = {"require_approval": True, "requested_action": "write_code"}
    if targets:
        meta["targets"] = list(targets)
        meta["target"] = target or list(targets)[0]
    elif target:
        meta["target"] = target
    if test_command:
        meta["test_cmd"] = list(test_command)
    if extra:
        meta.update(extra)
    return meta


def _operation_targets(req: ApprovalRequest) -> set:
    op = req.canonical_operation
    targets = {op.target} if op.target else set()
    params = getattr(op, "parameters", None) or {}
    extra = params.get("targets") if isinstance(params, dict) else None
    if isinstance(extra, (list, tuple, set)):
        targets |= {str(t) for t in extra}
    return targets


def find_pending_request(
    runtime: BrainFrogRuntime,
    *,
    target: Optional[str] = None,
    targets: Optional[Sequence[str]] = None,
    requester: Optional[str] = None,
    channel: Optional[str] = None,
) -> Optional[ApprovalRequest]:
    """Return the newest active pending request matching the given operation scope."""
    wanted = set()
    if target:
        wanted.add(target)
    if targets:
        wanted |= {str(t) for t in targets}

    candidates: List[ApprovalRequest] = []
    for req in runtime.approval_service.store.list_requests(active_only=True):
        status = str(getattr(req.status, "value", req.status)).lower()
        if status != "pending" or req.is_expired():
            continue
        if requester is not None and req.user_id != requester:
            continue
        if channel is not None and req.channel.lower() != channel.lower():
            continue
        if wanted and not (_operation_targets(req) & wanted):
            continue
        candidates.append(req)
    if not candidates:
        return None
    return max(candidates, key=lambda r: getattr(r, "created_at", 0.0))


def approve_request(
    runtime: BrainFrogRuntime,
    request_id: str,
    *,
    channel: str = "cli",
    session_id: Optional[str] = None,
    approver: str = APPROVER_ID,
) -> ApprovalRequest:
    """Approve a pending request via the canonical approval service."""
    ok, msg, req = runtime.approval_service.approve(
        request_id=request_id,
        approver_id=approver,
        channel=channel,
        session_id=session_id,
    )
    if not ok or req is None:
        raise AssertionError(f"Approval transition failed for {request_id!r}: {msg}")
    return req


def request_approval(
    runtime: BrainFrogRuntime,
    *,
    text: str,
    target: Optional[str] = None,
    targets: Optional[Sequence[str]] = None,
    channel: str = "cli",
    requester: str = REQUESTER_ID,
    conversation_id: str = "default",
    workspace: Optional[Any] = None,
    test_command: Optional[Sequence[str]] = None,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, str]:
    """Step 1 for handle_message-driven tests.

    Returns (OutgoingMessage, request_id); raises if the runtime did not queue an
    approval request.
    """
    meta = dict(extra_meta or {})
    meta.update(approval_metadata(target=target, targets=targets, test_command=test_command))
    meta.setdefault("user_id", requester)
    if workspace is not None:
        meta.setdefault("repo_dir", str(workspace))

    resp = runtime.handle_message(
        IncomingMessage(
            id=f"approval_req_{time.time_ns()}",
            channel=channel,
            user_id=requester,
            conversation_id=conversation_id,
            text=text,
            metadata=meta,
        )
    )
    request_id = (resp.metadata or {}).get("request_id")
    if resp.status != "pending_approval" or not request_id:
        raise AssertionError(
            f"Expected pending_approval with request_id, got status={resp.status!r} "
            f"text={resp.text!r} error={resp.error!r}"
        )
    return resp, str(request_id)


def execute_request(
    runtime: BrainFrogRuntime,
    request_id: str,
    *,
    channel: str = "cli",
    requester: str = REQUESTER_ID,
    conversation_id: str = "default",
) -> Any:
    """Step 3 variant for handle_message tests: run the approved request via /exec."""
    return runtime.handle_message(
        IncomingMessage(
            id=f"exec_{request_id}",
            channel=channel,
            user_id=requester,
            conversation_id=conversation_id,
            text=f"/exec {request_id}",
        )
    )


def run_approved_task(
    runtime: BrainFrogRuntime,
    request: TaskRequest,
    *,
    target: Optional[str] = None,
    targets: Optional[Sequence[str]] = None,
    test_command: Optional[Sequence[str]] = None,
    requester: str = REQUESTER_ID,
    approver: str = APPROVER_ID,
) -> TaskResult:
    """Full 3-step dance through execute_autonomous_task.

    Step 1 queues the approval request (TaskResult REJECTED), the operator approves
    it, and step 3 resubmits the identical task bound to the approval id. Returns
    the FINAL execution TaskResult.
    """
    meta1 = dict(request.metadata or {})
    meta1.update(approval_metadata(target=target, targets=targets, test_command=test_command))
    meta1.setdefault("user_id", requester)

    first = runtime.execute_autonomous_task(dataclasses.replace(request, metadata=meta1))
    if first.status != "REJECTED":
        raise AssertionError(
            f"Step 1 should be gated on approval, got status={first.status!r} "
            f"summary={first.summary!r}"
        )

    pending = find_pending_request(
        runtime,
        target=target,
        targets=targets,
        requester=meta1["user_id"],
        channel=request.channel,
    )
    if pending is None:
        raise AssertionError("No pending approval request found after step 1")

    approve_request(
        runtime,
        pending.request_id,
        channel=request.channel,
        session_id=pending.session_id,
        approver=approver,
    )

    meta3 = dict(meta1)
    meta3["approval_id"] = pending.request_id
    return runtime.execute_autonomous_task(dataclasses.replace(request, metadata=meta3))


async def run_sdk_with_approval(
    client: Any,
    task: str,
    *,
    task_id: str,
    target: Optional[str] = None,
    targets: Optional[Sequence[str]] = None,
    test_command: Optional[Sequence[str]] = None,
    allowed_scope: Optional[Sequence[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    on_event: Optional[Any] = None,
    approver: str = APPROVER_ID,
    raise_on_failure: bool = False,
) -> TaskResult:
    """Full approval dance through the async SDK client surface."""
    meta1 = dict(metadata or {})
    meta1.update(approval_metadata(target=target, targets=targets, test_command=test_command))

    first = await client.run(
        task, task_id=task_id, allowed_scope=allowed_scope,
        metadata=meta1, on_event=on_event, raise_on_failure=False,
    )
    if first.status != "REJECTED":
        raise AssertionError(
            f"Step 1 should be gated on approval, got status={first.status!r} "
            f"summary={first.summary!r}"
        )

    request_id = _sdk_pending_id(client, target=target, targets=targets)
    client.approve(request_id, approver=approver)

    meta3 = dict(meta1)
    meta3["approval_id"] = request_id
    return await client.run(
        task, task_id=task_id, allowed_scope=allowed_scope,
        metadata=meta3, on_event=on_event, raise_on_failure=raise_on_failure,
    )


def run_sdk_sync_with_approval(
    client: Any,
    task: str,
    *,
    task_id: str,
    target: Optional[str] = None,
    targets: Optional[Sequence[str]] = None,
    test_command: Optional[Sequence[str]] = None,
    allowed_scope: Optional[Sequence[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    on_event: Optional[Any] = None,
    approver: str = APPROVER_ID,
    raise_on_failure: bool = False,
) -> TaskResult:
    """Full approval dance through the synchronous SDK client surface."""
    meta1 = dict(metadata or {})
    meta1.update(approval_metadata(target=target, targets=targets, test_command=test_command))

    first = client.run_sync(
        task, task_id=task_id, allowed_scope=allowed_scope,
        metadata=meta1, on_event=on_event, raise_on_failure=False,
    )
    if first.status != "REJECTED":
        raise AssertionError(
            f"Step 1 should be gated on approval, got status={first.status!r} "
            f"summary={first.summary!r}"
        )

    request_id = _sdk_pending_id(client, target=target, targets=targets)
    client.approve(request_id, approver=approver)

    meta3 = dict(meta1)
    meta3["approval_id"] = request_id
    return client.run_sync(
        task, task_id=task_id, allowed_scope=allowed_scope,
        metadata=meta3, on_event=on_event, raise_on_failure=raise_on_failure,
    )


def _prepare_meta(
    meta: Optional[Dict[str, Any]],
    target: Optional[str],
    targets: Optional[Sequence[str]],
    test_command: Optional[Sequence[str]],
) -> Dict[str, Any]:
    prepared = dict(meta or {})
    prepared.update(approval_metadata(target=target, targets=targets, test_command=test_command))
    return prepared


async def prepare_sdk_approval(
    client: Any,
    task: str,
    *,
    task_id: str,
    target: Optional[str] = None,
    targets: Optional[Sequence[str]] = None,
    test_command: Optional[Sequence[str]] = None,
    allowed_scope: Optional[Sequence[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Step 1 + approve for async clients; returns final-run metadata bound to the approval.

    Use for flows that need control over the approved execution itself
    (e.g. client.start() + TaskHandle cancellation).
    """
    meta = _prepare_meta(metadata, target, targets, test_command)
    first = await client.run(
        task, task_id=task_id, allowed_scope=allowed_scope,
        metadata=meta, raise_on_failure=False,
    )
    if first.status != "REJECTED":
        raise AssertionError(
            f"Step 1 should be gated on approval, got status={first.status!r} "
            f"summary={first.summary!r}"
        )
    request_id = _sdk_pending_id(client, target=target, targets=targets)
    client.approve(request_id, approver=APPROVER_ID)
    meta["approval_id"] = request_id
    return meta


def prepare_sdk_sync_approval(
    client: Any,
    task: str,
    *,
    task_id: str,
    target: Optional[str] = None,
    targets: Optional[Sequence[str]] = None,
    test_command: Optional[Sequence[str]] = None,
    allowed_scope: Optional[Sequence[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Step 1 + approve for sync clients; returns final-run metadata bound to the approval."""
    meta = _prepare_meta(metadata, target, targets, test_command)
    first = client.run_sync(
        task, task_id=task_id, allowed_scope=allowed_scope,
        metadata=meta, raise_on_failure=False,
    )
    if first.status != "REJECTED":
        raise AssertionError(
            f"Step 1 should be gated on approval, got status={first.status!r} "
            f"summary={first.summary!r}"
        )
    request_id = _sdk_pending_id(client, target=target, targets=targets)
    client.approve(request_id, approver=APPROVER_ID)
    meta["approval_id"] = request_id
    return meta


def _sdk_pending_id(
    client: Any,
    *,
    target: Optional[str] = None,
    targets: Optional[Sequence[str]] = None,
) -> str:
    """Return the newest pending SDK approval id matching the operation scope.

    Primary discovery uses the public SDK surface (canonical-session filtered).
    Clients whose conversation_id differs from the request session fall back to
    the runtime approval store, still filtered by the canonical operation scope.
    """
    wanted = set()
    if target:
        wanted.add(target)
    if targets:
        wanted |= {str(t) for t in targets}

    pool = list(client.list_pending_approvals())
    if not pool:
        store = client.runtime.approval_service.store
        pool = [
            req for req in store.list_requests(active_only=True)
            if str(getattr(req.status, "value", req.status)).lower() == "pending"
            and not req.is_expired()
        ]

    candidates = []
    for req in pool:
        if wanted and not (_operation_targets(req) & wanted):
            continue
        candidates.append(req)
    if not candidates:
        raise AssertionError("No pending SDK approval request found after step 1")
    newest = max(candidates, key=lambda r: getattr(r, "created_at", 0.0))
    return newest.request_id
