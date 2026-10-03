"""Phase 14B-3: HIGH-02 Stale Approval Invalidation & Session Incarnation Tests.

Validates the remediation of:
- HIGH-02: Stale approvals surviving /reset or /new session resets.
- Cryptographic session incarnation binding (session_id + session_incarnation_id).
- Invalidation of outstanding (PENDING and APPROVED) approvals on session reset.
- Preservation of session incarnation across process restarts.
- Immediate rejection of cross-incarnation, cross-user, cross-channel, and legacy approvals.
- Concurrency races between /approve vs /reset and /exec vs /reset.
- True end-to-end verification proving orchestrator is never invoked for stale approvals.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    FileApprovalStore,
    InMemoryApprovalStore,
)
from core.runtime.messages import IncomingMessage
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import (
    FileSessionStore,
    InMemorySessionStore,
    SessionManager,
    SessionState,
)
from system1.base import Answer, SystemOneClient
from system2 import System2Client


# =============================================================================
# Deterministic Test Doubles
# =============================================================================

class DeterministicFakeSystem1(SystemOneClient):
    name: str = "fake_system1"

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        return {
            "likely_domain": Answer(choice="unrelated", confidence=0.9),
            "change_type": Answer(choice="write_files", confidence=0.9),
            "is_sensitive": Answer(noul=0.1, confidence=0.9),
        }


class DeterministicFakeSystem2:
    provider_name: str = "fake_system2"
    model: str = "mock-model"

    def __init__(self) -> None:
        self.guidelines: str = ""
        self.call_count: int = 0

    def plan_task(self, task: str, focus_tree: str = "", **kwargs: Any) -> List[Any]:
        self.call_count += 1
        return []

    def write_code(self, step: Any, task: str, file_contents: Dict[str, str], **kwargs: Any) -> Dict[str, str]:
        self.call_count += 1
        return {"config.py": "# modified"}

    def draft_pr(self, task: str, files_changed: List[str], test_summary: str = "", **kwargs: Any) -> Dict[str, str]:
        return {"title": f"feat: {task}", "body": "PR"}


# =============================================================================
# Phase 14B-3 Test Suite
# =============================================================================

class TestApprovalSessionInvalidation(unittest.TestCase):
    """Test suite validating session incarnation lifecycle and stale approval invalidation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="bf_test_session_invalidation_")
        self.repo_dir = Path(self.temp_dir).resolve()
        self.approvals_dir = self.repo_dir / ".brainfrog" / "approvals"
        self.sessions_dir = self.repo_dir / ".brainfrog" / "sessions"

        self.session_store = FileSessionStore(sessions_dir=self.sessions_dir)
        self.sessions = SessionManager(store=self.session_store)

        self.approval_store = FileApprovalStore(approvals_dir=self.approvals_dir)
        self.approval_service = ApprovalService(store=self.approval_store, two_man_rule_enabled=True)

        self.fake_s2 = DeterministicFakeSystem2()
        self.runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=self.sessions,
            approval_service=self.approval_service,
            system1_factory=lambda _: DeterministicFakeSystem1(),
            system2_factory=lambda **_: self.fake_s2,
            require_approval=True,
            two_man_rule_enabled=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_and_approve_request(
        self,
        requester: str = "user_requester",
        approver: str = "user_approver",
        target: str = "config.py",
        channel: str = "telegram",
        conversation_id: str = "conv_1",
    ) -> str:
        """Helper to create and approve a request through the runtime."""
        # 1. Requester triggers privileged operation -> pending approval
        req_msg = IncomingMessage(
            channel=channel,
            user_id=requester,
            conversation_id=conversation_id,
            text=f"write_files {target}",
        )
        res_req = self.runtime.process_message(req_msg)
        self.assertEqual(res_req.status, "pending_approval")
        req_id = res_req.metadata["request_id"]

        # 2. Approver approves
        approve_msg = IncomingMessage(
            channel=channel,
            user_id=approver,
            conversation_id=conversation_id,
            text=f"/approve {req_id}",
        )
        res_app = self.runtime.process_message(approve_msg)
        self.assertTrue(res_app.success, f"Peer approval failed: {res_app.text}")
        return req_id

    # -------------------------------------------------------------------------
    # Test 1 — Approved request becomes invalid after /reset
    # -------------------------------------------------------------------------
    def test_approved_request_becomes_invalid_after_reset(self) -> None:
        """Test 1: An approved request becomes completely unusable after /reset."""
        req_id = self._create_and_approve_request()

        # Execute /reset
        reset_msg = IncomingMessage(
            channel="telegram",
            user_id="user_requester",
            conversation_id="conv_1",
            text="/reset",
        )
        res_reset = self.runtime.process_message(reset_msg)
        self.assertTrue(res_reset.success)

        # Attempt to /exec old request after reset
        exec_msg = IncomingMessage(
            channel="telegram",
            user_id="user_requester",
            conversation_id="conv_1",
            text=f"/exec {req_id}",
        )
        res_exec = self.runtime.process_message(exec_msg)

        self.assertFalse(res_exec.success, "Execution of old approval must be DENIED after /reset")
        self.assertEqual(res_exec.status, "rejected")
        self.assertTrue(
            "cancelled" in res_exec.text.lower() or "mismatch" in res_exec.text.lower(),
            f"Expected cancellation or incarnation mismatch rejection, got: {res_exec.text}",
        )

    # -------------------------------------------------------------------------
    # Test 2 — Pending request becomes invalid after /reset
    # -------------------------------------------------------------------------
    def test_pending_request_becomes_invalid_after_reset(self) -> None:
        """Test 2: A pending request becomes unusable and cannot be executed after /reset."""
        req_msg = IncomingMessage(
            channel="telegram",
            user_id="user_requester",
            conversation_id="conv_1",
            text="write_files config.py",
        )
        res_req = self.runtime.process_message(req_msg)
        req_id = res_req.metadata["request_id"]

        # Reset session
        reset_msg = IncomingMessage(
            channel="telegram",
            user_id="user_requester",
            conversation_id="conv_1",
            text="/reset",
        )
        self.runtime.process_message(reset_msg)

        # Approver attempts to approve old request after reset -> should fail
        approve_msg = IncomingMessage(
            channel="telegram",
            user_id="user_approver",
            conversation_id="conv_1",
            text=f"/approve {req_id}",
        )
        res_app = self.runtime.process_message(approve_msg)
        self.assertFalse(res_app.success, "Cannot approve cancelled request after /reset")

        # Attempt /exec
        exec_msg = IncomingMessage(
            channel="telegram",
            user_id="user_requester",
            conversation_id="conv_1",
            text=f"/exec {req_id}",
        )
        res_exec = self.runtime.process_message(exec_msg)
        self.assertFalse(res_exec.success)

    # -------------------------------------------------------------------------
    # Test 3 — /new invalidates old approval
    # -------------------------------------------------------------------------
    def test_new_command_invalidates_old_approval(self) -> None:
        """Test 3: /new creates a fresh session incarnation and invalidates prior approvals."""
        req_id = self._create_and_approve_request()

        new_msg = IncomingMessage(
            channel="telegram",
            user_id="user_requester",
            conversation_id="conv_1",
            text="/new",
        )
        res_new = self.runtime.process_message(new_msg)
        self.assertTrue(res_new.success)

        # Attempt /exec old approval
        exec_msg = IncomingMessage(
            channel="telegram",
            user_id="user_requester",
            conversation_id="conv_1",
            text=f"/exec {req_id}",
        )
        res_exec = self.runtime.process_message(exec_msg)
        self.assertFalse(res_exec.success, "Old approval must not execute in incarnation created by /new")
        self.assertEqual(res_exec.status, "rejected")

    # -------------------------------------------------------------------------
    # Test 4 — Same deterministic session ID
    # -------------------------------------------------------------------------
    def test_same_deterministic_session_id_different_incarnation(self) -> None:
        """Test 4: Deterministic session_id remains constant while incarnation_id changes."""
        session_before = self.sessions.get_or_create("telegram", "user_alpha", "chat_1")
        old_session_id = session_before.session_id
        old_incarnation = session_before.session_incarnation_id

        # Reset session
        self.sessions.reset(old_session_id)

        session_after = self.sessions.get_or_create("telegram", "user_alpha", "chat_1")
        new_session_id = session_after.session_id
        new_incarnation = session_after.session_incarnation_id

        self.assertEqual(old_session_id, new_session_id, "session_id identity remains deterministic")
        self.assertNotEqual(old_incarnation, new_incarnation, "session_incarnation_id must be freshly generated")

    # -------------------------------------------------------------------------
    # Test 5 — Restart preserves incarnation
    # -------------------------------------------------------------------------
    def test_restart_preserves_incarnation(self) -> None:
        """Test 5: Process restart does NOT change the session incarnation."""
        session1 = self.sessions.get_or_create("telegram", "user_beta", "conv_beta")
        incarnation_initial = session1.session_incarnation_id
        self.sessions.save(session1)

        # Simulate process termination and restart with fresh Runtime and SessionManager
        new_session_store = FileSessionStore(sessions_dir=self.sessions_dir)
        new_session_mgr = SessionManager(store=new_session_store)

        session2 = new_session_mgr.get_or_create("telegram", "user_beta", "conv_beta")
        self.assertEqual(session2.session_incarnation_id, incarnation_initial, "Restart must preserve active incarnation")

    # -------------------------------------------------------------------------
    # Test 6 — Reset creates new incarnation
    # -------------------------------------------------------------------------
    def test_reset_creates_new_incarnation(self) -> None:
        """Test 6: Invoking reset() forces creation of a distinct new incarnation."""
        session = self.sessions.get_or_create("telegram", "user_gamma", "conv_gamma")
        inc_1 = session.session_incarnation_id

        session.reset()
        inc_2 = session.session_incarnation_id

        self.assertNotEqual(inc_1, inc_2)
        self.assertTrue(len(inc_2) >= 16)

    # -------------------------------------------------------------------------
    # Test 7 — Restart + old approval
    # -------------------------------------------------------------------------
    def test_restart_preserves_valid_approval(self) -> None:
        """Test 7: Approval remains valid across restart if session was NOT reset."""
        req_id = self._create_and_approve_request()

        # Simulate restart
        new_session_store = FileSessionStore(sessions_dir=self.sessions_dir)
        new_sessions = SessionManager(store=new_session_store)
        new_approval_store = FileApprovalStore(approvals_dir=self.approvals_dir)
        new_approval_service = ApprovalService(store=new_approval_store, two_man_rule_enabled=True)

        new_fake_s2 = DeterministicFakeSystem2()
        new_runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=new_sessions,
            approval_service=new_approval_service,
            system1_factory=lambda _: DeterministicFakeSystem1(),
            system2_factory=lambda **_: new_fake_s2,
            require_approval=True,
            two_man_rule_enabled=True,
        )

        exec_msg = IncomingMessage(
            channel="telegram",
            user_id="user_requester",
            conversation_id="conv_1",
            text=f"/exec {req_id}",
        )
        res_exec = new_runtime.process_message(exec_msg)
        self.assertTrue(res_exec.success, f"Approval must be valid after process restart without reset: {res_exec.error}")

    # -------------------------------------------------------------------------
    # Test 8 — Reset + restart + new session
    # -------------------------------------------------------------------------
    def test_reset_restart_new_session_denies_old_approval(self) -> None:
        """Test 8: Reset followed by restart and recreate session rejects old approval."""
        req_id = self._create_and_approve_request()

        # /reset in Runtime 1
        reset_msg = IncomingMessage(
            channel="telegram",
            user_id="user_requester",
            conversation_id="conv_1",
            text="/reset",
        )
        self.runtime.process_message(reset_msg)

        # Process restart
        new_session_store = FileSessionStore(sessions_dir=self.sessions_dir)
        new_sessions = SessionManager(store=new_session_store)
        new_approval_store = FileApprovalStore(approvals_dir=self.approvals_dir)
        new_approval_service = ApprovalService(store=new_approval_store, two_man_rule_enabled=True)

        new_runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=new_sessions,
            approval_service=new_approval_service,
            system1_factory=lambda _: DeterministicFakeSystem1(),
            system2_factory=lambda **_: DeterministicFakeSystem2(),
            require_approval=True,
            two_man_rule_enabled=True,
        )

        # Attempt to execute in Runtime 2
        exec_msg = IncomingMessage(
            channel="telegram",
            user_id="user_requester",
            conversation_id="conv_1",
            text=f"/exec {req_id}",
        )
        res_exec = new_runtime.process_message(exec_msg)
        self.assertFalse(res_exec.success, "Old approval must not execute across reset + restart boundary")
        self.assertEqual(res_exec.status, "rejected")

    # -------------------------------------------------------------------------
    # Test 9 — Approved-before-reset
    # -------------------------------------------------------------------------
    def test_approved_before_reset_denied(self) -> None:
        """Test 9: Request was fully APPROVED prior to /reset. /reset must cancel it."""
        req_id = self._create_and_approve_request()

        # Verify state is APPROVED before reset
        req_pre = self.approval_store.get(req_id)
        self.assertIsNotNone(req_pre)
        assert req_pre is not None
        self.assertEqual(req_pre.status, ApprovalStatus.APPROVED)

        # Issue /reset
        self.runtime.process_message(
            IncomingMessage(
                channel="telegram",
                user_id="user_requester",
                conversation_id="conv_1",
                text="/reset",
            )
        )

        # Verify state became CANCELLED on disk
        req_post = self.approval_store.get(req_id)
        self.assertIsNotNone(req_post)
        assert req_post is not None
        self.assertEqual(req_post.status, ApprovalStatus.CANCELLED)

    # -------------------------------------------------------------------------
    # Test 10 — Approve vs reset race
    # -------------------------------------------------------------------------
    def test_approve_vs_reset_race(self) -> None:
        """Test 10: Race between /approve and /reset leaves zero executable stale approvals."""
        for iteration in range(10):
            # Create a pending request
            req_msg = IncomingMessage(
                channel="telegram",
                user_id="user_requester",
                conversation_id=f"conv_race_{iteration}",
                text="write_files config.py",
            )
            res_req = self.runtime.process_message(req_msg)
            req_id = res_req.metadata["request_id"]

            barrier = threading.Barrier(2)

            def _approve_task() -> None:
                barrier.wait()
                self.runtime.process_message(
                    IncomingMessage(
                        channel="telegram",
                        user_id="user_approver",
                        conversation_id=f"conv_race_{iteration}",
                        text=f"/approve {req_id}",
                    )
                )

            def _reset_task() -> None:
                barrier.wait()
                self.runtime.process_message(
                    IncomingMessage(
                        channel="telegram",
                        user_id="user_requester",
                        conversation_id=f"conv_race_{iteration}",
                        text="/reset",
                    )
                )

            t1 = threading.Thread(target=_approve_task)
            t2 = threading.Thread(target=_reset_task)
            t1.start()
            t2.start()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

            # In either race outcome, the request must NOT be executable now under the current session
            exec_msg = IncomingMessage(
                channel="telegram",
                user_id="user_requester",
                conversation_id=f"conv_race_{iteration}",
                text=f"/exec {req_id}",
            )
            res_exec = self.runtime.process_message(exec_msg)
            self.assertFalse(
                res_exec.success,
                f"Iteration {iteration}: Approval must never be executable after reset completed",
            )

    # -------------------------------------------------------------------------
    # Test 11 — Reset vs exec race
    # -------------------------------------------------------------------------
    def test_reset_vs_exec_race(self) -> None:
        """Test 11: Race between /exec and /reset never allows stale execution in new incarnation."""
        for iteration in range(10):
            req_id = self._create_and_approve_request(
                conversation_id=f"conv_exec_race_{iteration}"
            )
            barrier = threading.Barrier(2)
            results = {}

            def _exec_task() -> None:
                barrier.wait()
                results["exec"] = self.runtime.process_message(
                    IncomingMessage(
                        channel="telegram",
                        user_id="user_requester",
                        conversation_id=f"conv_exec_race_{iteration}",
                        text=f"/exec {req_id}",
                    )
                )

            def _reset_task() -> None:
                barrier.wait()
                results["reset"] = self.runtime.process_message(
                    IncomingMessage(
                        channel="telegram",
                        user_id="user_requester",
                        conversation_id=f"conv_exec_race_{iteration}",
                        text="/reset",
                    )
                )

            t1 = threading.Thread(target=_exec_task)
            t2 = threading.Thread(target=_reset_task)
            t1.start()
            t2.start()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

            # Check post-race state
            exec_res = results.get("exec")
            if exec_res and exec_res.success:
                # Exec won before reset: request must be CONSUMED
                final_req = self.approval_store.get(req_id)
                self.assertIsNotNone(final_req)
                assert final_req is not None
                self.assertEqual(final_req.status, ApprovalStatus.CONSUMED)
            else:
                # Reset won or cancelled it: request must be CANCELLED or rejected
                final_req = self.approval_store.get(req_id)
                self.assertIsNotNone(final_req)
                assert final_req is not None
                self.assertIn(final_req.status, (ApprovalStatus.CANCELLED, ApprovalStatus.CONSUMED))

            # A subsequent attempt to /exec under the post-reset session must ALWAYS fail
            followup_exec = self.runtime.process_message(
                IncomingMessage(
                    channel="telegram",
                    user_id="user_requester",
                    conversation_id=f"conv_exec_race_{iteration}",
                    text=f"/exec {req_id}",
                )
            )
            self.assertFalse(followup_exec.success, "Subsequent exec must always fail")

    # -------------------------------------------------------------------------
    # Test 12 — Cross-user
    # -------------------------------------------------------------------------
    def test_cross_user_execution_rejected(self) -> None:
        """Test 12: User B cannot execute an approval created by User A."""
        req_id = self._create_and_approve_request(requester="user_a", approver="user_peer")

        # User B attempts to execute it
        msg_b = IncomingMessage(
            channel="telegram",
            user_id="user_b",
            conversation_id="conv_1",
            text=f"/exec {req_id}",
        )
        res_b = self.runtime.process_message(msg_b)
        self.assertFalse(res_b.success)
        self.assertIn("mismatch", res_b.text.lower())

    # -------------------------------------------------------------------------
    # Test 13 — Cross-channel
    # -------------------------------------------------------------------------
    def test_cross_channel_execution_rejected(self) -> None:
        """Test 13: An approval created in Telegram cannot be executed in WhatsApp."""
        req_id = self._create_and_approve_request(channel="telegram")

        msg_wa = IncomingMessage(
            channel="whatsapp",
            user_id="user_requester",
            conversation_id="conv_1",
            text=f"/exec {req_id}",
        )
        res_wa = self.runtime.process_message(msg_wa)
        self.assertFalse(res_wa.success)
        self.assertIn("mismatch", res_wa.text.lower())

    # -------------------------------------------------------------------------
    # Test 14 — Legacy approval
    # -------------------------------------------------------------------------
    def test_legacy_approval_without_incarnation_rejected(self) -> None:
        """Test 14: An approval missing session_incarnation_id cannot be executed."""
        op = CanonicalOperation(action_type="write_files", target="config.py")
        # Legacy request without session_incarnation_id
        legacy_req = ApprovalRequest(
            request_id="req_legacy_001",
            session_id="telegram:user_req:conv_1",
            channel="telegram",
            user_id="user_req",
            conversation_id="conv_1",
            operation_type="write_files",
            canonical_operation=op,
            operation_digest=op.compute_digest(),
            risk_class="MEDIUM",
            created_at=time.time(),
            expires_at=time.time() + 300,
            nonce="0123456789abcdef",
            status=ApprovalStatus.APPROVED,
            session_incarnation_id=None,  # Legacy
        )
        self.approval_store.save(legacy_req)

        # Attempt to execute through runtime
        exec_msg = IncomingMessage(
            channel="telegram",
            user_id="user_req",
            conversation_id="conv_1",
            text="/exec req_legacy_001",
        )
        res_exec = self.runtime.process_message(exec_msg)
        self.assertFalse(res_exec.success)
        self.assertIn("lacks session incarnation binding", res_exec.text)

    # -------------------------------------------------------------------------
    # Test 15 — Multiple outstanding approvals
    # -------------------------------------------------------------------------
    def test_multiple_outstanding_approvals_invalidated(self) -> None:
        """Test 15: Resetting Session 1 invalidates all its approvals without touching Session 2."""
        # Create 3 approvals for Session 1
        req1 = self._create_and_approve_request(requester="user_s1", approver="admin", conversation_id="conv_s1", target="file1.py")
        req2 = self._create_and_approve_request(requester="user_s1", approver="admin", conversation_id="conv_s1", target="file2.py")
        req3 = self._create_and_approve_request(requester="user_s1", approver="admin", conversation_id="conv_s1", target="file3.py")

        # Create 1 approval for Session 2
        req_other = self._create_and_approve_request(requester="user_s2", approver="admin", conversation_id="conv_s2", target="file_other.py")

        # Reset Session 1
        reset_msg = IncomingMessage(
            channel="telegram",
            user_id="user_s1",
            conversation_id="conv_s1",
            text="/reset",
        )
        self.runtime.process_message(reset_msg)

        # Assert all 3 in Session 1 are CANCELLED
        for r_id in (req1, req2, req3):
            r = self.approval_store.get(r_id)
            self.assertIsNotNone(r)
            assert r is not None
            self.assertEqual(r.status, ApprovalStatus.CANCELLED)

        # Assert Session 2 request is still APPROVED and untouched
        r_s2 = self.approval_store.get(req_other)
        self.assertIsNotNone(r_s2)
        assert r_s2 is not None
        self.assertEqual(r_s2.status, ApprovalStatus.APPROVED)

    # -------------------------------------------------------------------------
    # Test 16 — True End-to-End Requirement (Section 23)
    # -------------------------------------------------------------------------
    def test_true_end_to_end_orchestrator_not_executed_on_stale_approval(self) -> None:
        """Section 23: Complete flow verifying orchestrator is never invoked for stale approvals."""
        # 1. IncomingMessage triggers approval creation
        req_msg = IncomingMessage(
            channel="telegram",
            user_id="user_requester",
            conversation_id="conv_e2e",
            text="write_files sensitive.py",
        )
        res_req = self.runtime.process_message(req_msg)
        self.assertEqual(res_req.status, "pending_approval")
        req_id = res_req.metadata["request_id"]

        # 2. Peer approves via /approve
        app_msg = IncomingMessage(
            channel="telegram",
            user_id="peer_approver",
            conversation_id="conv_e2e",
            text=f"/approve {req_id}",
        )
        res_app = self.runtime.process_message(app_msg)
        self.assertTrue(res_app.success)

        # 3. User resets session via /reset
        res_reset = self.runtime.process_message(
            IncomingMessage(
                channel="telegram",
                user_id="user_requester",
                conversation_id="conv_e2e",
                text="/reset",
            )
        )
        self.assertTrue(res_reset.success)

        # 4. User attempts /exec <old_request_id> in new session
        initial_s2_calls = self.fake_s2.call_count
        res_exec = self.runtime.process_message(
            IncomingMessage(
                channel="telegram",
                user_id="user_requester",
                conversation_id="conv_e2e",
                text=f"/exec {req_id}",
            )
        )

        # Assertions
        self.assertFalse(res_exec.success, "Stale execution must be denied")
        self.assertEqual(res_exec.status, "rejected")
        self.assertEqual(self.fake_s2.call_count, initial_s2_calls, "Canonical orchestrator / System 2 MUST NOT be executed")


if __name__ == "__main__":
    unittest.main()
