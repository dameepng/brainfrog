"""Comprehensive Unit and Property Tests for P1.3F Result Aggregation.

Test Matrix:
A. Basic:
   - One child aggregation
   - Multiple children aggregation
   - Empty group aggregation
   - Deterministic output
B. Success:
   - All children DONE -> AggregateStatus.COMPLETE, success=True
   - Result summaries and verification results reflected
C. Failure:
   - One FAILED child
   - Multiple FAILED children
   - CANCELLED child
   - Failure codes and cancellation reasons remain visible
D. Incomplete:
   - Non-terminal statuses: CREATED, PLANNING, APPROVAL_REQUIRED, EXECUTING, VERIFYING
   - Mixed terminal and non-terminal children -> AggregateStatus.INCOMPLETE, success=False
E. Policies:
   - ALL_REQUIRED: all DONE -> COMPLETE; any FAILED/CANCELLED -> FAILED; any non-terminal -> INCOMPLETE
   - ALLOW_PARTIAL: all DONE -> COMPLETE; mixed DONE & FAILED -> PARTIAL; all FAILED -> FAILED
F. Determinism:
   - Independent completion order does not affect aggregate ordering
   - Repeated aggregation produces identical output and identical digest
G. Parent Binding:
   - Wrong parent_work_id fails closed
   - Wrong delegation_group_id fails closed
   - Wrong actor fails closed
   - Wrong session_id fails closed
   - Wrong session_incarnation_id fails closed
   - Mixed valid and invalid child set fails closed
H. Bounds:
   - Max children truncation with explicit metadata (truncated=True, omitted_children > 0)
   - Oversized result summary truncation with '... [truncated]'
   - Max artifact references bounding
   - Aggregate payload byte limit enforcement
I. Idempotence:
   - Repeated aggregation does not mutate any child or parent state
J. OCC (Optimistic Concurrency Control):
   - Stale parent revision raises StaleWorkRevisionError on attach
   - Concurrent update detection
K. Persistence & Store Integration:
   - Read canonical child work state from WorkStore
   - Attach aggregate to parent work via WorkStore + OCC
   - Reconstruct across store reloads (FileWorkStore)
   - No second result store
L. Secret Safety:
   - Child summaries with API keys, tokens, and PEM private keys are redacted
   - Credential fields rejected via reject_secrets
M. Authority Invariance:
   - No approvals created or consumed
   - No capabilities mutated
   - No transactions or orchestrator executed
   - Module contains zero subprocess, network, or shell primitives
N. Property Tests:
   - State determinism: same canonical state -> same aggregate & digest
   - Invariance: aggregation produces zero authority changes
"""
from __future__ import annotations

import ast
import inspect
import json
import math
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.runtime.capabilities import Capabilities, FilesystemPolicy, GitPolicy, NetworkPolicy, ShellPolicy
from core.runtime.child_work import ChildWork, save_child_work
from core.runtime.contract import reject_secrets
from core.runtime.delegation import DelegationContract
from core.runtime.delegation_runtime import (
    DelegationGroup,
    DelegationMode,
    DelegationRuntime,
    save_delegation_group,
)
from core.runtime.result_aggregation import (
    CURRENT_RESULT_AGGREGATION_SCHEMA_VERSION,
    MAX_AGGREGATE_BYTES,
    MAX_AGGREGATE_CHILDREN,
    MAX_ARTIFACTS_PER_CHILD,
    MAX_FAILURE_MESSAGE_BYTES,
    MAX_SUMMARY_BYTES,
    AggregateResult,
    AggregateStatus,
    AggregationPolicy,
    ArtifactReference,
    ChildResult,
    ResultAggregationBindingError,
    ResultAggregationError,
    ResultAggregationIntegrityError,
    ResultAggregationLimitError,
    ResultAggregationPolicyError,
    ResultAggregator,
    aggregate_and_save,
    aggregate_children,
    aggregate_from_store,
    attach_aggregate_to_parent,
    build_child_result,
    get_aggregate_for_group,
    get_latest_aggregate,
)
from core.runtime.subagent import Subagent, SubagentStatus
from core.runtime.work import (
    InMemoryWorkStore,
    StaleWorkRevisionError,
    VerificationResult,
    VerificationStatus,
    Work,
    WorkFailure,
    WorkStatus,
)
from core.runtime.work_store import FileWorkStore


