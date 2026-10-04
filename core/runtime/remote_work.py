"""Channel-agnostic coordination state for remote autonomous coding.

This module normalizes authenticated runtime input and coordinates descriptive
Work/Plan state. It has no execution, provider, approval, or filesystem APIs.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from core.runtime.capabilities import Capabilities
from core.runtime.contract import reject_secrets
from core.runtime.planning import DeterministicPlanner, Plan, Planner, apply_plan_to_work
from core.runtime.work import InMemoryWorkStore, Work, WorkStatus, WorkStore


MAX_REMOTE_INTENT_CHARS = 16_384
REMOTE_CHANNELS = frozenset({"telegram", "whatsapp"})


@dataclass(frozen=True)
class RemoteWorkRequest:
    request_id: str
    actor: str
    channel: str
    session_id: str
    session_incarnation_id: str
    intent: str
    workspace: str
    targets: Tuple[str, ...]

    def __post_init__(self) -> None:
        values = (self.request_id, self.actor, self.channel, self.session_id,
                  self.session_incarnation_id, self.intent, self.workspace)
        if any(type(v) is not str or not v.strip() for v in values):
            raise ValueError("Remote work identity and intent fields are required")
        if self.channel not in REMOTE_CHANNELS:
            raise ValueError("Remote work requires an authenticated remote channel")
        if len(self.intent) > MAX_REMOTE_INTENT_CHARS:
            raise ValueError("Remote work request exceeds maximum intent length")
        if type(self.targets) not in (tuple, list):
            raise ValueError("Remote work targets must be a bounded sequence")
        object.__setattr__(self, "targets", tuple(self.targets))
        if not self.targets or any(type(v) is not str or not v for v in self.targets):
            raise ValueError("Remote coding requires explicit validated targets")
        reject_secrets({"intent": self.intent, "targets": self.targets})


@dataclass(frozen=True)
class RemoteWorkResult:
    request_id: str
    work_id: str
    status: str
    message: str
    approval_required: bool = False
    approval_request_id: Optional[str] = None
    plan_id: Optional[str] = None
    transaction_id: Optional[str] = None
    error_code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "request_id": self.request_id, "work_id": self.work_id,
            "status": self.status, "message": self.message,
            "approval_required": self.approval_required,
            "approval_request_id": self.approval_request_id,
            "plan_id": self.plan_id, "transaction_id": self.transaction_id,
            "error_code": self.error_code,
        }
        reject_secrets(data)
        return data


@dataclass(frozen=True)
class RemoteWorkContext:
    request: RemoteWorkRequest
    work_id: str
    plan_id: str
    approval_request_id: Optional[str] = None
    transaction_id: Optional[str] = None


class RemoteWorkCoordinator:
    """Coordinates lifecycle records while delegating all authority elsewhere."""

    def __init__(self, work_store: Optional[WorkStore] = None,
                 planner: Optional[Planner] = None) -> None:
        self.work_store = work_store or InMemoryWorkStore()
        self.planner = planner or DeterministicPlanner()
        self._contexts: Dict[str, RemoteWorkContext] = {}
        self._approval_index: Dict[str, str] = {}

    def normalize(self, *, message_id: str, actor: str, channel: str,
                  session_id: str, session_incarnation_id: str, intent: str,
                  workspace: Path, targets: Tuple[str, ...]) -> RemoteWorkRequest:
        # Every identity value comes from the authenticated runtime/session, never metadata.
        request_id = message_id.strip() or f"remote_{secrets.token_hex(16)}"
        return RemoteWorkRequest(
            request_id=request_id, actor=actor.strip(), channel=channel.strip().lower(),
            session_id=session_id, session_incarnation_id=session_incarnation_id,
            intent=intent.strip(), workspace=str(workspace.resolve()), targets=targets,
        )

    def submit(self, request: RemoteWorkRequest, capabilities: Capabilities) -> Tuple[Work, Plan]:
        work = Work(intent=request.intent, goal=request.intent, scope=request.targets,
                    capabilities=capabilities)
        self.work_store.create(work)
        work = self.work_store.save(work.transition(WorkStatus.PLANNING))
        plan = self.planner.create_plan(work)
        work = self.work_store.save(apply_plan_to_work(work, plan))
        self._contexts[work.id] = RemoteWorkContext(request, work.id, plan.id)
        return work, plan

    def bind_approval(self, work_id: str, approval_request_id: str) -> RemoteWorkContext:
        context = self._contexts[work_id]
        context = RemoteWorkContext(context.request, context.work_id, context.plan_id,
                                    approval_request_id, context.transaction_id)
        self._contexts[work_id] = context
        self._approval_index[approval_request_id] = work_id
        return context

    def context_for_approval(self, approval_request_id: str) -> Optional[RemoteWorkContext]:
        work_id = self._approval_index.get(approval_request_id)
        return self._contexts.get(work_id) if work_id else None

    def transition_for_approval(self, approval_request_id: str,
                                status: WorkStatus) -> Optional[Work]:
        context = self.context_for_approval(approval_request_id)
        if context is None:
            return None
        work = self.work_store.get(context.work_id)
        if work is None or work.status == status:
            return work
        return self.work_store.save(work.transition(status))

    def bind_transaction(self, approval_request_id: str, transaction_id: str) -> None:
        context = self.context_for_approval(approval_request_id)
        if context is None:
            return
        self._contexts[context.work_id] = RemoteWorkContext(
            context.request, context.work_id, context.plan_id,
            context.approval_request_id, transaction_id,
        )

    def result(self, approval_request_id: str, status: str, message: str,
               *, error_code: Optional[str] = None) -> Optional[RemoteWorkResult]:
        context = self.context_for_approval(approval_request_id)
        if context is None:
            return None
        return RemoteWorkResult(
            request_id=context.request.request_id, work_id=context.work_id,
            plan_id=context.plan_id, approval_request_id=approval_request_id,
            transaction_id=context.transaction_id, status=status, message=message,
            approval_required=status == "approval_required", error_code=error_code,
        )


__all__ = ["MAX_REMOTE_INTENT_CHARS", "REMOTE_CHANNELS", "RemoteWorkContext",
           "RemoteWorkCoordinator", "RemoteWorkRequest", "RemoteWorkResult"]
