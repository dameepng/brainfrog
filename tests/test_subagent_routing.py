"""Unit and Property Tests for P1.3J Subagent Routing and Specialization.

Tests all components of subagent profile definition, registry management,
deterministic eligibility evaluation, ranking, capability attenuation boundary,
and P1.3I execution integration.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.runtime.approval import RiskClass
from core.runtime.capabilities import (
    Capabilities,
    FilesystemPolicy,
    GitPolicy,
    NetworkPolicy,
    ShellPolicy,
)
from core.runtime.approval import ApprovalService, CanonicalOperation, InMemoryApprovalStore
from core.runtime.child_work import ChildWork, get_child_work, save_child_work
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.delegation import (
    DelegationAttenuationError,
    DelegationContract,
)
from core.runtime.delegation_runtime import (
    CoordinationState,
    DelegationGroup,
    DelegationMode,
    save_delegation_group,
)
from orchestrator import PlanStep, StepResult
from core.runtime.permissions import PermissionAction
from core.runtime.subagent import Subagent, SubagentStatus
from core.runtime.subagent_execution import (
    SubagentExecutionBlockedError,
    SubagentExecutionCoordinator,
    SubagentExecutionRequest,
    SubagentExecutionStateError,
)
from core.runtime.subagent_routing import (
    MAX_METADATA_BYTES,
    MAX_PROFILES,
    DuplicateProfileError,
    NoEligibleSubagentError,
    ProfileRegistryLimitError,
    ProfileValidationError,
    SubagentProfile,
    SubagentProfileRegistry,
    SubagentRouter,
    SubagentRoutingError,
    SubagentRoutingResult,
    TaskRoutingRequest,
    TaskRoutingValidationError,
    TaskType,
    compute_profile_rank_key,
    compute_routing_digest,
    create_default_registry,
    create_routed_child_work,
    create_routed_delegation,
    is_profile_eligible,
    parse_task_type,
    validate_profile_id,
    validate_task_id,
)
from core.runtime.work import InMemoryWorkStore, Work, WorkStatus


class BaseSubagentRoutingTestCase(unittest.TestCase):
    """Shared test fixture setting up workspaces, profiles, and parent work."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.repo_dir = Path(self.temp_dir)
        self.work_store = InMemoryWorkStore()
        self.now = time.time()

        self.actor = "user_lead_001"
        self.session_id = "sess_routing_01"
        self.session_incarnation_id = "inc_routing_01"

        # Parent capabilities covering entire workspace read and src/ tests/ docs/ write
        self.parent_capabilities = Capabilities(
            filesystem=FilesystemPolicy(read=("src/", "tests/", "docs/", "ui/"), write=("src/", "tests/", "docs/")),
            network=NetworkPolicy(access=False),
            git=GitPolicy(read=True, commit=False, push=False),
        )

        self.parent_work = Work(
            id="work_parent_route_01",
            intent="Implement and verify subsystem",
            goal="Ship robust implementation",
            capabilities=self.parent_capabilities,
            actor_id=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            status=WorkStatus.PLANNING,
            created_at=self.now,
            updated_at=self.now,
        )
        self.work_store.create(self.parent_work)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def make_execution_contract(
        self,
        target_path: str = "src/auth.py",
        channel: str = "cli",
    ) -> ApprovedExecutionContract:
        caps = Capabilities(
            filesystem=FilesystemPolicy(read=(target_path,), write=(target_path,)),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=False),
            git=GitPolicy(read=False, commit=False),
        )
        service = ApprovalService(InMemoryApprovalStore())
        action_type = "write_code"
        targets = [target_path]
        op = CanonicalOperation(action_type, target_path, {"targets": targets})
        req = service.create_request(
            self.session_id,
            channel,
            self.actor,
            "chat",
            action_type,
            op,
            session_incarnation_id=self.session_incarnation_id,
            capabilities=caps,
            workspace_root=str(self.repo_dir),
        )
        service.approve(req.request_id, "approver_lead", channel)
        service.verify_and_consume(
            req.request_id,
            req.operation_digest,
            self.session_id,
            channel,
            session_incarnation_id=self.session_incarnation_id,
            requester_id=self.actor,
        )
        return ApprovedExecutionContract.from_approval_request(req, self.repo_dir)