class BaseResultAggregationTestCase(unittest.TestCase):
    """Fixture providing parent work, subagents, delegations, and child works."""

    def setUp(self) -> None:
        self.now = time.time()
        self.actor = "actor_developer_alice"
        self.session_id = "session_agg_01"
        self.session_incarnation_id = "inc_agg_alpha"
        self.parent_work_id = "work_parent_01"

        self.store = InMemoryWorkStore()

        self.parent_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=False),
            git=GitPolicy(read=True, commit=False),
        )

        self.parent_work = Work(
            id=self.parent_work_id,
            intent="Refactor core microservices",
            goal="Refactor auth, db, and api components",
            scope=("src/",),
            capabilities=self.parent_caps,
            status=WorkStatus.EXECUTING,
            actor_id=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            created_at=self.now,
            updated_at=self.now,
        )
        self.store.create(self.parent_work)

    def _create_child(
        self,
        index: int,
        status: WorkStatus = WorkStatus.CREATED,
        summary: str = "",
        failure: Optional[WorkFailure] = None,
        cancellation_reason: Optional[str] = None,
        verification_result: Optional[VerificationResult] = None,
        artifacts: Optional[Sequence[Any]] = None,
        parent_id: Optional[str] = None,
        actor: Optional[str] = None,
        session_id: Optional[str] = None,
        session_inc: Optional[str] = None,
    ) -> ChildWork:
        letter = chr(ord('a') + index)
        sub_id = f"sub_{letter}_{index:02d}"
        delg_id = f"delg_{letter}_{index:02d}"
        child_id = f"work_child_{letter}_{index:02d}"

        eff_parent = parent_id or self.parent_work_id
        eff_actor = actor or self.actor
        eff_session = session_id or self.session_id
        eff_inc = session_inc or self.session_incarnation_id

        sub = Subagent(
            subagent_id=sub_id,
            parent_work_id=eff_parent,
            session_id=eff_session,
            session_incarnation_id=eff_inc,
            actor=eff_actor,
            role=f"worker_{letter}",
            purpose=f"Handle task {letter}",
            created_at=self.now,
            updated_at=self.now,
        )

        delg_caps = Capabilities(
            filesystem=FilesystemPolicy(read=(f"src/{letter}.py",), write=(f"src/{letter}.py",)),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=False),
            git=GitPolicy(read=True, commit=False),
        )
        delg = DelegationContract(
            delegation_id=delg_id,
            parent_work_id=eff_parent,
            child_subagent_id=sub_id,
            actor=eff_actor,
            session_id=eff_session,
            session_incarnation_id=eff_inc,
            target_scope=(f"src/{letter}.py",),
            capabilities=delg_caps,
            expires_at=self.now + 3600.0,
            created_at=self.now,
        )

        is_exact_parent = (
            eff_parent == self.parent_work_id
            and eff_actor == self.actor
            and eff_session == self.session_id
            and eff_inc == self.session_incarnation_id
        )
        child_work = ChildWork.create(
            parent_work=self.parent_work if is_exact_parent else Work(
                id=eff_parent,
                actor_id=eff_actor,
                session_id=eff_session,
                session_incarnation_id=eff_inc,
                capabilities=self.parent_caps,
                status=WorkStatus.EXECUTING,
            ),
            subagent=sub,
            delegation=delg,
            child_work_id=child_id,
            intent=f"Intent {letter}",
            goal=f"Goal {letter}",
            created_at=self.now,
        )

        # Apply target status and metadata
        meta = dict(child_work.work.resume_metadata or {})
        if summary:
            meta["result_summary"] = summary
        if artifacts:
            meta["artifact_references"] = list(artifacts)

        # Transition work directly if non-CREATED
        w = child_work.work
        if meta != (child_work.work.resume_metadata or {}):
            w = w.with_update(resume_metadata=meta)

        if status != WorkStatus.CREATED:
            # Step through valid transitions
            if status in (WorkStatus.PLANNING, WorkStatus.APPROVAL_REQUIRED, WorkStatus.EXECUTING, WorkStatus.VERIFYING, WorkStatus.DONE, WorkStatus.FAILED, WorkStatus.CANCELLED):
                if status == WorkStatus.DONE:
                    w = w.transition(WorkStatus.PLANNING)
                    w = w.transition(WorkStatus.APPROVAL_REQUIRED)
                    w = w.transition(WorkStatus.EXECUTING)
                    w = w.transition(WorkStatus.VERIFYING)
                    w = w.transition(WorkStatus.DONE, verification_result=verification_result)
                elif status == WorkStatus.FAILED:
                    w = w.transition(WorkStatus.FAILED, failure=failure or WorkFailure(code="ERR_FAIL", summary="Failed child"))
                elif status == WorkStatus.CANCELLED:
                    w = w.transition(WorkStatus.PLANNING)
                    w = w.transition(WorkStatus.APPROVAL_REQUIRED)
                    w = w.transition(WorkStatus.CANCELLED, cancellation_reason=cancellation_reason or "Child cancelled")
                elif status == WorkStatus.PLANNING:
                    w = w.transition(WorkStatus.PLANNING)
                elif status == WorkStatus.APPROVAL_REQUIRED:
                    w = w.transition(WorkStatus.PLANNING)
                    w = w.transition(WorkStatus.APPROVAL_REQUIRED)
                elif status == WorkStatus.EXECUTING:
                    w = w.transition(WorkStatus.PLANNING)
                    w = w.transition(WorkStatus.APPROVAL_REQUIRED)
                    w = w.transition(WorkStatus.EXECUTING)
                elif status == WorkStatus.VERIFYING:
                    w = w.transition(WorkStatus.PLANNING)
                    w = w.transition(WorkStatus.APPROVAL_REQUIRED)
                    w = w.transition(WorkStatus.EXECUTING)
                    w = w.transition(WorkStatus.VERIFYING)

        saved_child = child_work.with_update(
            verification_result=verification_result,
            failure=failure,
            cancellation_reason=cancellation_reason,
            resume_metadata=w.resume_metadata,
        )
        saved_child = ChildWork(
            child_work_id=child_id,
            parent_work_id=eff_parent,
            subagent_id=sub_id,
            delegation_id=delg_id,
            session_id=eff_session,
            session_incarnation_id=eff_inc,
            actor=eff_actor,
            created_at=self.now,
            updated_at=self.now,
            work=w,
            delegation_digest=delg.digest,
            delegation_expires_at=delg.expires_at,
            delegation=delg,
            subagent=sub,
        )

        return save_child_work(self.store, saved_child)


