"""Property-style security invariant tests for Phase 15H Work domain.

Tests verify all 10 architectural security properties:
- PROPERTY 1: No Work action can directly execute filesystem mutation.
- PROPERTY 2: No Work action can bypass ApprovalService.
- PROPERTY 3: No Work action can bypass ApprovedExecutionContract.
- PROPERTY 4: No Work action can bypass Transaction.
- PROPERTY 5: No Work action can create a second orchestrator path.
- PROPERTY 6: Stale session incarnation cannot regain execution authority.
- PROPERTY 7: Concurrent resume cannot execute twice.
- PROPERTY 8: Stale Work revision cannot overwrite newer state.
- PROPERTY 9: Unauthorized actor cannot inspect another actor's Work.
- PROPERTY 10: Work persistence cannot leak secrets.
"""
from __future__ import annotations

import concurrent.futures
import inspect
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    InMemoryApprovalStore,
)
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.session import InMemorySessionStore, SessionManager, SessionState
from core.runtime.transaction import (
    InMemoryTransactionStore,
    Transaction,
    TransactionRecoveryManager,
    TransactionStatus,
)
from core.runtime.work import (
    VerificationResult,
    VerificationStatus,
    Work,
    WorkFailure,
    WorkStatus,
)
from core.runtime.work_authorization import (
    authorize_work_execution_continuation,
    authorize_work_inspection,
)
from core.runtime.work_continuation import cancel_work, resume_work
from core.runtime.work_presentation import (
    _clean,
    format_work_detail,
    format_works_list,
)
from core.runtime.work_store import (
    FileWorkStore,
    InMemoryWorkStore,
    StaleWorkRevisionError,
)