# =============================================================================
# Test Group 1: SubagentProfileRegistry
# =============================================================================

class TestProfileRegistry(BaseSubagentRoutingTestCase):
    """Group 1: Registry operations, duplicate handling, bounds, deterministic listing."""

    def test_default_registry_creation(self) -> None:
        registry = create_default_registry()
        self.assertGreaterEqual(len(registry), 6)
        self.assertIsNotNone(registry.get("researcher"))
        self.assertIsNotNone(registry.get("backend_worker"))
        self.assertIsNotNone(registry.get("frontend_worker"))
        self.assertIsNotNone(registry.get("test_runner"))
        self.assertIsNotNone(registry.get("reviewer"))
        self.assertIsNotNone(registry.get("documentation_worker"))

    def test_register_and_lookup_profile(self) -> None:
        registry = SubagentProfileRegistry()
        prof = SubagentProfile(
            profile_id="custom_worker",
            name="Custom Worker",
            description="Performs specialized custom work",
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/",), write=()),
            ),
            supported_operations=("read_code",),
            supported_task_types=(TaskType.RESEARCH,),
            max_risk_class=RiskClass.LOW,
            filesystem_scope=("src/**",),
        )
        registry.register(prof)

        retrieved = registry.get("custom_worker")
        self.assertIsNotNone(retrieved)
        assert retrieved is not None
        self.assertEqual(retrieved.profile_id, "custom_worker")
        self.assertEqual(retrieved.name, "Custom Worker")

    def test_duplicate_profile_id_rejected(self) -> None:
        registry = SubagentProfileRegistry()
        prof = SubagentProfile(
            profile_id="worker_dup",
            name="Worker 1",
            description="Worker",
            required_capabilities=Capabilities(),
            supported_operations=("read_code",),
            supported_task_types=(TaskType.RESEARCH,),
        )
        registry.register(prof)

        with self.assertRaises(DuplicateProfileError):
            registry.register(prof)

    def test_bounded_profile_count_limit_enforced(self) -> None:
        registry = SubagentProfileRegistry()
        for i in range(MAX_PROFILES):
            registry.register(
                SubagentProfile(
                    profile_id=f"worker_{i:03d}",
                    name=f"Worker {i}",
                    description="Worker",
                    required_capabilities=Capabilities(),
                    supported_operations=("read_code",),
                    supported_task_types=(TaskType.RESEARCH,),
                )
            )
        self.assertEqual(len(registry), MAX_PROFILES)

        # Exceeding limit raises ProfileRegistryLimitError fail-closed
        with self.assertRaises(ProfileRegistryLimitError):
            registry.register(
                SubagentProfile(
                    profile_id="worker_overflow",
                    name="Overflow Worker",
                    description="Worker",
                    required_capabilities=Capabilities(),
                    supported_operations=("read_code",),
                    supported_task_types=(TaskType.RESEARCH,),
                )
            )

    def test_deterministic_profile_listing(self) -> None:
        registry = SubagentProfileRegistry()
        ids = ["charlie", "alpha", "bravo", "delta"]
        for pid in ids:
            registry.register(
                SubagentProfile(
                    profile_id=pid,
                    name=f"Worker {pid}",
                    description="Worker",
                    required_capabilities=Capabilities(),
                    supported_operations=("read_code",),
                    supported_task_types=(TaskType.RESEARCH,),
                )
            )
        listed = registry.list_profiles()
        self.assertEqual([p.profile_id for p in listed], ["alpha", "bravo", "charlie", "delta"])


# =============================================================================
# Test Group 2: Eligibility Rules
# =============================================================================