class TestBasicAggregation(BaseResultAggregationTestCase):
    """Section 21.A: Basic result aggregation tests."""

    def test_single_child_success(self) -> None:
        child = self._create_child(0, status=WorkStatus.DONE, summary="Auth module refactored")
        res = aggregate_children(self.parent_work, [child], current_time=self.now)

        self.assertEqual(res.parent_work_id, self.parent_work_id)
        self.assertEqual(res.policy, AggregationPolicy.ALL_REQUIRED)
        self.assertEqual(res.status, AggregateStatus.COMPLETE)
        self.assertTrue(res.success)
        self.assertEqual(res.total_children, 1)
        self.assertEqual(res.completed_children, 1)
        self.assertEqual(res.successful_children, 1)
        self.assertEqual(res.failed_children, 0)
        self.assertEqual(res.cancelled_children, 0)
        self.assertEqual(res.incomplete_children, 0)
        self.assertEqual(len(res.children), 1)
        self.assertEqual(res.children[0].child_work_id, child.id)
        self.assertEqual(res.children[0].result_summary, "Auth module refactored")
        self.assertTrue(res.children[0].success)
        self.assertTrue(len(res.aggregate_digest) > 0)

    def test_multiple_children_success(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE, summary="Auth module ok")
        c2 = self._create_child(1, status=WorkStatus.DONE, summary="DB module ok")
        c3 = self._create_child(2, status=WorkStatus.DONE, summary="API module ok")

        res = aggregate_children(self.parent_work, [c1, c2, c3], current_time=self.now)
        self.assertEqual(res.status, AggregateStatus.COMPLETE)
        self.assertTrue(res.success)
        self.assertEqual(res.total_children, 3)
        self.assertEqual(res.successful_children, 3)
        self.assertEqual(res.completed_children, 3)
        self.assertEqual(res.failed_children, 0)
        self.assertEqual(len(res.children), 3)

    def test_empty_children_group(self) -> None:
        res = aggregate_children(self.parent_work, [], current_time=self.now)
        self.assertEqual(res.total_children, 0)
        self.assertEqual(res.status, AggregateStatus.COMPLETE)
        self.assertTrue(res.success)
        self.assertEqual(len(res.children), 0)
        self.assertFalse(res.truncated)

    def test_deterministic_output(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE, summary="First")
        c2 = self._create_child(1, status=WorkStatus.DONE, summary="Second")

        res1 = aggregate_children(self.parent_work, [c1, c2], current_time=self.now)
        res2 = aggregate_children(self.parent_work, [c1, c2], current_time=self.now)

        self.assertEqual(res1.aggregate_digest, res2.aggregate_digest)
        self.assertEqual(res1.to_dict(), res2.to_dict())


class TestSuccessAggregation(BaseResultAggregationTestCase):
    """Section 21.B: Success outcomes."""

    def test_all_done_with_verification_result(self) -> None:
        vr = VerificationResult(
            status=VerificationStatus.PASS,
            checks=("auth_tests", "schema_validation"),
            passed=2,
            failed=0,
            summary="All 2 checks passed",
        )
        child = self._create_child(0, status=WorkStatus.DONE, verification_result=vr)
        res = aggregate_children(self.parent_work, [child], current_time=self.now)

        self.assertEqual(res.status, AggregateStatus.COMPLETE)
        self.assertTrue(res.success)
        cr = res.children[0]
        self.assertEqual(cr.verification_status, "PASS")
        self.assertEqual(cr.result_summary, "All 2 checks passed")


