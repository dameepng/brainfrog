"""BrainFrog Phase 5 — Canonical Runtime SDK Client.

Provides the single, authoritative public API boundary for BrainFrog.
Delegates strictly to BrainFrogRuntime, preserving all trust, transaction,
process safety, verification, and proof invariants established in Phases 1–4.

Product Thesis:
    "Don't just trust the agent. Verify it."
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Union,
)

from core.runtime import (
    ApprovalRequest,
    ApprovalStatus,
    BrainFrogRuntime,
    DeterministicProofVerifier,
    FileProofStore,
    ProofArtifact,
    ProofVerificationReport,
    TaskActiveError,
    TaskAlreadyCompletedError,
    TaskRecoveryRequiredError,
    TaskPlan,
    TaskRequest,
    TaskReservationCoordinator,
    TaskReservationHandle,
    TaskResult,
    terminate_processes_for_task,
    validate_task_id,
)
from brainfrog.sdk.errors import (
    ApprovalRequiredError,
    BrainFrogSDKError,
    ExecutionFailedError,
    InvalidRequestError,
    PermissionDeniedError,
    ProofPersistenceError,
    RecoveryRequiredError,
    RuntimeUnavailableError,
    TaskCancelledError,
    TaskNotFoundError,
    VerificationFailedError,
    WorkspaceBusyError,
)
from brainfrog.sdk.events import (
    EventBroker,
    EventType,
    SDKEvent,
    create_sdk_event,
)
from brainfrog.sdk.models import RuntimeConfig, TaskStatus


class TaskHandle:
    """Handle to an actively running or completed SDK task."""

    def __init__(
        self,
        task_id: str,
        request: TaskRequest,
        client: BrainFrogClient,
        future: Optional[asyncio.Future[TaskResult]],
        cancel_event: threading.Event,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self.task_id = task_id
        self.request = request
        self._client = client
        self._future = future
        self._cancel_event = cancel_event
        self._loop = loop
        self._done_event = threading.Event()
        self._result: Optional[TaskResult] = None
        self._exception: Optional[BaseException] = None

    def _set_result(self, result: TaskResult) -> None:
        """Internal callback to set the task result."""
        self._result = result
        self._done_event.set()
        if self._future is not None and self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._future.set_result, result)
            except RuntimeError:
                pass

    def _set_exception(self, exc: BaseException) -> None:
        """Internal callback to set the task exception."""
        self._exception = exc
        self._done_event.set()
        if self._future is not None and self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._future.set_exception, exc)
            except RuntimeError:
                pass

    @property
    def is_done(self) -> bool:
        """Check if the task has finished executing."""
        return self._done_event.is_set()

    @property
    def is_cancelled(self) -> bool:
        """Check if cancellation was requested on this task."""
        return self._cancel_event.is_set()

    @property
    def status(self) -> TaskStatus:
        """Return the current task status."""
        if not self._done_event.is_set():
            return TaskStatus.CANCELLED if self.is_cancelled else TaskStatus.RUNNING
        if self._exception is not None:
            return TaskStatus.FAILED
        res = self._result
        if res is None:
            return TaskStatus.FAILED
        if res.verified:
            return TaskStatus.COMMITTED
        if res.status in ("CANCELLED", "cancelled"):
            return TaskStatus.CANCELLED
        if res.status in ("LOCKED", "locked"):
            return TaskStatus.LOCKED
        if res.status in ("REJECTED", "rejected"):
            return TaskStatus.REJECTED
        if res.status in ("RECOVERY_REQUIRED", "recovery_required") or res.recovery_required:
            return TaskStatus.RECOVERY_REQUIRED
        if res.status in ("ROLLED_BACK", "rolled_back"):
            return TaskStatus.ROLLED_BACK
        return TaskStatus.FAILED

    def wait_sync(self, timeout: Optional[float] = None) -> TaskResult:
        """Wait synchronously for the task to complete and return its TaskResult."""
        signaled = self._done_event.wait(timeout=timeout)
        if not signaled:
            raise TimeoutError(f"Task '{self.task_id}' did not complete within {timeout}s")
        if self._exception is not None:
            raise self._exception
        assert self._result is not None
        return self._result

    async def wait(self) -> TaskResult:
        """Asynchronously wait for the task to complete and return its TaskResult."""
        if self._future is not None:
            return await self._future
        return await asyncio.to_thread(self.wait_sync)

    async def cancel(self) -> None:
        """Request immediate, safe cancellation of the task."""
        self._cancel_event.set()
        terminate_processes_for_task(self.task_id)
        if self._future is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._future), timeout=15.0)
            except (asyncio.TimeoutError, Exception):
                pass
        else:
            await asyncio.to_thread(self._done_event.wait, 15.0)

    def cancel_sync(self) -> None:
        """Synchronously signal cancellation and kill descendant subprocesses."""
        self._cancel_event.set()
        terminate_processes_for_task(self.task_id)


class BrainFrogClient:
    """Canonical public SDK client for BrainFrog.

    Enforces:
    - One Runtime: Delegates directly to BrainFrogRuntime.
    - One Trust Boundary: Passes through Trust, Approval, and Permission policies.
    - One Task Identity: task_id propagates stably end-to-end.
    - One Session Identity: channel:user_id:conversation_id session isolation.
    - Verification is Authoritative: Never accepts success without verified=True.
    - Idempotency: Rejects duplicate active or completed task_ids.
    """

    def __init__(
        self,
        workspace: Union[str, Path],
        *,
        config: Optional[RuntimeConfig] = None,
        runtime: Optional[BrainFrogRuntime] = None,
        channel: str = "cli",
        user_id: str = "local",
        conversation_id: Optional[str] = None,
        backend: str = "jev",
        provider: Optional[str] = None,
        model: Optional[str] = None,
        test_command: Optional[Sequence[str]] = None,
        timeout_seconds: Optional[float] = None,
        max_retries: int = 2,
        require_approval: bool = False,
        two_man_rule_enabled: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        system1_factory: Optional[Callable[[str], Any]] = None,
        system2_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        raw_ws = Path(workspace)
        try:
            self._workspace = raw_ws.resolve()
        except Exception as e:
            raise InvalidRequestError(f"Invalid workspace path: {workspace}") from e

        if not self._workspace.exists() or not self._workspace.is_dir():
            raise InvalidRequestError(f"Workspace path does not exist or is not a directory: {self._workspace}")

        if config is not None:
            self._config = config
        else:
            self._config = RuntimeConfig(
                workspace=self._workspace,
                channel=channel,
                user_id=user_id,
                conversation_id=conversation_id,
                backend=backend,
                provider=provider,
                model=model,
                test_command=test_command,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
                require_approval=require_approval,
                two_man_rule_enabled=two_man_rule_enabled,
                metadata=metadata or {},
            )

        self._active_tasks: Dict[str, TaskHandle] = {}
        self._active_cancel_events: Dict[str, threading.Event] = {}
        self._active_reservations: Dict[str, TaskReservationHandle] = {}
        self._reservations = TaskReservationCoordinator(self._workspace)
        self._events = EventBroker()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bf_sdk")
        self._lock = threading.Lock()

        if runtime is not None:
            self._runtime = runtime
        else:
            try:
                self._runtime = BrainFrogRuntime(
                    repo_dir=self._workspace,
                    default_backend=self._config.backend,
                    default_provider=self._config.provider,
                    default_model=self._config.model,
                    default_test_cmd=list(self._config.test_command) if self._config.test_command else None,
                    require_approval=self._config.require_approval,
                    two_man_rule_enabled=self._config.two_man_rule_enabled,
                    auto_recover_transactions=self._config.auto_recover_transactions,
                    persist_sessions=self._config.persist_sessions,
                    system1_factory=system1_factory,
                    system2_factory=system2_factory,
                )
            except Exception as exc:
                raise RuntimeUnavailableError(f"Failed to initialize BrainFrog runtime: {exc}") from exc

    @property
    def workspace(self) -> Path:
        """The canonical workspace directory for this client."""
        return self._workspace

    @property
    def runtime(self) -> BrainFrogRuntime:
        """Underlying canonical runtime instance."""
        return self._runtime

    @property
    def canonical_session_id(self) -> str:
        """Canonical session identity matching the runtime's session resolver: channel:user_id:conversation_id."""
        c = self._config.channel or "cli"
        u = self._config.user_id or "default"
        conv = self._config.conversation_id or "default"
        return f"{c}:{u}:{conv}"

    async def __aenter__(self) -> BrainFrogClient:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    def __enter__(self) -> BrainFrogClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close_sync()

    async def close(self) -> None:
        """Cancel any running tasks and release client resources."""
        active = list(self._active_tasks.values())
        for handle in active:
            if not handle.is_done:
                await handle.cancel()
        with self._lock:
            for res_h in list(self._active_reservations.values()):
                res_h.release()
            self._active_reservations.clear()
        self._reservations.release_all()
        self._events.clear()
        self._executor.shutdown(wait=False)

    def close_sync(self) -> None:
        """Synchronously cancel running tasks and release resources."""
        active = list(self._active_tasks.values())
        for handle in active:
            if not handle.is_done:
                handle.cancel_sync()
        with self._lock:
            for res_h in list(self._active_reservations.values()):
                res_h.release()
            self._active_reservations.clear()
        self._reservations.release_all()
        self._events.clear()
        self._executor.shutdown(wait=False)

    def on_event(self, handler: Callable[[SDKEvent], None]) -> None:
        """Register a synchronous event listener callback."""
        self._events.add_sync_handler(handler)

    def remove_event_handler(self, handler: Callable[[SDKEvent], None]) -> None:
        """Remove a previously registered synchronous event listener callback."""
        self._events.remove_sync_handler(handler)

    async def events(self, task_id: Optional[str] = None) -> AsyncIterator[SDKEvent]:
        """Subscribe to lifecycle events for a specific task or all tasks as an async iterator."""
        q = self._events.subscribe_async(task_id)
        try:
            while True:
                ev = await q.get()
                yield ev
                # Stop iterating if this task reached terminal state
                if task_id and ev.event_type in (
                    EventType.TASK_COMPLETED,
                    EventType.TASK_FAILED,
                    EventType.TASK_CANCELLED,
                    EventType.RECOVERY_REQUIRED,
                ):
                    break
        finally:
            self._events.unsubscribe_async(q, task_id)

    def _normalize_request(
        self,
        task: Union[str, TaskRequest],
        *,
        task_id: Optional[str] = None,
        test_command: Optional[Sequence[str]] = None,
        allowed_scope: Optional[Sequence[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TaskRequest:
        """Validate and normalize input into a canonical TaskRequest."""
        if isinstance(task, TaskRequest):
            req = task
            if req.workspace.resolve() != self._workspace:
                raise InvalidRequestError(
                    f"TaskRequest workspace '{req.workspace}' does not match client workspace '{self._workspace}'"
                )
            try:
                chosen_id = validate_task_id(req.task_id)
            except (ValueError, TypeError) as exc:
                raise InvalidRequestError(str(exc)) from exc
            return req

        if not isinstance(task, str) or not task.strip():
            raise InvalidRequestError("Task description must be a non-empty string.")

        MAX_TASK_LENGTH = 100_000
        if len(task.strip()) > MAX_TASK_LENGTH:
            raise InvalidRequestError(f"Task description exceeds maximum allowed length of {MAX_TASK_LENGTH} characters.")

        raw_id = (task_id or f"task_{uuid.uuid4().hex[:12]}").strip()
        try:
            chosen_id = validate_task_id(raw_id)
        except (ValueError, TypeError) as exc:
            raise InvalidRequestError(str(exc)) from exc

        if allowed_scope:
            for s in allowed_scope:
                if re.match(r"^[a-zA-Z]:", s):
                    raise InvalidRequestError(f"Scope path traversal forbidden: drive-relative or absolute path '{s}'")
                p = Path(s)
                if ".." in p.parts or os.path.isabs(s):
                    raise InvalidRequestError(f"Scope path traversal forbidden: '{s}'")

        merged_metadata = dict(self._config.metadata)
        if metadata:
            merged_metadata.update(metadata)

        return TaskRequest(
            task_id=chosen_id,
            user_request=task.strip(),
            workspace=self._workspace,
            channel=self._config.channel,
            session_id=self._config.conversation_id,
            allowed_scope=allowed_scope,
            test_command=test_command or self._config.test_command,
            max_retries=self._config.max_retries,
            metadata=merged_metadata,
        )

    def _execute_task_pipeline(
        self,
        request: TaskRequest,
        cancel_event: threading.Event,
        on_event: Optional[Callable[[SDKEvent], None]] = None,
    ) -> TaskResult:
        """Internal authoritative execution pipeline.

        Shared identically between run() and run_sync().
        Does NOT instantiate Orchestrator. Delegates to BrainFrogRuntime.
        """
        if on_event:
            self._events.add_sync_handler(on_event)

        def event_sink(ev: SDKEvent) -> None:
            self._events.emit(ev)

        # Wire cancellation and event callback into request metadata
        req_meta = dict(request.metadata)
        req_meta["cancel_event"] = cancel_event
        req_meta["event_listener"] = event_sink

        wired_request = TaskRequest(
            task_id=request.task_id,
            user_request=request.user_request,
            workspace=request.workspace,
            channel=request.channel,
            session_id=request.session_id,
            allowed_scope=request.allowed_scope,
            test_command=request.test_command,
            max_retries=request.max_retries,
            metadata=req_meta,
        )

        try:
            result: TaskResult = self._runtime.execute_autonomous_task(wired_request)
            return result
        except KeyboardInterrupt:
            # Propagate clean cancellation TaskResult
            return TaskResult(
                task_id=request.task_id,
                user_request=request.user_request,
                success=False,
                verified=False,
                status="CANCELLED",
                summary="⚠️ Task cancelled by user/SDK.",
                failure_reason="Execution cancelled by cancellation token",
            )
        finally:
            if on_event:
                self._events.remove_sync_handler(on_event)

    def start(
        self,
        task: Union[str, TaskRequest],
        *,
        task_id: Optional[str] = None,
        test_command: Optional[Sequence[str]] = None,
        allowed_scope: Optional[Sequence[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        on_event: Optional[Callable[[SDKEvent], None]] = None,
    ) -> TaskHandle:
        """Start task asynchronously and return a TaskHandle immediately."""
        req = self._normalize_request(
            task,
            task_id=task_id,
            test_command=test_command,
            allowed_scope=allowed_scope,
            metadata=metadata,
        )

        with self._lock:
            if req.task_id in self._active_tasks and not self._active_tasks[req.task_id].is_done:
                raise InvalidRequestError(f"Task '{req.task_id}' is already actively executing.")

            try:
                reservation = self._reservations.reserve(req.task_id)
            except TaskRecoveryRequiredError as exc:
                raise RecoveryRequiredError(str(exc), task_id=req.task_id) from exc
            except (TaskActiveError, TaskAlreadyCompletedError) as exc:
                raise InvalidRequestError(str(exc)) from exc

            cancel_ev = threading.Event()

            loop: Optional[asyncio.AbstractEventLoop] = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

            future: Optional[asyncio.Future[TaskResult]] = None
            if loop is not None and not loop.is_closed():
                future = loop.create_future()

            handle = TaskHandle(
                task_id=req.task_id,
                request=req,
                client=self,
                future=future,
                cancel_event=cancel_ev,
                loop=loop,
            )

            self._active_tasks[req.task_id] = handle
            self._active_cancel_events[req.task_id] = cancel_ev
            self._active_reservations[req.task_id] = reservation

        def _worker() -> None:
            try:
                res = self._execute_task_pipeline(req, cancel_ev, on_event)
                handle._set_result(res)
            except BaseException as exc:
                handle._set_exception(exc)
            finally:
                with self._lock:
                    self._active_tasks.pop(req.task_id, None)
                    self._active_cancel_events.pop(req.task_id, None)
                    res_h = self._active_reservations.pop(req.task_id, None)
                    if res_h is not None:
                        res_h.release()

        self._executor.submit(_worker)
        return handle

    async def run(
        self,
        task: Union[str, TaskRequest],
        *,
        task_id: Optional[str] = None,
        test_command: Optional[Sequence[str]] = None,
        allowed_scope: Optional[Sequence[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        on_event: Optional[Callable[[SDKEvent], None]] = None,
        raise_on_failure: bool = False,
    ) -> TaskResult:
        """Run an autonomous task to completion asynchronously.

        If raise_on_failure is True, raises a typed error on non-verified results.
        """
        handle = self.start(
            task,
            task_id=task_id,
            test_command=test_command,
            allowed_scope=allowed_scope,
            metadata=metadata,
            on_event=on_event,
        )
        result = await handle.wait()

        if raise_on_failure and not result.verified:
            self._raise_for_status(result)

        return result

    def run_sync(
        self,
        task: Union[str, TaskRequest],
        *,
        task_id: Optional[str] = None,
        test_command: Optional[Sequence[str]] = None,
        allowed_scope: Optional[Sequence[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        on_event: Optional[Callable[[SDKEvent], None]] = None,
        raise_on_failure: bool = False,
    ) -> TaskResult:
        """Run an autonomous task to completion synchronously.

        Delegates to the exact same execution pipeline as run().
        """
        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "run_sync() cannot be called from a thread with an active running event loop; "
                "use 'await client.run(...)' instead."
            )
        except RuntimeError as exc:
            if "run_sync() cannot be called" in str(exc):
                raise

        req = self._normalize_request(
            task,
            task_id=task_id,
            test_command=test_command,
            allowed_scope=allowed_scope,
            metadata=metadata,
        )

        cancel_ev = threading.Event()
        with self._lock:
            if req.task_id in self._active_tasks and not self._active_tasks[req.task_id].is_done:
                raise InvalidRequestError(f"Task '{req.task_id}' is already actively executing.")
            try:
                reservation = self._reservations.reserve(req.task_id)
            except TaskRecoveryRequiredError as exc:
                raise RecoveryRequiredError(str(exc), task_id=req.task_id) from exc
            except (TaskActiveError, TaskAlreadyCompletedError) as exc:
                raise InvalidRequestError(str(exc)) from exc

            handle = TaskHandle(
                task_id=req.task_id,
                request=req,
                client=self,
                future=None,
                cancel_event=cancel_ev,
                loop=None,
            )
            self._active_tasks[req.task_id] = handle
            self._active_cancel_events[req.task_id] = cancel_ev
            self._active_reservations[req.task_id] = reservation

        try:
            result = self._execute_task_pipeline(req, cancel_ev, on_event)
            handle._set_result(result)
        except BaseException as exc:
            handle._set_exception(exc)
            raise
        finally:
            with self._lock:
                self._active_tasks.pop(req.task_id, None)
                self._active_cancel_events.pop(req.task_id, None)
                res_h = self._active_reservations.pop(req.task_id, None)
                if res_h is not None:
                    res_h.release()

        if raise_on_failure and not result.verified:
            self._raise_for_status(result)

        return result

    def cancel_task(self, task_id: str) -> None:
        """Signal cancellation for an active task by ID."""
        with self._lock:
            ev = self._active_cancel_events.get(task_id)
        if ev:
            ev.set()
        terminate_processes_for_task(task_id)

    def get_proof(self, task_id: str) -> Optional[ProofArtifact]:
        """Retrieve the authoritative ProofArtifact for a completed task."""
        store = FileProofStore(self._workspace)
        try:
            return store.load(task_id)
        except FileNotFoundError:
            return None

    def verify_proof(self, target: Union[str, Path]) -> ProofVerificationReport:
        """Deterministically inspect and verify a proof artifact by task ID or path.

        Performs read-only structural, gate, transaction, and provenance consistency checks.
        Does not trust LLM assertions, self-declared VERIFIED status, or derived Markdown.
        """
        verifier = DeterministicProofVerifier(self._workspace)
        return verifier.verify(target)

    def get_task(self, task_id: str) -> Optional[TaskResult]:
        """Construct a TaskResult from a previously saved proof artifact."""
        artifact = self.get_proof(task_id)
        if not artifact:
            return None
        is_verified = (artifact.verdict.status == "VERIFIED")
        task_status: str
        if is_verified:
            task_status = "COMMITTED"
        elif artifact.verdict.status == "RECOVERY_REQUIRED":
            task_status = "RECOVERY_REQUIRED"
        elif artifact.changes.status.upper() == "ROLLED_BACK":
            task_status = "ROLLED_BACK"
        elif artifact.verdict.status == "CANCELLED":
            task_status = "CANCELLED"
        else:
            task_status = "FAILED"

        return TaskResult(
            task_id=artifact.task.task_id,
            user_request=artifact.task.description,
            success=is_verified,
            verified=is_verified,
            status=task_status,
            summary=artifact.verdict.reason,
            plan=None,
            tests_passed=(artifact.verification.exit_code == 0),
            test_output=artifact.verification.stdout_summary,
            changed_files=[f.path for f in artifact.changes.files],
            transaction_id=artifact.changes.transaction_id,
            transaction_status=artifact.changes.status.lower(),
            proof_path=str(self._workspace / ".brainfrog" / "proofs" / f"{task_id}.json"),
            proof_markdown_path=str(self._workspace / ".brainfrog" / "proofs" / f"{task_id}.md"),
            proof_artifact=artifact,
        )

    def list_pending_approvals(self) -> List[ApprovalRequest]:
        """List active pending approvals for the current canonical session."""
        pending = self._runtime.approval_service.store.list_requests(
            session_id=self.canonical_session_id,
            active_only=True,
            limit=50,
        )
        return [
            r for r in pending
            if not r.is_expired()
            and (
                r.status == ApprovalStatus.PENDING
                or str(getattr(r.status, "value", r.status)).lower() == "pending"
            )
        ]

    def approve(self, approval_id: str, approver: str, channel: Optional[str] = None) -> None:
        """Approve a pending approval request via canonical approval service."""
        eff_channel = channel or self._config.channel
        success, msg, _ = self._runtime.approval_service.approve(
            request_id=approval_id,
            approver_id=approver,
            channel=eff_channel,
            session_id=self.canonical_session_id,
        )
        if not success:
            raise PermissionDeniedError(f"Approval failed: {msg}")

    def reject(self, approval_id: str, approver: str, reason: str = "Rejected by operator", channel: Optional[str] = None) -> None:
        """Reject a pending approval request via canonical approval service."""
        eff_channel = channel or self._config.channel
        success, msg, _ = self._runtime.approval_service.reject(
            request_id=approval_id,
            approver_id=approver,
            channel=eff_channel,
            reason=reason,
            session_id=self.canonical_session_id,
        )
        if not success:
            raise PermissionDeniedError(f"Rejection failed: {msg}")

    @staticmethod
    def _raise_for_status(result: TaskResult) -> None:
        """Map a failed TaskResult to the appropriate typed exception."""
        if result.status == "CANCELLED":
            raise TaskCancelledError(
                result.failure_reason or "Task was cancelled",
                task_id=result.task_id,
            )
        if result.status == "LOCKED":
            raise WorkspaceBusyError(
                result.failure_reason or "Workspace locked",
                task_id=result.task_id,
            )
        if result.status == "REJECTED":
            if "approval" in (result.failure_reason or "").lower():
                raise ApprovalRequiredError(
                    result.failure_reason or "Operation requires approval",
                    approval_id="pending",
                    task_id=result.task_id,
                )
            raise PermissionDeniedError(
                result.failure_reason or "Action denied by trust policy",
                task_id=result.task_id,
            )
        if "proof persistence" in (result.failure_reason or "").lower():
            raise ProofPersistenceError(
                result.failure_reason or "Proof persistence failed",
                task_id=result.task_id,
            )
        if result.recovery_required or result.status == "RECOVERY_REQUIRED":
            raise RecoveryRequiredError(
                result.failure_reason or "Manual recovery required",
                task_id=result.task_id,
            )
        if not result.tests_passed:
            raise VerificationFailedError(
                result.failure_reason or "Tests or verification gate failed",
                task_id=result.task_id,
                details=result.verification_details,
            )
        raise ExecutionFailedError(
            result.failure_reason or "Execution failed",
            task_id=result.task_id,
        )


__all__ = [
    "BrainFrogClient",
    "TaskHandle",
]