class TestEligibilityRules(BaseSubagentRoutingTestCase):
    """Group 2: Strict deterministic eligibility checking."""

    def setUp(self) -> None:
        super().setUp()
        self.registry = create_default_registry()
        self.router = SubagentRouter(self.registry)

    def test_exact_task_and_operation_eligibility(self) -> None:
        # Backend task: implementation in src/auth.py
        req = TaskRoutingRequest(
            task_id="task_backend_01",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/auth.py",), write=("src/auth.py",)),
            ),
            operation="write_code",
            risk_class=RiskClass.MEDIUM,
            target_scope=("src/auth.py",),
        )
        eligible = self.registry.resolve_eligible(req)
        eligible_ids = [p.profile_id for p in eligible]
        self.assertIn("backend_worker", eligible_ids)
        self.assertNotIn("researcher", eligible_ids)  # researcher is read-only
        self.assertNotIn("reviewer", eligible_ids)    # reviewer is read-only
        self.assertNotIn("frontend_worker", eligible_ids)  # frontend scoped to ui/**

    def test_capability_mismatch_fails_closed(self) -> None:
        # Task requires write to src/api.py, but researcher is read-only
        researcher = self.registry.get("researcher")
        assert researcher is not None

        req = TaskRoutingRequest(
            task_id="task_write_01",
            task_type=TaskType.RESEARCH,
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/api.py",), write=("src/api.py",)),
            ),
            operation="read_code",
            risk_class=RiskClass.LOW,
            target_scope=("src/api.py",),
        )
        is_el, reason = is_profile_eligible(researcher, req)
        self.assertFalse(is_el)
        self.assertIn("not covered by profile scopes", reason)

    def test_operation_mismatch_fails_closed(self) -> None:
        # Researcher does not support 'run_tests'
        researcher = self.registry.get("researcher")
        assert researcher is not None

        req = TaskRoutingRequest(
            task_id="task_tests_01",
            task_type=TaskType.RESEARCH,
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("tests/",), write=())),
            operation="run_tests",
            risk_class=RiskClass.LOW,
        )
        is_el, reason = is_profile_eligible(researcher, req)
        self.assertFalse(is_el)
        self.assertIn("Operation 'run_tests' not supported", reason)

    def test_risk_mismatch_fails_closed(self) -> None:
        # Researcher max risk is LOW; task risk is CRITICAL
        researcher = self.registry.get("researcher")
        assert researcher is not None

        req = TaskRoutingRequest(
            task_id="task_crit_01",
            task_type=TaskType.RESEARCH,
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/",), write=())),
            operation="read_code",
            risk_class=RiskClass.CRITICAL,
        )
        is_el, reason = is_profile_eligible(researcher, req)
        self.assertFalse(is_el)
        self.assertIn("exceeds profile max risk", reason)

    def test_filesystem_scope_mismatch_fails_closed(self) -> None:
        # Backend worker scoped to src/**; task targets secret/credentials.env
        backend = self.registry.get("backend_worker")
        assert backend is not None

        req = TaskRoutingRequest(
            task_id="task_scope_01",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("secret/credentials.env",), write=("secret/credentials.env",)),
            ),
            operation="write_code",
            risk_class=RiskClass.MEDIUM,
            target_scope=("secret/credentials.env",),
        )
        is_el, reason = is_profile_eligible(backend, req)
        self.assertFalse(is_el)
        self.assertTrue("not covered by profile scopes" in reason or "outside profile scope" in reason)

    def test_network_scope_mismatch_fails_closed(self) -> None:
        # Profile has no network access; task requires network
        researcher = self.registry.get("researcher")
        assert researcher is not None

        req = TaskRoutingRequest(
            task_id="task_net_01",
            task_type=TaskType.RESEARCH,
            required_capabilities=Capabilities(network=NetworkPolicy(access=True)),
            operation="read_code",
            risk_class=RiskClass.LOW,
            network_requirements=("api.external.com",),
        )
        is_el, reason = is_profile_eligible(researcher, req)
        self.assertFalse(is_el)
        self.assertIn("does not permit network access", reason)

    def test_empty_candidate_set_raises_no_eligible_subagent_error(self) -> None:
        # Request shell execution: no default profile supports shell execution
        req = TaskRoutingRequest(
            task_id="task_shell_01",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(shell=ShellPolicy(execute=True)),
            operation="write_code",
            risk_class=RiskClass.MEDIUM,
        )
        with self.assertRaises(NoEligibleSubagentError):
            self.router.route(req)