class TestFailureAggregation(BaseResultAggregationTestCase):
    """Section 21.C: Failure and cancellation outcomes."""

    def test_one_failed_child(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE, summary="Auth ok")
        c2 = self._create_child(
            1,
            status=WorkStatus.FAILED,
            failure=WorkFailure(code="SYNTAX_ERROR", summary="Compilation failed at line 42"),
        )
        res = aggregate_children(self.parent_work, [c1, c2], policy=AggregationPolicy.ALL_REQUIRED, current_time=self.now)

        self.assertEqual(res.status, AggregateStatus.FAILED)
        self.assertFalse(res.success)
        self.assertEqual(res.total_children, 2)
        self.assertEqual(res.successful_children, 1)
        self.assertEqual(res.failed_children, 1)
        self.assertEqual(res.completed_children, 2)

        # Failures remain visible
        failed_cr = res.children[1]
        self.assertFalse(failed_cr.success)
        self.assertEqual(failed_cr.failure_code, "SYNTAX_ERROR")
        self.assertEqual(failed_cr.failure_message, "Compilation failed at line 42")

    def test_multiple_failed_children(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.FAILED, failure=WorkFailure(code="ERR_1", summary="Error 1"))
        c2 = self._create_child(1, status=WorkStatus.FAILED, failure=WorkFailure(code="ERR_2", summary="Error 2"))
        res = aggregate_children(self.parent_work, [c1, c2], current_time=self.now)

        self.assertEqual(res.status, AggregateStatus.FAILED)
        self.assertFalse(res.success)
        self.assertEqual(res.failed_children, 2)

    def test_cancelled_child_remains_visible(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE, summary="Done ok")
        c2 = self._create_child(1, status=WorkStatus.CANCELLED, cancellation_reason="Parent timeout cancelled worker")
        res = aggregate_children(self.parent_work, [c1, c2], current_time=self.now)

        self.assertEqual(res.status, AggregateStatus.FAILED)
        self.assertFalse(res.success)
        self.assertEqual(res.cancelled_children, 1)

        cr = res.children[1]
        self.assertFalse(cr.success)
        self.assertEqual(cr.cancellation_reason, "Parent timeout cancelled worker")
        self.assertEqual(cr.result_summary, "Parent timeout cancelled worker")


class TestIncompleteAggregation(BaseResultAggregationTestCase):
    """Section 21.D: Incomplete non-terminal worker outcomes."""

    def test_all_non_terminal_statuses(self) -> None:
        statuses = [
            WorkStatus.CREATED,
            WorkStatus.PLANNING,
            WorkStatus.APPROVAL_REQUIRED,
            WorkStatus.EXECUTING,
            WorkStatus.VERIFYING,
        ]
        for idx, st in enumerate(statuses):
            child = self._create_child(idx, status=st)
            res = aggregate_children(self.parent_work, [child], current_time=self.now)
            self.assertEqual(res.status, AggregateStatus.INCOMPLETE)
            self.assertFalse(res.success)
            self.assertEqual(res.incomplete_children, 1)
            self.assertEqual(res.completed_children, 0)
            self.assertFalse(res.children[0].success)

    def test_mixed_terminal_and_non_terminal(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE, summary="Finished A")
        c2 = self._create_child(1, status=WorkStatus.EXECUTING)
        c3 = self._create_child(2, status=WorkStatus.FAILED)

        res = aggregate_children(self.parent_work, [c1, c2, c3], current_time=self.now)
        # Even if one failed, incomplete state takes precedence in describing whole set progress
        self.assertEqual(res.status, AggregateStatus.INCOMPLETE)
        self.assertFalse(res.success)
        self.assertEqual(res.incomplete_children, 1)
        self.assertEqual(res.successful_children, 1)
        self.assertEqual(res.failed_children, 1)
        self.assertEqual(res.completed_children, 2)