class TestWorkSecurityProperties(unittest.TestCase):
    """Enforce the 10 architectural and security invariants of Phase 15H."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.repo_dir = Path(self.temp_dir)
        self.work_store = FileWorkStore(repo_dir=self.repo_dir)
        self.approval_store = InMemoryApprovalStore()
        self.approval_service = ApprovalService(store=self.approval_store)
        self.transaction_store = InMemoryTransactionStore()
        self.recovery_mgr = TransactionRecoveryManager(
            workspace=self.repo_dir,
            store=self.transaction_store,
        )

        # Baseline workspace file to verify no unexpected modifications
        self.canary_file = self.repo_dir / "canary.txt"
        self.canary_file.write_text("unmodified canary", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_property_1_no_direct_filesystem_mutation(self) -> None:
        """PROPERTY 1: No Work action can directly execute filesystem mutation."""
        # Record state of files outside .brainfrog
        canary_stat_before = self.canary_file.stat()

        work = Work(
            id="work_p1",
            intent="Mutate codebase",
            goal="Mutate codebase",
            actor_id="alice",
            channel="cli",
            status=WorkStatus.PLANNING,
        )
        self.work_store.create(work)

        # Work operations (resume, cancel, update) cannot mutate workspace files
        resume_work(
            work_id="work_p1",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            approval_service=self.approval_service,
            transaction_store=self.transaction_store,
            recovery_manager=self.recovery_mgr,
        )

        cancel_work(
            work_id="work_p1",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            approval_service=self.approval_service,
            transaction_store=self.transaction_store,
        )

        canary_stat_after = self.canary_file.stat()
        self.assertEqual(canary_stat_before.st_mtime_ns, canary_stat_after.st_mtime_ns)
        self.assertEqual(self.canary_file.read_text(encoding="utf-8"), "unmodified canary")

        # Workspace should contain only canary.txt and .brainfrog directory
        workspace_entries = set(os.listdir(self.repo_dir))
        self.assertEqual(workspace_entries, {"canary.txt", ".brainfrog"})

    def test_property_2_no_work_action_can_bypass_approval_service(self) -> None:
        """PROPERTY 2: No Work action can bypass ApprovalService."""
        work = Work(
            id="work_p2",
            intent="Run high risk database drop",
            goal="Drop DB",
            actor_id="alice",
            channel="telegram",
            status=WorkStatus.APPROVAL_REQUIRED,
            approval_request_id="req_high_risk_1",
        )
        self.work_store.create(work)

        # Attempt to resume work in APPROVAL_REQUIRED without ApprovalService transition
        ok, msg, updated = resume_work(
            work_id="work_p2",
            actor_id="alice",
            channel="telegram",
            work_store=self.work_store,
            approval_service=self.approval_service,
            transaction_store=self.transaction_store,
            recovery_manager=self.recovery_mgr,
        )
        self.assertTrue(ok)
        self.assertIn("awaiting approval", msg.lower())

        # Work status must NOT transition to EXECUTING or DONE
        reloaded = self.work_store.get("work_p2")
        assert reloaded is not None
        self.assertEqual(reloaded.status, WorkStatus.APPROVAL_REQUIRED)

    def test_property_3_no_work_action_can_bypass_approved_execution_contract(self) -> None:
        """PROPERTY 3: No Work action can bypass ApprovedExecutionContract."""
        work = Work(
            id="work_p3",
            intent="Build project",
            goal="Compile binaries",
            actor_id="alice",
            channel="cli",
            status=WorkStatus.PLANNING,
        )
        self.work_store.create(work)

        # Verify Work dataclass has no authority fields
        work_dict = work.to_dict()
        self.assertNotIn("contract", work_dict)
        self.assertNotIn("execution_permissions", work_dict)
        self.assertNotIn("approved_contract", work_dict)

        # Work deserialization strictly rejects authority injection
        with self.assertRaises(ValueError):
            Work.from_dict({
                **work_dict,
                "contract": {"type": "ApprovedExecutionContract"},
            })

        # resume_work never returns execution contract or capability tokens
        ok, msg, res_work = resume_work(
            work_id="work_p3",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            approval_service=self.approval_service,
            transaction_store=self.transaction_store,
            recovery_manager=self.recovery_mgr,
        )
        self.assertNotIsInstance(res_work, ApprovedExecutionContract)

    def test_property_4_no_work_action_can_bypass_transaction(self) -> None:
        """PROPERTY 4: No Work action can bypass Transaction."""
        # A work in EXECUTING state without a valid transaction fails closed
        work = Work(
            id="work_p4_no_tx",
            intent="Mutate files",
            goal="Mutate files",
            actor_id="alice",
            channel="cli",
            status=WorkStatus.EXECUTING,
            transaction_id=None,  # Missing transaction!
        )
        self.work_store.create(work)

        ok, msg, res = resume_work(
            work_id="work_p4_no_tx",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            approval_service=self.approval_service,
            transaction_store=self.transaction_store,
            recovery_manager=self.recovery_mgr,
        )
        self.assertFalse(ok)
        self.assertIn("missing transaction", msg.lower())

        stored = self.work_store.get("work_p4_no_tx")
        assert stored is not None
        self.assertEqual(stored.status, WorkStatus.FAILED)
        self.assertEqual(stored.failure.code, "MISSING_TX")

    def test_property_5_no_second_orchestrator_path(self) -> None:
        """PROPERTY 5: No Work action can create a second orchestrator path."""
        import core.runtime.work_continuation as wc_mod

        wc_source = inspect.getsource(wc_mod)
        # work_continuation must not define or run an execution loop or orchestrator
        self.assertNotIn("class Orchestrator", wc_source)
        self.assertNotIn("run_orchestrator", wc_source)
        self.assertNotIn("execute_plan_steps", wc_source)

        # orchestrator.py is the sole execution engine
        from orchestrator import Orchestrator
        self.assertTrue(inspect.isclass(Orchestrator))

    def test_property_6_stale_session_incarnation_cannot_regain_execution_authority(self) -> None:
        """PROPERTY 6: Stale session incarnation cannot regain execution authority."""
        work = Work(
            id="work_p6",
            intent="Deploy server",
            goal="Deploy server",
            actor_id="alice",
            channel="telegram",
            session_id="sess_100",
            session_incarnation_id="inc_old_1",
            status=WorkStatus.APPROVAL_REQUIRED,
        )
        self.work_store.create(work)

        # Attempt to resume with stale session incarnation
        ok, reason = authorize_work_execution_continuation(
            work=work,
            actor_id="alice",
            channel="telegram",
            current_session_id="sess_100",
            current_session_incarnation_id="inc_new_2",  # Reset happened!
        )
        self.assertFalse(ok)
        self.assertIn("session incarnation", reason.lower())

        # Calling resume_work also fails closed
        res_ok, res_msg, _ = resume_work(
            work_id="work_p6",
            actor_id="alice",
            channel="telegram",
            session_id="sess_100",
            session_incarnation_id="inc_new_2",
            work_store=self.work_store,
            approval_service=self.approval_service,
            transaction_store=self.transaction_store,
            recovery_manager=self.recovery_mgr,
        )
        self.assertFalse(res_ok)
        self.assertIn("session incarnation", res_msg.lower())

    def test_property_7_concurrent_resume_cannot_execute_twice(self) -> None:
        """PROPERTY 7: Concurrent resume cannot execute twice."""
        # Create a mock transaction in COMMITTED status
        tx = Transaction(
            id="tx_p7_1",
            session_id="sess_p7",
            status=TransactionStatus.COMMITTED,
        )
        self.transaction_store.save(tx)

        work = Work(
            id="work_p7",
            intent="Process data",
            goal="Process data",
            actor_id="alice",
            channel="cli",
            status=WorkStatus.EXECUTING,
            transaction_id="tx_p7_1",
        )
        self.work_store.create(work)

        results = []
        def attempt_resume() -> tuple[bool, str]:
            return resume_work(
                work_id="work_p7",
                actor_id="alice",
                channel="cli",
                work_store=self.work_store,
                approval_service=self.approval_service,
                transaction_store=self.transaction_store,
                recovery_manager=self.recovery_mgr,
            )[:2]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futs = [executor.submit(attempt_resume) for _ in range(8)]
            for fut in concurrent.futures.as_completed(futs):
                results.append(fut.result())

        # Work must transition to VERIFYING (committed tx recovered)
        final_work = self.work_store.get("work_p7")
        assert final_work is not None
        self.assertEqual(final_work.status, WorkStatus.VERIFYING)

    def test_property_8_stale_work_revision_cannot_overwrite_newer_state(self) -> None:
        """PROPERTY 8: Stale Work revision cannot overwrite newer state."""
        work = Work(
            id="work_p8",
            intent="Task 8",
            goal="Goal 8",
            status=WorkStatus.CREATED,
        )
        created = self.work_store.create(work)
        self.assertEqual(created.revision, 1)

        # Worker A reads revision 1
        worker_a_view = self.work_store.get("work_p8")
        assert worker_a_view is not None

        # Worker B reads revision 1 and updates to revision 2
        worker_b_view = self.work_store.get("work_p8")
        assert worker_b_view is not None
        self.work_store.save(worker_b_view.transition(WorkStatus.PLANNING), expected_revision=1)

        # Current revision is now 2
        current = self.work_store.get("work_p8")
        assert current is not None
        self.assertEqual(current.revision, 2)

        # Worker A tries to update using stale revision 1 -> must raise StaleWorkRevisionError
        with self.assertRaises(StaleWorkRevisionError):
            self.work_store.save(worker_a_view.transition(WorkStatus.FAILED), expected_revision=1)

        # Verify state was not corrupted
        reloaded = self.work_store.get("work_p8")
        assert reloaded is not None
        self.assertEqual(reloaded.status, WorkStatus.PLANNING)
        self.assertEqual(reloaded.revision, 2)

    def test_property_9_unauthorized_actor_cannot_inspect_another_actors_work(self) -> None:
        """PROPERTY 9: Unauthorized actor cannot inspect another actor's Work."""
        work = Work(
            id="work_p9_secret",
            intent="Audit proprietary security findings",
            goal="Fix zero-day CVE-2026-9999",
            actor_id="alice",
            channel="telegram",
            status=WorkStatus.PLANNING,
        )
        self.work_store.create(work)

        # Mallory attempts to inspect Alice's work
        ok, reason = authorize_work_inspection(
            work=work,
            actor_id="mallory",
            channel="telegram",
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "Work 'work_p9_secret' not found or access denied.")

        # Inspection listing strictly excludes Alice's work when queried by Mallory
        mallory_works = self.work_store.list_for_actor("mallory")
        self.assertEqual(len(mallory_works), 0)

    def test_property_10_work_persistence_cannot_leak_secrets(self) -> None:
        """PROPERTY 10: Work persistence cannot leak secrets."""
        # 1. Constructor strictly rejects credentials
        with self.assertRaises(ValueError):
            Work(
                id="work_p10_cred",
                intent="sk-proj-1234567890123456789012345678901234567890",
                goal="legitimate goal",
            )

        with self.assertRaises(ValueError):
            WorkFailure(
                code="AUTH_FAIL",
                summary="Authorization: Bearer secret_token_xyz123",
            )

        # 2. Defense-in-depth presentation cleaning scrubs any unexpected tokens
        leak_input = "Exported key=password: super_secret_pass_123"
        cleaned = _clean(leak_input)
        self.assertNotIn("super_secret_pass_123", cleaned)
        self.assertIn("REDACTED", cleaned)


if __name__ == "__main__":
    unittest.main()