# =============================================================================
# Test Group 3: Deterministic Ranking
# =============================================================================

class TestDeterministicRanking(BaseSubagentRoutingTestCase):
    """Group 3: Ranking heuristics (task match, least privilege, least scope, risk, tie-break)."""

    def test_exact_task_match_wins(self) -> None:
        registry = SubagentProfileRegistry()
        # Profile 1 has RESEARCH as primary task type
        p1 = SubagentProfile(
            profile_id="primary_researcher",
            name="Primary Researcher",
            description="Research",
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/",))),
            supported_operations=("read_code",),
            supported_task_types=(TaskType.RESEARCH, TaskType.REVIEW),
            max_risk_class=RiskClass.LOW,
            filesystem_scope=("src/**",),
        )
        # Profile 2 has RESEARCH as secondary task type
        p2 = SubagentProfile(
            profile_id="secondary_researcher",
            name="Secondary Researcher",
            description="Reviewer and researcher",
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/",))),
            supported_operations=("read_code",),
            supported_task_types=(TaskType.REVIEW, TaskType.RESEARCH),
            max_risk_class=RiskClass.LOW,
            filesystem_scope=("src/**",),
        )
        registry.register(p2)
        registry.register(p1)

        req = TaskRoutingRequest(
            task_id="task_r1",
            task_type=TaskType.RESEARCH,
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/auth.py",))),
            operation="read_code",
            target_scope=("src/auth.py",),
        )
        router = SubagentRouter(registry)
        result = router.route(req)
        self.assertEqual(result.selected_profile_id, "primary_researcher")

    def test_narrower_sufficient_capability_wins(self) -> None:
        registry = SubagentProfileRegistry()
        # Profile A is read-only (smaller capability surface)
        prof_read = SubagentProfile(
            profile_id="read_only_worker",
            name="Read Only",
            description="Inspects code",
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/",), write=())),
            supported_operations=("read_code",),
            supported_task_types=(TaskType.RESEARCH,),
            max_risk_class=RiskClass.LOW,
            filesystem_scope=("src/**",),
        )
        # Profile B has write capability (larger capability surface)
        prof_write = SubagentProfile(
            profile_id="broad_worker",
            name="Broad Worker",
            description="Inspects and writes code",
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/",), write=("src/",))),
            supported_operations=("read_code", "write_code"),
            supported_task_types=(TaskType.RESEARCH,),
            max_risk_class=RiskClass.LOW,
            filesystem_scope=("src/**",),
        )
        registry.register(prof_write)
        registry.register(prof_read)

        req = TaskRoutingRequest(
            task_id="task_read_01",
            task_type=TaskType.RESEARCH,
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/auth.py",))),
            operation="read_code",
            target_scope=("src/auth.py",),
        )
        router = SubagentRouter(registry)
        result = router.route(req)
        # Narrower read-only profile should win under least privilege!
        self.assertEqual(result.selected_profile_id, "read_only_worker")

    def test_narrower_scope_surface_wins(self) -> None:
        registry = SubagentProfileRegistry()
        # Profile Broad has workspace-wide wildcard '**'
        prof_broad = SubagentProfile(
            profile_id="worker_broad_scope",
            name="Broad Scope Worker",
            description="Broad",
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/", "tests/", "docs/", "ui/"))),
            supported_operations=("read_code",),
            supported_task_types=(TaskType.RESEARCH,),
            max_risk_class=RiskClass.LOW,
            filesystem_scope=("**",),
        )
        # Profile Narrow has specific directory scope 'src/auth/**'
        prof_narrow = SubagentProfile(
            profile_id="worker_narrow_scope",
            name="Narrow Scope Worker",
            description="Narrow",
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/auth/",))),
            supported_operations=("read_code",),
            supported_task_types=(TaskType.RESEARCH,),
            max_risk_class=RiskClass.LOW,
            filesystem_scope=("src/auth/**",),
        )
        registry.register(prof_broad)
        registry.register(prof_narrow)

        req = TaskRoutingRequest(
            task_id="task_narrow_01",
            task_type=TaskType.RESEARCH,
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/auth/token.py",))),
            operation="read_code",
            target_scope=("src/auth/token.py",),
        )
        router = SubagentRouter(registry)
        result = router.route(req)
        # Narrower scope must win!
        self.assertEqual(result.selected_profile_id, "worker_narrow_scope")

    def test_lower_risk_wins(self) -> None:
        registry = SubagentProfileRegistry()
        prof_low = SubagentProfile(
            profile_id="worker_risk_low",
            name="Low Risk Worker",
            description="Worker",
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/",))),
            supported_operations=("read_code",),
            supported_task_types=(TaskType.RESEARCH,),
            max_risk_class=RiskClass.LOW,
            filesystem_scope=("src/**",),
        )
        prof_med = SubagentProfile(
            profile_id="worker_risk_med",
            name="Medium Risk Worker",
            description="Worker",
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/",))),
            supported_operations=("read_code",),
            supported_task_types=(TaskType.RESEARCH,),
            max_risk_class=RiskClass.MEDIUM,
            filesystem_scope=("src/**",),
        )
        registry.register(prof_med)
        registry.register(prof_low)

        req = TaskRoutingRequest(
            task_id="task_risk_01",
            task_type=TaskType.RESEARCH,
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/auth.py",))),
            operation="read_code",
            risk_class=RiskClass.LOW,
            target_scope=("src/auth.py",),
        )
        router = SubagentRouter(registry)
        result = router.route(req)
        self.assertEqual(result.selected_profile_id, "worker_risk_low")

    def test_stable_profile_id_tiebreaker(self) -> None:
        registry = SubagentProfileRegistry()
        p_beta = SubagentProfile(
            profile_id="worker_beta",
            name="Worker Beta",
            description="Worker",
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/",))),
            supported_operations=("read_code",),
            supported_task_types=(TaskType.RESEARCH,),
            max_risk_class=RiskClass.LOW,
            filesystem_scope=("src/**",),
        )
        p_alpha = SubagentProfile(
            profile_id="worker_alpha",
            name="Worker Alpha",
            description="Worker",
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/",))),
            supported_operations=("read_code",),
            supported_task_types=(TaskType.RESEARCH,),
            max_risk_class=RiskClass.LOW,
            filesystem_scope=("src/**",),
        )
        registry.register(p_beta)
        registry.register(p_alpha)

        req = TaskRoutingRequest(
            task_id="task_tie_01",
            task_type=TaskType.RESEARCH,
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/auth.py",))),
            operation="read_code",
            target_scope=("src/auth.py",),
        )
        router = SubagentRouter(registry)
        result = router.route(req)
        self.assertEqual(result.selected_profile_id, "worker_alpha")

    def test_repeated_routing_is_idempotent_and_deterministic(self) -> None:
        router = SubagentRouter(create_default_registry())
        req = TaskRoutingRequest(
            task_id="task_repeat_01",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/",), write=("src/auth.py",)),
            ),
            operation="write_code",
            risk_class=RiskClass.MEDIUM,
            target_scope=("src/auth.py",),
        )
        r1 = router.route(req)
        r2 = router.route(req)
        self.assertEqual(r1.selected_profile_id, r2.selected_profile_id)
        self.assertEqual(r1.routing_digest, r2.routing_digest)
        self.assertEqual(r1.candidate_count, r2.candidate_count)