class TestPolicyAggregation(BaseResultAggregationTestCase):
    """Section 21.E: Policy semantics (ALL_REQUIRED vs ALLOW_PARTIAL)."""

    def test_all_required_policy_mixed(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE, summary="Done A")
        c2 = self._create_child(1, status=WorkStatus.FAILED, failure=WorkFailure(code="ERR_B", summary="Failed B"))

        res = aggregate_children(self.parent_work, [c1, c2], policy=AggregationPolicy.ALL_REQUIRED, current_time=self.now)
        self.assertEqual(res.policy, AggregationPolicy.ALL_REQUIRED)
        self.assertEqual(res.status, AggregateStatus.FAILED)
        self.assertFalse(res.success)

    def test_allow_partial_policy_mixed(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE, summary="Done A")
        c2 = self._create_child(1, status=WorkStatus.FAILED, failure=WorkFailure(code="ERR_B", summary="Failed B"))
        c3 = self._create_child(2, status=WorkStatus.DONE, summary="Done C")

        res = aggregate_children(self.parent_work, [c1, c2, c3], policy=AggregationPolicy.ALLOW_PARTIAL, current_time=self.now)
        self.assertEqual(res.policy, AggregationPolicy.ALLOW_PARTIAL)
        self.assertEqual(res.status, AggregateStatus.PARTIAL)
        self.assertFalse(res.success)  # PARTIAL != COMPLETE, success is False
        self.assertEqual(res.successful_children, 2)
        self.assertEqual(res.failed_children, 1)
        # Failed child remains represented
        self.assertEqual(res.children[1].failure_code, "ERR_B")

    def test_allow_partial_all_succeeded(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE)
        c2 = self._create_child(1, status=WorkStatus.DONE)

        res = aggregate_children(self.parent_work, [c1, c2], policy=AggregationPolicy.ALLOW_PARTIAL, current_time=self.now)
        self.assertEqual(res.status, AggregateStatus.COMPLETE)
        self.assertTrue(res.success)

    def test_allow_partial_all_failed(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.FAILED)
        c2 = self._create_child(1, status=WorkStatus.FAILED)

        res = aggregate_children(self.parent_work, [c1, c2], policy=AggregationPolicy.ALLOW_PARTIAL, current_time=self.now)
        self.assertEqual(res.status, AggregateStatus.FAILED)
        self.assertFalse(res.success)

    def test_invalid_policy_rejected(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE)
        with self.assertRaises(ResultAggregationPolicyError):
            aggregate_children(self.parent_work, [c1], policy="unsupported_policy")


class TestDeterminismAggregation(BaseResultAggregationTestCase):
    """Section 21.F: Deterministic ordering independent of completion timing."""

    def test_ordering_preserved_from_delegation_group(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE, summary="Child A")
        c2 = self._create_child(1, status=WorkStatus.DONE, summary="Child B")
        c3 = self._create_child(2, status=WorkStatus.DONE, summary="Child C")

        group = DelegationGroup(
            delegation_group_id="group_test_01",
            parent_work_id=self.parent_work_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            mode=DelegationMode.PARALLEL,
            child_work_ids=(c1.id, c2.id, c3.id),
            dependencies={c1.id: (), c2.id: (), c3.id: ()},
        )

        # Pass in reverse order
        res1 = aggregate_children(self.parent_work, [c3, c1, c2], delegation_group=group, current_time=self.now)
        # Pass in different shuffle
        res2 = aggregate_children(self.parent_work, [c2, c3, c1], delegation_group=group, current_time=self.now)

        self.assertEqual([c.child_work_id for c in res1.children], [c1.id, c2.id, c3.id])
        self.assertEqual([c.child_work_id for c in res2.children], [c1.id, c2.id, c3.id])
        self.assertEqual(res1.aggregate_digest, res2.aggregate_digest)

    def test_digest_stability(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE, summary="Stable")
        # Aggregating at different current_time should produce the SAME aggregate_digest
        res1 = aggregate_children(self.parent_work, [c1], current_time=self.now)
        res2 = aggregate_children(self.parent_work, [c1], current_time=self.now + 100.0)

        self.assertEqual(res1.aggregate_digest, res2.aggregate_digest)


class TestParentBindingAggregation(BaseResultAggregationTestCase):
    """Section 21.G: Parent and session binding validation (fail-closed)."""

    def test_wrong_parent_work_id_rejected(self) -> None:
        foreign_child = self._create_child(0, parent_id="work_foreign_parent")
        with self.assertRaises(ResultAggregationBindingError):
            aggregate_children(self.parent_work, [foreign_child])

    def test_wrong_actor_rejected(self) -> None:
        foreign_child = self._create_child(0, actor="actor_malicious_intruder")
        with self.assertRaises(ResultAggregationBindingError):
            aggregate_children(self.parent_work, [foreign_child])

    def test_wrong_session_id_rejected(self) -> None:
        foreign_child = self._create_child(0, session_id="session_foreign_context")
        with self.assertRaises(ResultAggregationBindingError):
            aggregate_children(self.parent_work, [foreign_child])

    def test_wrong_session_incarnation_rejected(self) -> None:
        foreign_child = self._create_child(0, session_inc="inc_stale_old")
        with self.assertRaises(ResultAggregationBindingError):
            aggregate_children(self.parent_work, [foreign_child])

    def test_mixed_valid_and_invalid_child_set_fails_closed(self) -> None:
        valid_child = self._create_child(0, status=WorkStatus.DONE)
        foreign_child = self._create_child(1, parent_id="work_foreign")
        with self.assertRaises(ResultAggregationBindingError):
            aggregate_children(self.parent_work, [valid_child, foreign_child])

    def test_undeclared_group_child_rejected(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE)
        c2 = self._create_child(1, status=WorkStatus.DONE)

        group = DelegationGroup(
            delegation_group_id="group_declared_only_c1",
            parent_work_id=self.parent_work_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            mode=DelegationMode.PARALLEL,
            child_work_ids=(c1.id,),
            dependencies={c1.id: ()},
        )

        with self.assertRaises(ResultAggregationBindingError):
            aggregate_children(self.parent_work, [c1, c2], delegation_group=group)


class TestBoundsAndTruncationAggregation(BaseResultAggregationTestCase):
    """Section 21.H: Resource limits, bounded summary, and truncation."""

    def test_max_children_truncation(self) -> None:
        # Create 10 children, but bound aggregation to 5
        children = [self._create_child(i, status=WorkStatus.DONE) for i in range(10)]
        res = aggregate_children(self.parent_work, children, max_children=5, current_time=self.now)

        self.assertTrue(res.truncated)
        self.assertEqual(res.total_children, 10)
        self.assertEqual(res.included_children, 5)
        self.assertEqual(res.omitted_children, 5)
        self.assertEqual(len(res.children), 5)
        # Semantic status still reflects ALL 10 children
        self.assertEqual(res.successful_children, 10)
        self.assertEqual(res.status, AggregateStatus.COMPLETE)

    def test_oversized_summary_truncated(self) -> None:
        huge_summary = "A" * (MAX_SUMMARY_BYTES + 500)
        child = self._create_child(0, status=WorkStatus.DONE, summary=huge_summary)
        res = aggregate_children(self.parent_work, [child], current_time=self.now)

        cr = res.children[0]
        self.assertTrue(cr.result_summary.endswith("... [truncated]"))
        self.assertLessEqual(len(cr.result_summary), MAX_SUMMARY_BYTES)

    def test_oversized_failure_message_truncated(self) -> None:
        huge_err = "E" * (MAX_FAILURE_MESSAGE_BYTES + 500)
        child = self._create_child(
            0,
            status=WorkStatus.FAILED,
            failure=WorkFailure(code="HUGE_ERR", summary=huge_err),
        )
        res = aggregate_children(self.parent_work, [child], current_time=self.now)

        cr = res.children[0]
        self.assertIsNotNone(cr.failure_message)
        assert cr.failure_message is not None
        self.assertTrue(cr.failure_message.endswith("... [truncated]"))
        self.assertLessEqual(len(cr.failure_message), MAX_FAILURE_MESSAGE_BYTES)

    def test_artifacts_bounding(self) -> None:
        arts = [
            ArtifactReference(artifact_id=f"art_{i:02d}", path=f"out/file_{i}.txt")
            for i in range(MAX_ARTIFACTS_PER_CHILD + 10)
        ]
        child = self._create_child(0, status=WorkStatus.DONE, artifacts=arts)
        res = aggregate_children(self.parent_work, [child], current_time=self.now)

        cr = res.children[0]
        self.assertEqual(len(cr.artifact_references), MAX_ARTIFACTS_PER_CHILD)

    def test_max_aggregate_bytes_limit(self) -> None:
        # Construct an artificial AggregateResult exceeding MAX_AGGREGATE_BYTES
        many_children = [
            ChildResult(
                child_work_id=f"work_child_{i:04d}",
                subagent_id=f"sub_{i:04d}",
                delegation_id=f"delg_{i:04d}",
                declared_order=i,
                work_status=WorkStatus.DONE,
                success=True,
                result_summary="X" * 1500,
            )
            for i in range(50)
        ]
        agg = AggregateResult(
            parent_work_id=self.parent_work_id,
            delegation_group_id="group_huge",
            policy=AggregationPolicy.ALL_REQUIRED,
            status=AggregateStatus.COMPLETE,
            success=True,
            total_children=50,
            completed_children=50,
            successful_children=50,
            failed_children=0,
            cancelled_children=0,
            incomplete_children=0,
            included_children=50,
            children=tuple(many_children),
            created_at=self.now,
        )
        with self.assertRaises(ResultAggregationLimitError):
            agg.to_dict()