# =============================================================================
# Test Group 4: Authority and Capability Attenuation Boundary
# =============================================================================

class TestAuthorityAndAttenuation(BaseSubagentRoutingTestCase):
    """Group 4: Authority boundary enforcement — parent ∩ profile ∩ task."""

    def test_router_cannot_expand_parent_capability(self) -> None:
        # Parent only has read authority over src/**, NO write authority
        parent_read_only = Capabilities(
            filesystem=FilesystemPolicy(read=("src/",), write=()),
        )
        router = SubagentRouter(create_default_registry())
        backend_prof = router.registry.get("backend_worker")
        assert backend_prof is not None

        req = TaskRoutingRequest(
            task_id="task_expand_01",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/",), write=("src/auth.py",)),
            ),
            operation="write_code",
            risk_class=RiskClass.MEDIUM,
            target_scope=("src/auth.py",),
            parent_capabilities=parent_read_only,
        )

        # Router rejects backend_worker when parent lacks required write authority
        with self.assertRaises(NoEligibleSubagentError):
            router.route(req)

    def test_delegation_creation_attenuates_correctly(self) -> None:
        router = SubagentRouter(create_default_registry())
        req = TaskRoutingRequest(
            task_id="task_delg_01",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/",), write=("src/pay.py",)),
            ),
            operation="write_code",
            risk_class=RiskClass.MEDIUM,
            target_scope=("src/pay.py",),
        )
        res = router.route(req)
        assert res.selected_profile is not None

        delg = create_routed_delegation(
            parent_capabilities=self.parent_capabilities,
            profile=res.selected_profile,
            request=req,
            delegation_id="delg_routed_01",
            parent_work_id=self.parent_work.id,
            child_subagent_id="sub_routed_01",
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            expires_at=self.now + 300.0,
            created_at=self.now,
        )

        # Attenuation guarantees: write scope contains ONLY src/pay.py
        self.assertEqual(delg.capabilities.filesystem.write, ("src/pay.py",))
        # Child does NOT inherit broad write scope from parent or profile
        self.assertNotIn("src/**", delg.capabilities.filesystem.write)
        self.assertNotIn("tests/**", delg.capabilities.filesystem.write)

    def test_profile_cannot_exceed_parent_authority(self) -> None:
        # Parent does not permit network access
        parent_no_net = Capabilities(
            filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
            network=NetworkPolicy(access=False),
        )
        prof_net = SubagentProfile(
            profile_id="net_worker",
            name="Network Worker",
            description="Needs network",
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/",), write=()),
                network=NetworkPolicy(access=True),
            ),
            supported_operations=("read_code",),
            supported_task_types=(TaskType.RESEARCH,),
            max_risk_class=RiskClass.LOW,
            network_scope=("configured_model_api",),
        )
        req = TaskRoutingRequest(
            task_id="task_net_atten_01",
            task_type=TaskType.RESEARCH,
            required_capabilities=Capabilities(network=NetworkPolicy(access=True)),
            operation="read_code",
        )

        with self.assertRaises(DelegationAttenuationError):
            create_routed_delegation(
                parent_capabilities=parent_no_net,
                profile=prof_net,
                request=req,
                delegation_id="delg_net_fail",
                parent_work_id=self.parent_work.id,
                child_subagent_id="sub_fail",
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                expires_at=self.now + 300.0,
            )