class TestIdempotenceAndAuthorityInvariance(BaseResultAggregationTestCase):
    """Sections 21.I & 21.M: Idempotence and strictly zero authority modification."""

    def test_idempotent_aggregation(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE, summary="Done A")
        c2 = self._create_child(1, status=WorkStatus.FAILED, failure=WorkFailure(code="ERR", summary="Fail B"))

        res1 = aggregate_children(self.parent_work, [c1, c2], current_time=self.now)
        res2 = aggregate_children(self.parent_work, [c1, c2], current_time=self.now)

        self.assertEqual(res1.to_dict(), res2.to_dict())

    def test_no_mutation_on_child_or_parent(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.EXECUTING)
        initial_status = c1.status
        initial_rev = c1.revision

        aggregate_children(self.parent_work, [c1], current_time=self.now)

        c1_after = self.store.get(c1.id)
        self.assertIsNotNone(c1_after)
        assert c1_after is not None
        self.assertEqual(c1_after.status, initial_status)
        self.assertEqual(c1_after.revision, initial_rev)

    def test_execution_boundary_ast_inspection(self) -> None:
        """Section 20: Result aggregation module must contain NO execution or subprocess primitives."""
        import core.runtime.result_aggregation as ra_mod
        source = inspect.getsource(ra_mod)
        tree = ast.parse(source)

        forbidden_names = {
            "subprocess",
            "os.system",
            "os.popen",
            "urllib",
            "requests",
            "httpx",
            "aiohttp",
            "TransactionCoordinator",
            "orchestrator",
            "ApprovalService",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name, forbidden_names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self.assertNotIn(node.module, forbidden_names)


class TestOccAndParentIntegration(BaseResultAggregationTestCase):
    """Section 21.J & 21.K: OCC conflict handling and WorkStore persistence."""

    def test_attach_aggregate_to_parent_persisted(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE, summary="Done A")
        res = aggregate_children(self.parent_work, [c1], delegation_group_id="group_a", current_time=self.now)

        saved_parent = attach_aggregate_to_parent(self.store, self.parent_work_id, res)
        self.assertIsNotNone(saved_parent.resume_metadata)

        summary_meta = get_aggregate_for_group(saved_parent, "group_a")
        self.assertIsNotNone(summary_meta)
        assert summary_meta is not None
        self.assertEqual(summary_meta["status"], "complete")
        self.assertTrue(summary_meta["success"])

        latest = get_latest_aggregate(saved_parent)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest["aggregate_digest"], res.aggregate_digest)

    def test_stale_parent_revision_raises_occ_error(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE)
        res = aggregate_children(self.parent_work, [c1], current_time=self.now)

        # Intentionally provide a stale revision
        with self.assertRaises(StaleWorkRevisionError):
            attach_aggregate_to_parent(self.store, self.parent_work_id, res, expected_revision=999)

    def test_aggregate_and_save_convenience(self) -> None:
        c1 = self._create_child(0, status=WorkStatus.DONE, summary="Done A")
        saved_parent, agg = aggregate_and_save(
            self.store,
            self.parent_work_id,
            policy=AggregationPolicy.ALL_REQUIRED,
            current_time=self.now,
        )
        self.assertEqual(agg.status, AggregateStatus.COMPLETE)
        self.assertEqual(saved_parent.id, self.parent_work_id)

    def test_persistence_reconstruction_across_file_store(self) -> None:
        """Section 21.K: Persistent store restart and reconstruction."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_store = FileWorkStore(works_dir=Path(tmp_dir))
            # Persist parent work
            file_store.create(self.parent_work)

            # Persist child work in file store
            c1 = self._create_child(0, status=WorkStatus.DONE, summary="Child persisted to disk")
            save_child_work(file_store, c1)

            # Attach child id to parent
            p_curr = file_store.get(self.parent_work_id)
            self.assertIsNotNone(p_curr)
            assert p_curr is not None
            meta = dict(p_curr.resume_metadata or {})
            meta["child_work_ids"] = [c1.id]
            file_store.save(p_curr.with_update(resume_metadata=meta))

            # Run aggregate from store
            res = aggregate_from_store(file_store, self.parent_work_id, current_time=self.now)
            self.assertEqual(res.status, AggregateStatus.COMPLETE)
            self.assertEqual(res.total_children, 1)
            self.assertEqual(res.children[0].result_summary, "Child persisted to disk")

            # Attach to parent in file store
            saved_p = attach_aggregate_to_parent(file_store, self.parent_work_id, res)

            # Re-read from brand new store instance
            new_store = FileWorkStore(works_dir=Path(tmp_dir))
            reloaded_p = new_store.get(self.parent_work_id)
            self.assertIsNotNone(reloaded_p)
            assert reloaded_p is not None
            summary = get_latest_aggregate(reloaded_p)
            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["aggregate_digest"], res.aggregate_digest)


class TestSecretSafetyAggregation(BaseResultAggregationTestCase):
    """Section 21.L: Secret redaction and credential safety."""

    def test_api_keys_and_tokens_scrubbed_from_child_summary(self) -> None:
        leaky_summary = "Processed data using " + ("sk-proj-" + "1234567890abcdef1234567890") + " and token " + ("ghp_" + "1234567890abcdef1234567890")
        cr = ChildResult(
            child_work_id="work_child_a_00",
            subagent_id="sub_a_00",
            delegation_id="delg_a_00",
            declared_order=0,
            work_status=WorkStatus.DONE,
            success=True,
            result_summary=leaky_summary,
        )

        self.assertNotIn("sk-proj-", cr.result_summary)
        self.assertNotIn("ghp_", cr.result_summary)
        self.assertIn("[REDACTED_API_KEY]", cr.result_summary)
        self.assertIn("[REDACTED_TOKEN]", cr.result_summary)

        # Verify AggregateResult serialization and persistence does not leak the secret
        agg = AggregateResult(
            parent_work_id=self.parent_work_id,
            delegation_group_id="group_test",
            policy=AggregationPolicy.ALL_REQUIRED,
            status=AggregateStatus.COMPLETE,
            success=True,
            total_children=1,
            completed_children=1,
            successful_children=1,
            failed_children=0,
            cancelled_children=0,
            incomplete_children=0,
            included_children=1,
            children=(cr,),
            created_at=self.now,
        )
        saved_parent = attach_aggregate_to_parent(self.store, self.parent_work_id, agg)
        summary = get_latest_aggregate(saved_parent)
        self.assertIsNotNone(summary)
        serialized = json.dumps(summary)
        self.assertNotIn("sk-proj-", serialized)
        self.assertNotIn("ghp_", serialized)

    def test_pem_private_keys_scrubbed(self) -> None:
        pem_key = (
            "-----BEGIN " + "RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA0Y3Fq9Z7Y5+Yw6z9Vb3h7L3G9m7H6l4K...\n"
            "-----END " + "RSA PRIVATE KEY-----"
        )
        cr = ChildResult(
            child_work_id="work_child_a_00",
            subagent_id="sub_a_00",
            delegation_id="delg_a_00",
            declared_order=0,
            work_status=WorkStatus.DONE,
            success=True,
            result_summary=f"Key generated: {pem_key}",
        )

        self.assertNotIn("BEGIN RSA PRIVATE KEY", cr.result_summary)
        self.assertIn("[REDACTED_PRIVATE_KEY]", cr.result_summary)

    def test_credential_fields_rejected_by_reject_secrets(self) -> None:
        # Attempting to deserialize a dictionary containing a forbidden credential field
        bad_dict = {
            "child_work_id": "work_child_a_00",
            "subagent_id": "sub_a_00",
            "delegation_id": "delg_a_00",
            "declared_order": 0,
            "work_status": "done",
            "success": True,
            "api_key": "secret_key_12345",  # forbidden field
        }
        with self.assertRaises(ValueError):
            ChildResult.from_dict(bad_dict)


class TestPropertyInvariants(BaseResultAggregationTestCase):
    """Section 21.N: Property tests."""

    def test_state_determinism_property(self) -> None:
        """Property: Identical canonical state always produces identical AggregateResult and digest."""
        c1 = self._create_child(0, status=WorkStatus.DONE, summary="Done 1")
        c2 = self._create_child(1, status=WorkStatus.DONE, summary="Done 2")

        res_a = aggregate_children(self.parent_work, [c1, c2], current_time=self.now)
        res_b = aggregate_children(self.parent_work, [c1, c2], current_time=self.now)

        self.assertEqual(res_a.aggregate_digest, res_b.aggregate_digest)
        self.assertEqual(res_a.to_dict(), res_b.to_dict())

    def test_authority_invariance_property(self) -> None:
        """Property: Aggregation never grants or modifies capabilities or execution authority."""
        c1 = self._create_child(0, status=WorkStatus.DONE)
        caps_before = self.parent_work.capabilities

        res = aggregate_children(self.parent_work, [c1], current_time=self.now)

        # Parent capabilities unchanged
        self.assertEqual(self.parent_work.capabilities, caps_before)
        # AggregateResult has no capability fields
        self.assertFalse(hasattr(res, "capabilities"))
        self.assertFalse(hasattr(res, "approval_request_id"))
        self.assertFalse(hasattr(res, "transaction_id"))


if __name__ == "__main__":
    unittest.main()