# =============================================================================
# Test Group 5: P1.3I Execution Integration
# =============================================================================

class TestExecutionIntegration(BaseSubagentRoutingTestCase):
    """Group 5: Full integration from TaskRoutingRequest -> Router -> Delegation -> ChildWork -> P1.3I execution."""

    def test_full_routed_execution_pipeline(self) -> None:
        router = SubagentRouter(create_default_registry())

        # 1. Plan step requirement
        req = TaskRoutingRequest(
            task_id="step_refactor_auth",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/",), write=("src/auth.py",)),
            ),
            operation="write_code",
            risk_class=RiskClass.MEDIUM,
            target_scope=("src/auth.py",),
        )

        # 2. Deterministic routing
        routing_result = router.route(req, parent_capabilities=self.parent_capabilities)
        self.assertEqual(routing_result.selected_profile_id, "backend_worker")
        assert routing_result.selected_profile is not None

        # 3. Create canonical ChildWork via routed factory
        child_cw = create_routed_child_work(
            parent_work=self.parent_work,
            profile=routing_result.selected_profile,
            request=req,
            child_work_id="work_child_routed_auth_01",
            expires_in_seconds=300.0,
            created_at=self.now,
        )
        save_child_work(self.work_store, child_cw)

        # 4. Prepare P1.3I execution coordinator with mock orchestrator runner
        mock_runner = MagicMock(return_value=[
            StepResult(PlanStep("1", "Implement auth refactor", []), "opened_pr", 0, "Changes applied successfully")
        ])
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=mock_runner,
        )

        contract = self.make_execution_contract("src/auth.py")
        exec_req = SubagentExecutionRequest(
            parent_work_id=self.parent_work.id,
            child_work_id=child_cw.id,
            subagent_id=child_cw.subagent_id,
            delegation_id=child_cw.delegation_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Refactor auth token logic",
            execution_contract=contract,
        )

        # 5. Execute via canonical P1.3I execution pipeline
        exec_res = coordinator.execute_child(exec_req)

        self.assertTrue(exec_res.success)
        self.assertEqual(exec_res.status, WorkStatus.DONE)
        self.assertIn("Changes applied successfully", exec_res.summary)

        # 6. Verify WorkStore record updated canonically to DONE
        updated_cw = get_child_work(self.work_store, child_cw.id)
        assert updated_cw is not None
        self.assertEqual(updated_cw.status, WorkStatus.DONE)
        assert updated_cw.subagent is not None
        self.assertEqual(updated_cw.subagent.status, SubagentStatus.COMPLETED)


# =============================================================================
# Test Group 6: State Semantics & Invariants
# =============================================================================

class TestStateSemanticsAndInvariants(BaseSubagentRoutingTestCase):
    """Group 6: Routing failure does not mutate Work; blocked child remains blocked."""

    def test_routing_failure_does_not_mutate_parent_work(self) -> None:
        initial_parent = self.work_store.get(self.parent_work.id)
        assert initial_parent is not None
        initial_rev = initial_parent.revision
        initial_status = initial_parent.status

        router = SubagentRouter(create_default_registry())
        impossible_req = TaskRoutingRequest(
            task_id="task_impossible",
            task_type=TaskType.RESEARCH,
            required_capabilities=Capabilities(shell=ShellPolicy(execute=True)),
            operation="shell_execution",
            risk_class=RiskClass.CRITICAL,
        )

        with self.assertRaises(NoEligibleSubagentError):
            router.route(impossible_req)

        # Parent work remains completely untouched
        parent_after = self.work_store.get(self.parent_work.id)
        assert parent_after is not None
        self.assertEqual(parent_after.revision, initial_rev)
        self.assertEqual(parent_after.status, initial_status)

    def test_router_cannot_execute_or_unblock_blocked_child(self) -> None:
        router = SubagentRouter(create_default_registry())
        req = TaskRoutingRequest(
            task_id="task_blocked_dep",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/",), write=("src/pay.py",))),
            operation="write_code",
            target_scope=("src/pay.py",),
        )
        res = router.route(req)
        assert res.selected_profile is not None

        child_cw = create_routed_child_work(
            parent_work=self.parent_work,
            profile=res.selected_profile,
            request=req,
            child_work_id="work_child_blocked_pay_01",
        )
        save_child_work(self.work_store, child_cw)

        # Put child in a delegation group as BLOCKED
        group = DelegationGroup(
            delegation_group_id="grp_blocked_01",
            parent_work_id=self.parent_work.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            mode=DelegationMode.SEQUENTIAL,
            child_work_ids=(child_cw.id,),
            dependencies={child_cw.id: ()},
            max_concurrency=1,
            coordination_states={child_cw.id: CoordinationState.BLOCKED},
        )
        save_delegation_group(self.work_store, group)

        # Executing a blocked child must raise SubagentExecutionBlockedError
        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)
        contract = self.make_execution_contract("src/pay.py")
        exec_req = SubagentExecutionRequest(
            parent_work_id=self.parent_work.id,
            child_work_id=child_cw.id,
            subagent_id=child_cw.subagent_id,
            delegation_id=child_cw.delegation_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Refactor pay",
            execution_contract=contract,
        )

        with self.assertRaises(SubagentExecutionBlockedError):
            coordinator.execute_child(exec_req, delegation_group=group)


# =============================================================================
# Test Group 7: Security Boundaries & Input Validation
# =============================================================================

class TestSecurityBoundaries(BaseSubagentRoutingTestCase):
    """Group 7: Path traversal, absolute paths, secrets, unknown types, malformed input."""

    def test_path_traversal_in_scope_rejected(self) -> None:
        with self.assertRaises(Exception):  # DelegationAttenuationError / TaskRoutingValidationError
            TaskRoutingRequest(
                task_id="task_traversal",
                task_type=TaskType.RESEARCH,
                required_capabilities=Capabilities(),
                operation="read_code",
                target_scope=("../../../etc/passwd",),
            )

    def test_absolute_path_in_scope_rejected(self) -> None:
        with self.assertRaises(Exception):
            TaskRoutingRequest(
                task_id="task_abs_path",
                task_type=TaskType.RESEARCH,
                required_capabilities=Capabilities(),
                operation="read_code",
                target_scope=("/var/log/system.log",),
            )

    def test_secret_in_metadata_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TaskRoutingRequest(
                task_id="task_secret",
                task_type=TaskType.RESEARCH,
                required_capabilities=Capabilities(),
                operation="read_code",
                metadata={"api_key": "sk-proj-" + "supersecretkey123456789"},
            )

    def test_secret_in_profile_metadata_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SubagentProfile(
                profile_id="prof_secret",
                name="Worker",
                description="Worker",
                required_capabilities=Capabilities(),
                supported_operations=("read_code",),
                supported_task_types=(TaskType.RESEARCH,),
                metadata={"token": "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyz"},
            )

    def test_invalid_profile_id_rejected(self) -> None:
        with self.assertRaises(ProfileValidationError):
            validate_profile_id("../evil_id")
        with self.assertRaises(ProfileValidationError):
            validate_profile_id("evil/id")
        with self.assertRaises(ProfileValidationError):
            validate_profile_id("")

    def test_unknown_task_type_fails_closed(self) -> None:
        with self.assertRaises(TaskRoutingValidationError):
            parse_task_type("arbitrary_unknown_type")

    def test_serialization_round_trip(self) -> None:
        router = SubagentRouter(create_default_registry())
        prof = router.registry.get("backend_worker")
        assert prof is not None

        d = prof.to_dict()
        reconstructed = SubagentProfile.from_dict(d)
        self.assertEqual(prof.profile_id, reconstructed.profile_id)
        self.assertEqual(prof.supported_operations, reconstructed.supported_operations)
        self.assertEqual(prof.supported_task_types, reconstructed.supported_task_types)

        req = TaskRoutingRequest(
            task_id="task_ser_01",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(filesystem=FilesystemPolicy(read=("src/",), write=("src/auth.py",))),
            operation="write_code",
            risk_class=RiskClass.MEDIUM,
            target_scope=("src/auth.py",),
        )
        req_d = req.to_dict()
        req_rec = TaskRoutingRequest.from_dict(req_d)
        self.assertEqual(req.task_id, req_rec.task_id)
        self.assertEqual(req.task_type, req_rec.task_type)

        res = router.route(req)
        res_d = res.to_dict()
        res_rec = SubagentRoutingResult.from_dict(res_d)
        self.assertEqual(res.selected_profile_id, res_rec.selected_profile_id)
        self.assertEqual(res.routing_digest, res_rec.routing_digest)


if __name__ == "__main__":
    unittest.main()
