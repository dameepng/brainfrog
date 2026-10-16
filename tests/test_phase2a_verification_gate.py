"""Adversarial and functional test suite for BrainFrog Phase 2A Step 2:
Transaction Verification Gate.

Enforces Phase 2A Step 2 Invariants:
1. Filesystem mutation success is not equivalent to verified task correctness.
2. Missing or empty test_command never returns StepResult(status="verified").
3. Untested/unverified transactions cannot authorize Git staging or PR finalization.
4. Verifier exceptions or malformed outputs never strand transactions in VERIFYING state.
5. Conservative rollback detects external file deletions, destination mutations, and source collisions.
6. Historical COMMITTED records are never auto-promoted to VERIFIED.
7. _write_files() reports mutation (committed=True) and verification outcomes (COMMITTED_UNTESTED) distinctly.
8. Recovery preserves COMMITTED_UNTESTED and VERIFIED without false promotion.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.runtime.approval import ApprovalService, CanonicalOperation, InMemoryApprovalStore
from core.runtime.capabilities import Capabilities, FilesystemPolicy, NetworkPolicy
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.git_staging import (
    inspect_staged_index,
    stage_authorized_targets,
)
from core.runtime.process import HardenedProcessResult
from core.runtime.proof import (
    ChangesEvidence,
    FileProofStore,
    ProofArtifact,
    ProofVerdict,
    ReproducibilityEvidence,
    TaskEvidence,
    VerificationEvidence,
    compute_proof_verdict,
)
from core.runtime.proof_verifier import (
    DeterministicProofVerifier,
    ProofVerificationStatus,
)
from core.runtime.transaction import (
    BasicFilesystemVerifier,
    InMemoryTransactionStore,
    OperationStatus,
    OperationType,
    Transaction,
    TransactionCoordinator,
    TransactionOperation,
    TransactionRecoveryManager,
    TransactionResult,
    TransactionStatus,
    TransactionVerifier,
)
from core.runtime.workflow import (
    effective_tx_status,
    is_tx_committed,
    is_tx_rolled_back,
    is_tx_untested,
    is_tx_verified,
)
from core.runtime.workspace_lock import WorkspaceLock
from orchestrator import (
    Answer,
    Orchestrator,
    PlanStep,
    RunConfig,
    StepResult,
    _write_files,
)


class TestPhase2AVerificationGate(unittest.TestCase):
    """Adversarial verification gate test suite for Phase 2A Step 2."""

    def _init_repo(self, path: Path) -> None:
        """Helper to initialize bare Git repository with initial commit."""
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test Gate"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "gate@test.com"], cwd=path, check=True)
        init_file = path / "README.md"
        init_file.write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=path, check=True, capture_output=True)

    def _make_contract(self, repo_dir: Path, target_path: str) -> ApprovedExecutionContract:
        service = ApprovalService(InMemoryApprovalStore())
        caps = Capabilities(
            filesystem=FilesystemPolicy(read=(target_path,), write=(target_path,)),
            network=NetworkPolicy(access=True),
        )
        req = service.create_request(
            session_id="session-gate",
            session_incarnation_id="incarnation-gate",
            channel="cli",
            user_id="user-gate",
            conversation_id="chat-gate",
            operation_type="write_file",
            canonical_operation=CanonicalOperation("write_file", target_path),
            capabilities=caps,
            workspace_root=str(repo_dir.resolve()),
        )
        service.approve(req.request_id, "approver-gate", "cli")
        service.verify_and_consume(
            req.request_id,
            req.operation_digest,
            "session-gate",
            "cli",
            session_incarnation_id="incarnation-gate",
            requester_id="user-gate",
        )
        return ApprovedExecutionContract.from_approval_request(req, repo_dir)

    def test_adv_gate_01_no_test_command_yields_untested_not_verified(self):
        """1. Missing or empty test_command in contract execution never returns 'verified'."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            contract = self._make_contract(repo_dir, "calculator.py")

            cfg = RunConfig(
                task="implement calculator",
                repo_dir=repo_dir,
                mode="build",
                execution_contract=contract,
                test_command=[],  # Explicitly empty test_command
            )

            mock_s1 = MagicMock()
            mock_s2 = MagicMock()
            mock_s2.write_code.return_value = {"calculator.py": "def add(a, b): return a + b\n"}

            orch = Orchestrator(mock_s1, mock_s2, cfg)
            step = PlanStep("1", "implement calculator", ["calculator.py"])

            res = orch._run_step(step)

            # Invariant: Must return untested outcome, NEVER "verified"
            self.assertNotEqual(res.outcome, "verified")
            self.assertEqual(res.outcome, "untested")
            self.assertIn("no domain test suite was configured", res.detail)

            # Invariant: Transaction result is COMMITTED_UNTESTED, NOT VERIFIED
            last_tx = cfg.last_transaction_result
            self.assertIsNotNone(last_tx)
            assert last_tx is not None
            self.assertTrue(last_tx.committed)
            self.assertFalse(last_tx.is_verified)
            self.assertEqual(last_tx.status, TransactionStatus.COMMITTED_UNTESTED)
            self.assertEqual(last_tx.verification_outcome, "untested")

    def test_adv_gate_02_committed_untested_cannot_be_staged_or_committed_to_git(self):
        """2. COMMITTED_UNTESTED transaction cannot be staged or finalized to Git."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            (repo_dir / "untested.py").write_text("print('untested')\n", encoding="utf-8")
            op = TransactionOperation(
                type=OperationType.CREATE_FILE,
                target="untested.py",
                status=OperationStatus.EXECUTED,
            )

            tx_untested = TransactionResult(
                transaction_id="tx_untested_01",
                status=TransactionStatus.COMMITTED_UNTESTED,
                committed=True,
                rolled_back=False,
                operations=(op,),
                verification_outcome="untested",
            )

            # Direct staging rejection
            stage_res = stage_authorized_targets(repo_dir, ["untested.py"], last_tx_result=tx_untested)
            self.assertFalse(stage_res.success)
            self.assertEqual(stage_res.rejection_code, "unverified_transaction")
            self.assertIn("is not verified", stage_res.error)

            # Orchestrator _finalize_pr rejection
            cfg = RunConfig(
                task="test task",
                repo_dir=repo_dir,
                last_transaction_result=tx_untested,
            )
            mock_s1 = MagicMock()
            mock_s1.decide.return_value = {
                "diff_risk": Answer(choice="low", score=0, noul=0.1, confidence=0.9),
                "safe_to_proceed": Answer(choice="draft_pr", score="low", noul=1.0, confidence=0.95),
            }
            mock_s2 = MagicMock()
            orch = Orchestrator(mock_s1, mock_s2, cfg)
            step = PlanStep("1", "test step", ["untested.py"])

            finalize_res = orch._finalize_pr(step, retries=0, test_output="UNTESTED")
            self.assertEqual(finalize_res.outcome, "rejected")
            self.assertIn("No authorized targets", finalize_res.detail)

            # Verify index remains clean
            staged = inspect_staged_index(repo_dir)
            self.assertEqual(staged, [])

    def test_adv_gate_03_failing_tests_trigger_automatic_reverse_rollback(self):
        """3. Failing domain tests trigger reverse rollback and report error."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            contract = self._make_contract(repo_dir, "buggy.py")

            cfg = RunConfig(
                task="implement feature",
                repo_dir=repo_dir,
                mode="build",
                execution_contract=contract,
                test_command=["python", "-c", "import sys; sys.exit(1)"],  # Failing tests
            )

            mock_s1 = MagicMock()
            mock_s2 = MagicMock()
            mock_s2.write_code.return_value = {"buggy.py": "broken code\n"}

            orch = Orchestrator(mock_s1, mock_s2, cfg)
            step = PlanStep("1", "implement feature", ["buggy.py"])

            with self.assertRaises(RuntimeError) as ctx:
                orch._run_step(step)

            self.assertIn("failed with exit code 1", str(ctx.exception))

            # Invariant: Mutations were rolled back from disk
            self.assertFalse((repo_dir / "buggy.py").exists())

            # Invariant: Transaction result is marked rolled back
            last_tx = cfg.last_transaction_result
            self.assertIsNotNone(last_tx)
            assert last_tx is not None
            self.assertTrue(last_tx.rolled_back)
            self.assertEqual(last_tx.status, TransactionStatus.ROLLED_BACK)

    def test_adv_gate_04_test_suite_timeout_triggers_automatic_rollback(self):
        """4. Domain test suite timeout triggers automatic reverse rollback."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            contract = self._make_contract(repo_dir, "timed_out.py")

            cfg = RunConfig(
                task="implement feature",
                repo_dir=repo_dir,
                mode="build",
                execution_contract=contract,
                test_command=["python", "-c", "import time; time.sleep(10)"],
            )

            mock_s1 = MagicMock()
            mock_s2 = MagicMock()
            mock_s2.write_code.return_value = {"timed_out.py": "def sleep(): pass\n"}

            orch = Orchestrator(mock_s1, mock_s2, cfg)
            step = PlanStep("1", "implement feature", ["timed_out.py"])

            # Mock test process as timed out
            timeout_proc = HardenedProcessResult(
                args=["pytest"],
                returncode=-1,
                stdout="",
                stderr="Execution timed out after 30s",
                is_timeout=True,
            )

            with patch.object(orch, "_run_test_suite", return_value=timeout_proc):
                with self.assertRaises(RuntimeError) as ctx:
                    orch._run_step(step)

            self.assertIn("timed out", str(ctx.exception))
            self.assertFalse((repo_dir / "timed_out.py").exists())
            last_tx = cfg.last_transaction_result
            self.assertIsNotNone(last_tx)
            assert last_tx is not None
            self.assertTrue(last_tx.rolled_back)

    def test_adv_gate_05_cancellation_during_verification_rolls_back_safely(self):
        """5. Cancellation token set during verification triggers clean rollback."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            contract = self._make_contract(repo_dir, "cancel_target.py")

            cfg = RunConfig(
                task="feature",
                repo_dir=repo_dir,
                mode="build",
                execution_contract=contract,
                test_command=["python", "-c", "import sys; sys.exit(0)"],
            )

            mock_s1 = MagicMock()
            mock_s2 = MagicMock()
            mock_s2.write_code.return_value = {"cancel_target.py": "cancelled work\n"}

            orch = Orchestrator(mock_s1, mock_s2, cfg)
            step = PlanStep("1", "feature", ["cancel_target.py"])

            cancel_proc = HardenedProcessResult(
                args=["pytest"],
                returncode=-1,
                stdout="",
                stderr="Cancelled",
                is_cancelled=True,
            )

            with patch.object(orch, "_run_test_suite", return_value=cancel_proc):
                with self.assertRaises(RuntimeError) as ctx:
                    orch._run_step(step)

            self.assertIn("cancelled", str(ctx.exception).lower())
            self.assertFalse((repo_dir / "cancel_target.py").exists())
            self.assertTrue(cfg.last_transaction_result.rolled_back)

    def test_adv_gate_06_verifier_raising_exception_does_not_strand_transaction(self):
        """6. Verifier raising an unexpected exception safely triggers rollback and does not strand in VERIFYING."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            store = InMemoryTransactionStore()

            class CrashingVerifier(TransactionVerifier):
                def verify(self, tx: Transaction, ws: Path):
                    raise RuntimeError("Crash inside custom verifier algorithm")

            with WorkspaceLock(workspace):
                coord = TransactionCoordinator(workspace, store=store, verifier=CrashingVerifier())
                coord.stage_create("test.txt", "hello")

                res = coord.run()

                # Invariant: Must fail closed and roll back
                self.assertFalse(res.success)
                self.assertFalse(res.committed)
                self.assertTrue(res.rolled_back)
                self.assertEqual(res.status, TransactionStatus.ROLLED_BACK)
                self.assertIn("Crash inside custom verifier", str(res.error))

                # Invariant: Stored transaction is terminal, NOT stranded in VERIFYING
                saved = store.get(coord.tx.id)
                self.assertIsNotNone(saved)
                assert saved is not None
                self.assertTrue(saved.is_terminal)
                self.assertEqual(saved.status, TransactionStatus.ROLLED_BACK)

    def test_adv_gate_07_malformed_verifier_output_fails_closed_and_rolls_back(self):
        """7. Malformed verifier return (e.g. non-tuple or bad types) fails closed and rolls back."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            store = InMemoryTransactionStore()

            class MalformedVerifier(TransactionVerifier):
                def verify(self, tx: Transaction, ws: Path):
                    # Returns a string instead of a 3-tuple
                    return "not a tuple"  # type: ignore

            with WorkspaceLock(workspace):
                coord = TransactionCoordinator(workspace, store=store, verifier=MalformedVerifier())
                coord.stage_create("malformed.txt", "data")

                res = coord.run()
                self.assertFalse(res.success)
                self.assertTrue(res.rolled_back)
                self.assertEqual(res.status, TransactionStatus.ROLLED_BACK)
                self.assertIn("malformed result", str(res.error).lower())
                self.assertFalse((workspace / "malformed.txt").exists())

    def test_adv_gate_08_rollback_detects_external_file_deletion_and_aborts_conservatively(self):
        """8. Rollback detects external deletion of a modified file and aborts conservatively without recreating it."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            original = workspace / "editable.txt"
            original.write_text("v1 original", encoding="utf-8")

            with WorkspaceLock(workspace):
                coord = TransactionCoordinator(workspace)
                coord.stage_modify("editable.txt", "v2 modified by agent")
                self.assertTrue(coord.execute())
                self.assertEqual(original.read_text(encoding="utf-8"), "v2 modified by agent")

                # External user deletes the file while agent was running
                original.unlink()
                self.assertFalse(original.exists())

                # Rollback must detect external deletion and refuse to recreate the file
                rb_res = coord.rollback(reason="Test failure")
                self.assertFalse(rb_res.rolled_back)
                self.assertEqual(rb_res.status, TransactionStatus.FAILED)
                self.assertIn("deleted externally", str(rb_res.rollback_error))

                # User deletion preserved: file must NOT be recreated
                self.assertFalse(original.exists())

    def test_adv_gate_09_rollback_detects_external_rename_destination_mutation(self):
        """9. Rollback detects external modification to a renamed destination file and preserves user changes."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            src = workspace / "old_name.txt"
            src.write_text("initial content", encoding="utf-8")

            with WorkspaceLock(workspace):
                coord = TransactionCoordinator(workspace)
                coord.stage_rename("old_name.txt", "new_name.txt")
                self.assertTrue(coord.execute())
                self.assertFalse(src.exists())
                dest = workspace / "new_name.txt"
                self.assertTrue(dest.exists())

                # External user modifies the renamed file at new destination
                dest.write_text("user modifications at new destination", encoding="utf-8")

                # Rollback must detect external mutation and abort
                rb_res = coord.rollback(reason="Test failure")
                self.assertFalse(rb_res.rolled_back)
                self.assertEqual(rb_res.status, TransactionStatus.FAILED)
                self.assertIn("content modified externally", str(rb_res.rollback_error))

                # User modifications at destination remain preserved
                self.assertEqual(dest.read_text(encoding="utf-8"), "user modifications at new destination")
                self.assertFalse(src.exists())

    def test_adv_gate_10_rollback_detects_recreated_rename_source_collision(self):
        """10. Rollback detects collision if external actor recreated a file at original source path."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            src = workspace / "move_me.txt"
            src.write_text("original", encoding="utf-8")

            with WorkspaceLock(workspace):
                coord = TransactionCoordinator(workspace)
                coord.stage_rename("move_me.txt", "moved.txt")
                self.assertTrue(coord.execute())

                # External user creates a new file at move_me.txt
                src.write_text("new external file at original location", encoding="utf-8")

                # Rollback must detect collision and refuse to overwrite src
                rb_res = coord.rollback(reason="Test failure")
                self.assertFalse(rb_res.rolled_back)
                self.assertEqual(rb_res.status, TransactionStatus.FAILED)
                self.assertIn("source file was recreated externally", str(rb_res.rollback_error))

                # External file at original location remains untouched
                self.assertEqual(src.read_text(encoding="utf-8"), "new external file at original location")
                self.assertTrue((workspace / "moved.txt").exists())

    def test_adv_gate_11_legacy_committed_records_not_promoted_to_verified(self):
        """11. Historical persisted COMMITTED records deserialize as COMMITTED and are not promoted to VERIFIED."""
        legacy_data = {
            "id": "tx_legacy_100",
            "workspace": "/tmp/legacy",
            "status": "committed",
            "operations": [],
            "started_at": 1000.0,
            "updated_at": 1005.0,
            "completed_at": 1005.0,
        }

        tx = Transaction.from_dict(legacy_data)
        self.assertEqual(tx.status, TransactionStatus.COMMITTED)
        self.assertTrue(tx.is_committed)
        self.assertFalse(tx.is_verified)  # Invariant: Never auto-promoted!

        # Invariant: Legacy COMMITTED record without verified evidence cannot authorize staging
        tx_res = TransactionResult(
            transaction_id=tx.id,
            status=tx.status,
            committed=True,
            rolled_back=False,
            operations=(),
        )
        self.assertFalse(tx_res.is_verified)

    def test_adv_gate_12_write_files_reports_mutation_and_verification_outcomes_distinctly(self):
        """12. _write_files() reports successful mutation (committed=True) and explicit COMMITTED_UNTESTED status."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            files = {"helper.py": "def helper(): return True\n"}

            res = _write_files(workspace, files, mode="build")

            self.assertIsInstance(res, TransactionResult)
            # Mutation succeeded
            self.assertTrue(res.committed)
            self.assertTrue(res.success)
            # Domain verification was not executed: untested!
            self.assertFalse(res.is_verified)
            self.assertEqual(res.status, TransactionStatus.COMMITTED_UNTESTED)
            self.assertEqual(res.verification_outcome, "untested")
            self.assertTrue((workspace / "helper.py").exists())

    def test_adv_gate_13_recovery_of_new_states_preserves_untested_and_verified(self):
        """13. Recovery idempotently preserves COMMITTED_UNTESTED and VERIFIED without false status promotion."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            store = InMemoryTransactionStore()
            rm = TransactionRecoveryManager(workspace, store)

            # A. COMMITTED_UNTESTED transaction with commit marker
            tx1 = Transaction(
                id="tx_untested",
                workspace=str(workspace),
                status=TransactionStatus.COMMITTED_UNTESTED,
                commit_marker={"status": "committed_untested", "transaction_id": "tx_untested"},
            )
            store.save(tx1)

            rec1 = rm.recover("tx_untested")
            self.assertTrue(rec1.recovered)
            self.assertEqual(rec1.final_status, TransactionStatus.COMMITTED_UNTESTED)
            self.assertNotEqual(rec1.final_status, TransactionStatus.VERIFIED)

            # B. VERIFIED transaction with verified marker
            tx2 = Transaction(
                id="tx_verified",
                workspace=str(workspace),
                status=TransactionStatus.VERIFIED,
                commit_marker={"status": "verified", "transaction_id": "tx_verified"},
            )
            store.save(tx2)

            rec2 = rm.recover("tx_verified")
            self.assertTrue(rec2.recovered)
            self.assertEqual(rec2.final_status, TransactionStatus.VERIFIED)

            # C. Incomplete transaction in VERIFYING state (crashed during test run) -> must rollback
            (workspace / "abandoned.txt").write_text("abandoned", encoding="utf-8")
            op = TransactionOperation(
                type=OperationType.CREATE_FILE,
                target="abandoned.txt",
                status=OperationStatus.PREPARED,
                after_state={"content": "abandoned", "is_text": True},
            )
            tx3 = Transaction(
                id="tx_crashed_during_verify",
                workspace=str(workspace),
                status=TransactionStatus.VERIFYING,
                operations=[op],
                commit_marker=None,
            )
            store.save(tx3)

            rec3 = rm.recover("tx_crashed_during_verify")
            self.assertTrue(rec3.rolled_back)
            self.assertEqual(rec3.final_status, TransactionStatus.ROLLED_BACK)
            self.assertFalse((workspace / "abandoned.txt").exists())

    def test_adv_gate_14_verified_transaction_authorizes_staging_cleanly(self):
        """14. A legitimately VERIFIED transaction authorizes Git staging and commits cleanly."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            (repo_dir / "verified_feature.py").write_text("def test_ok(): pass\n", encoding="utf-8")
            op = TransactionOperation(
                type=OperationType.CREATE_FILE,
                target="verified_feature.py",
                status=OperationStatus.EXECUTED,
            )

            tx_verified = TransactionResult(
                transaction_id="tx_verified_ok",
                status=TransactionStatus.VERIFIED,
                committed=True,
                rolled_back=False,
                operations=(op,),
                verification={"passed": True, "domain_verified": True},
                verification_outcome="verified",
            )

            # Stage with verified transaction result
            stage_res = stage_authorized_targets(repo_dir, ["verified_feature.py"], last_tx_result=tx_verified)
            self.assertTrue(stage_res.success)
            self.assertEqual(stage_res.staged_paths, ("verified_feature.py",))

            # Staged in index
            staged = inspect_staged_index(repo_dir)
            self.assertEqual(staged, ["verified_feature.py"])

    # =========================================================================
    # Remediation Priority 1 — Proof consistency & rejection of contradictions
    # =========================================================================

    def test_adv_gate_15_proof_verifier_accepts_verified_changes_status(self):
        """15. DeterministicProofVerifier accepts valid ChangesEvidence.status == 'VERIFIED'."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            (repo_dir / "tested.py").write_text("def test_ok(): pass\n", encoding="utf-8")
            subprocess.run(["git", "add", "tested.py"], cwd=repo_dir, check=True)
            subprocess.run(["git", "commit", "-m", "Add tested.py"], cwd=repo_dir, check=True)

            store = FileProofStore(repo_dir)
            task_ev = TaskEvidence(
                task_id="task_p1_001",
                session_id="s1",
                description="Test priority 1",
                created_at="2026-10-14T00:00:00Z",
                completed_at="2026-10-14T00:00:01Z",
                duration_ms=1000,
            )
            # Create a valid proof with ChangesEvidence.status == "VERIFIED"
            artifact = ProofArtifact(
                task=task_ev,
                verdict=ProofVerdict(status="VERIFIED", reason="Task verified successfully"),
                provenance=None,
                verification=VerificationEvidence("pytest", 0, "1 passed", ""),
                changes=ChangesEvidence("tx_p1_001", "VERIFIED", []),
                reproducibility=ReproducibilityEvidence("pytest", 0),
            )
            store.save(artifact)

            verifier = DeterministicProofVerifier(repo_dir)
            report = verifier.verify("task_p1_001")

            # Find verdict_transaction_consistency check
            consistency_check = next((c for c in report.checks if c.check_name == "verdict_transaction_consistency"), None)
            self.assertIsNotNone(consistency_check)
            self.assertEqual(consistency_check.status, ProofVerificationStatus.VALID)
            self.assertIn("VERIFIED changes status", consistency_check.message)

    def test_adv_gate_16_proof_verifier_rejects_contradictory_verdict_combinations(self):
        """16. Proof verifier rejects contradictory verdict and changes status combinations."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)
            store = FileProofStore(repo_dir)

            task_ev = TaskEvidence(
                task_id="task_contra",
                session_id="s1",
                description="Contradiction test",
                created_at="2026-10-14T00:00:00Z",
                completed_at="2026-10-14T00:00:01Z",
                duration_ms=1000,
            )

            contradictory_cases = [
                # (verdict_status, changes_status, test_exit)
                ("VERIFIED", "COMMITTED_UNTESTED", 0),
                ("VERIFIED", "ROLLED_BACK", 0),
                ("FAILED", "VERIFIED", 1),
                ("CANCELLED", "VERIFIED", 0),
            ]

            for v_stat, c_stat, exit_code in contradictory_cases:
                artifact = ProofArtifact(
                    task=task_ev,
                    verdict=ProofVerdict(status=v_stat, reason="Test verdict"),
                    provenance=None,
                    verification=VerificationEvidence("pytest", exit_code, "test output", ""),
                    changes=ChangesEvidence("tx_contra", c_stat, []),
                    reproducibility=ReproducibilityEvidence("pytest", exit_code),
                )
                store.save(artifact, allow_overwrite=True)

                verifier = DeterministicProofVerifier(repo_dir)
                report = verifier.verify("task_contra")
                consistency_check = next((c for c in report.checks if c.check_name == "verdict_transaction_consistency"), None)
                self.assertIsNotNone(consistency_check)
                self.assertEqual(
                    consistency_check.status,
                    ProofVerificationStatus.INVALID,
                    f"Failed to reject contradiction: verdict={v_stat}, changes={c_stat}",
                )
                self.assertEqual(report.overall_status, ProofVerificationStatus.INVALID)

    # =========================================================================
    # Remediation Priority 2 — Crash recovery preserving external changes
    # =========================================================================

    def test_adv_gate_17_recovery_preserves_external_changes_on_executed_create(self):
        """17. Recovery rollback does not delete an externally modified file created by an EXECUTED op."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            store = InMemoryTransactionStore()
            rm = TransactionRecoveryManager(workspace=workspace, store=store)

            # op was CREATE_FILE, marked EXECUTED with after_state content="original"
            target_path = workspace / "user_created.txt"
            target_path.write_text("external_user_modifications", encoding="utf-8")

            op = TransactionOperation(
                type=OperationType.CREATE_FILE,
                target="user_created.txt",
                status=OperationStatus.EXECUTED,
                after_state={"content": "original", "is_text": True},
            )
            tx = Transaction(
                id="tx_crash_create",
                workspace=str(workspace),
                status=TransactionStatus.EXECUTING,
                operations=[op],
                commit_marker=None,
            )
            store.save(tx)

            # Recovery must detect external modification, abort deletion, and fail closed
            rec = rm.recover("tx_crash_create")
            self.assertFalse(rec.recovered)
            self.assertFalse(rec.rolled_back)
            self.assertTrue(rec.ambiguous)
            self.assertEqual(rec.final_status, TransactionStatus.FAILED)
            # External file must NOT be deleted!
            self.assertTrue(target_path.exists())
            self.assertEqual(target_path.read_text(encoding="utf-8"), "external_user_modifications")
            tx_saved = store.get("tx_crash_create")
            self.assertIsNotNone(tx_saved)
            self.assertEqual(tx_saved.operations[0].status, OperationStatus.FAILED)

            # Repeated recovery attempt must be idempotent and preserve file
            rec2 = rm.recover("tx_crash_create")
            self.assertFalse(rec2.recovered)
            self.assertFalse(rec2.rolled_back)
            self.assertTrue(rec2.ambiguous)
            self.assertEqual(rec2.final_status, TransactionStatus.FAILED)
            self.assertEqual(target_path.read_text(encoding="utf-8"), "external_user_modifications")

    def test_adv_gate_18_recovery_preserves_external_changes_on_executed_modify(self):
        """18. Recovery rollback does not overwrite an externally modified file with before_state."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            store = InMemoryTransactionStore()
            rm = TransactionRecoveryManager(workspace=workspace, store=store)

            target_path = workspace / "important.py"
            target_path.write_text("v3_external_fix", encoding="utf-8")

            op = TransactionOperation(
                type=OperationType.MODIFY_FILE,
                target="important.py",
                status=OperationStatus.EXECUTED,
                before_state={"content": "v1_original", "is_text": True},
                after_state={"content": "v2_tx_changed", "is_text": True},
            )
            tx = Transaction(
                id="tx_crash_modify",
                workspace=str(workspace),
                status=TransactionStatus.EXECUTING,
                operations=[op],
                commit_marker=None,
            )
            store.save(tx)

            rec = rm.recover("tx_crash_modify")
            self.assertFalse(rec.recovered)
            self.assertFalse(rec.rolled_back)
            self.assertTrue(rec.ambiguous)
            self.assertEqual(rec.final_status, TransactionStatus.FAILED)
            # External edits must NOT be overwritten with v1_original!
            self.assertEqual(target_path.read_text(encoding="utf-8"), "v3_external_fix")

            # Repeated recovery attempt is idempotent
            rec2 = rm.recover("tx_crash_modify")
            self.assertFalse(rec2.recovered)
            self.assertTrue(rec2.ambiguous)
            self.assertEqual(target_path.read_text(encoding="utf-8"), "v3_external_fix")

    def test_adv_gate_19_recovery_preserves_external_file_on_executed_delete(self):
        """19. Recovery rollback does not overwrite an externally created file where DELETE occurred."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            store = InMemoryTransactionStore()
            rm = TransactionRecoveryManager(workspace=workspace, store=store)

            target_path = workspace / "deleted_file.txt"
            # An external file was created at the path that was deleted by the transaction
            target_path.write_text("external_recreated_file", encoding="utf-8")

            op = TransactionOperation(
                type=OperationType.DELETE_FILE,
                target="deleted_file.txt",
                status=OperationStatus.EXECUTED,
                before_state={"content": "tx_deleted_content", "is_text": True},
                after_state=None,
            )
            tx = Transaction(
                id="tx_crash_delete",
                workspace=str(workspace),
                status=TransactionStatus.EXECUTING,
                operations=[op],
                commit_marker=None,
            )
            store.save(tx)

            rec = rm.recover("tx_crash_delete")
            self.assertFalse(rec.recovered)
            self.assertFalse(rec.rolled_back)
            self.assertTrue(rec.ambiguous)
            self.assertEqual(rec.final_status, TransactionStatus.FAILED)
            # External file must NOT be overwritten with before_state!
            self.assertEqual(target_path.read_text(encoding="utf-8"), "external_recreated_file")

            # Repeated recovery attempt is idempotent
            rec2 = rm.recover("tx_crash_delete")
            self.assertFalse(rec2.recovered)
            self.assertTrue(rec2.ambiguous)
            self.assertEqual(target_path.read_text(encoding="utf-8"), "external_recreated_file")

    def test_adv_gate_20_recovery_preserves_external_changes_on_executed_rename(self):
        """20. Recovery rollback does not move or overwrite on RENAME if destination changed or source collides."""
        # Scenario A: destination was modified externally
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            store = InMemoryTransactionStore()
            rm = TransactionRecoveryManager(workspace=workspace, store=store)

            src_rel = "src.txt"
            dst_rel = "dst.txt"
            dst_path = workspace / dst_rel
            dst_path.write_text("external_edit_at_destination", encoding="utf-8")

            op = TransactionOperation(
                type=OperationType.RENAME_FILE,
                target=src_rel,
                new_target=dst_rel,
                status=OperationStatus.EXECUTED,
                before_state={"destination": dst_rel, "content": "original_content", "is_text": True},
                after_state={"destination": dst_rel, "content": "original_content", "is_text": True},
            )
            tx = Transaction(
                id="tx_crash_rename_dest_mod",
                workspace=str(workspace),
                status=TransactionStatus.EXECUTING,
                operations=[op],
                commit_marker=None,
            )
            store.save(tx)

            rec = rm.recover("tx_crash_rename_dest_mod")
            self.assertFalse(rec.recovered)
            self.assertTrue(rec.ambiguous)
            self.assertEqual(rec.final_status, TransactionStatus.FAILED)
            self.assertTrue(dst_path.exists())
            self.assertEqual(dst_path.read_text(encoding="utf-8"), "external_edit_at_destination")
            self.assertFalse((workspace / src_rel).exists())

        # Scenario B: source path has a newly created colliding file
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            store = InMemoryTransactionStore()
            rm = TransactionRecoveryManager(workspace=workspace, store=store)

            src_rel = "old_name.txt"
            dst_rel = "new_name.txt"
            (workspace / dst_rel).write_text("valid_after_state", encoding="utf-8")
            # Colliding file at source path
            (workspace / src_rel).write_text("colliding_file", encoding="utf-8")

            op = TransactionOperation(
                type=OperationType.RENAME_FILE,
                target=src_rel,
                new_target=dst_rel,
                status=OperationStatus.EXECUTED,
                before_state={"destination": dst_rel, "content": "valid_after_state", "is_text": True},
                after_state={"destination": dst_rel, "content": "valid_after_state", "is_text": True},
            )
            tx = Transaction(
                id="tx_crash_rename_src_collision",
                workspace=str(workspace),
                status=TransactionStatus.EXECUTING,
                operations=[op],
                commit_marker=None,
            )
            store.save(tx)

            rec = rm.recover("tx_crash_rename_src_collision")
            self.assertFalse(rec.recovered)
            self.assertTrue(rec.ambiguous)
            self.assertEqual(rec.final_status, TransactionStatus.FAILED)
            # Both files preserved without overwrite
            self.assertEqual((workspace / src_rel).read_text(encoding="utf-8"), "colliding_file")
            self.assertEqual((workspace / dst_rel).read_text(encoding="utf-8"), "valid_after_state")

    # =========================================================================
    # Remediation Priority 3 — Workflow status consistency
    # =========================================================================

    def test_adv_gate_21_workflow_status_predicates_and_effective_status(self):
        """21. workflow.py status predicates handle COMMITTED, COMMITTED_UNTESTED, and VERIFIED distinctly."""
        # 1. COMMITTED (legacy)
        self.assertTrue(is_tx_committed(TransactionStatus.COMMITTED))
        self.assertFalse(is_tx_verified(TransactionStatus.COMMITTED))
        self.assertFalse(is_tx_untested(TransactionStatus.COMMITTED))
        self.assertEqual(effective_tx_status(TransactionStatus.COMMITTED), "COMMITTED")

        # 2. COMMITTED_UNTESTED
        self.assertTrue(is_tx_committed(TransactionStatus.COMMITTED_UNTESTED))
        self.assertFalse(is_tx_verified(TransactionStatus.COMMITTED_UNTESTED))
        self.assertTrue(is_tx_untested(TransactionStatus.COMMITTED_UNTESTED))
        self.assertEqual(effective_tx_status(TransactionStatus.COMMITTED_UNTESTED), "COMMITTED_UNTESTED")

        # 3. VERIFIED
        self.assertTrue(is_tx_committed(TransactionStatus.VERIFIED))
        self.assertTrue(is_tx_verified(TransactionStatus.VERIFIED))
        self.assertFalse(is_tx_untested(TransactionStatus.VERIFIED))
        self.assertEqual(effective_tx_status(TransactionStatus.VERIFIED), "VERIFIED")

        # 4. ROLLED_BACK
        self.assertFalse(is_tx_committed(TransactionStatus.ROLLED_BACK))
        self.assertTrue(is_tx_rolled_back(TransactionStatus.ROLLED_BACK))
        self.assertFalse(is_tx_verified(TransactionStatus.ROLLED_BACK))

    def test_adv_gate_22_workflow_proof_verdict_and_recovery_requirements(self):
        """22. Workflow synthesizes correct proof verdict and recovery requirements per transaction outcome."""
        # A. COMMITTED_UNTESTED -> proof verdict FAILED, but NOT recovery_required
        v_untested = compute_proof_verdict(
            tx_status="committed_untested",
            tests_passed=False,
            gate_passed=False,
        )
        self.assertEqual(v_untested.status, "FAILED")
        self.assertIn("authoritative verification", v_untested.reason)

        # B. VERIFIED -> proof verdict VERIFIED
        v_verified = compute_proof_verdict(
            tx_status="verified",
            tests_passed=True,
            gate_passed=True,
        )
        self.assertEqual(v_verified.status, "VERIFIED")

        # C. COMMITTED with tests passed -> proof verdict VERIFIED
        v_committed_passed = compute_proof_verdict(
            tx_status="committed",
            tests_passed=True,
            gate_passed=True,
        )
        self.assertEqual(v_committed_passed.status, "VERIFIED")

        # D. COMMITTED with tests failed -> proof verdict FAILED
        v_committed_failed = compute_proof_verdict(
            tx_status="committed",
            tests_passed=False,
            gate_passed=False,
        )
        self.assertEqual(v_committed_failed.status, "FAILED")

    # =========================================================================
    # Remediation Priority 4 — Verification authority
    # =========================================================================

    def test_adv_gate_23_direct_commit_arguments_cannot_authorize_verified(self):
        """23. Direct commit(verified=True) or commit(status=VERIFIED) cannot yield VERIFIED without domain evidence."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)

            with WorkspaceLock(workspace):
                # Subcase A: commit(status=VERIFIED) without verification evidence -> fails closed to COMMITTED_UNTESTED
                coord_a = TransactionCoordinator(workspace=workspace)
                coord_a.begin()
                coord_a.stage_create("test_a.txt", "content")
                coord_a.execute()
                res_a = coord_a.commit(status=TransactionStatus.VERIFIED)
                self.assertEqual(res_a.status, TransactionStatus.COMMITTED_UNTESTED)
                self.assertEqual(res_a.verification_outcome, "untested")
                self.assertFalse(bool(res_a.verification and res_a.verification.get("domain_verified")))

                # Subcase B: commit(verified=True) without verification evidence -> fails closed to COMMITTED_UNTESTED
                coord_b = TransactionCoordinator(workspace=workspace)
                coord_b.begin()
                coord_b.stage_create("test_b.txt", "content")
                coord_b.execute()
                res_b = coord_b.commit(verified=True)
                self.assertEqual(res_b.status, TransactionStatus.COMMITTED_UNTESTED)
                self.assertEqual(res_b.verification_outcome, "untested")
                self.assertFalse(bool(res_b.verification and res_b.verification.get("domain_verified")))

    def test_adv_gate_24_basic_filesystem_verifier_cannot_produce_verified(self):
        """24. BasicFilesystemVerifier alone cannot produce VERIFIED state."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            with WorkspaceLock(workspace):
                coord = TransactionCoordinator(
                    workspace=workspace,
                    verifier=BasicFilesystemVerifier(),
                )
                coord.begin()
                coord.stage_create("fs_test.txt", "fs content")
                coord.execute()

                # coord.verify() with BasicFilesystemVerifier
                verify_ok = coord.verify()
                self.assertTrue(verify_ok)
                # Even though verify returned True, domain_verified must remain False
                self.assertFalse(coord.tx.verification_result.get("domain_verified", False))

                # Attempting commit(status=VERIFIED) must fail closed to COMMITTED_UNTESTED
                res = coord.commit(status=TransactionStatus.VERIFIED)
                self.assertEqual(res.status, TransactionStatus.COMMITTED_UNTESTED)
                self.assertFalse(res.verification.get("domain_verified", False))

    def test_adv_gate_25_legitimate_recorded_verification_yields_verified(self):
        """25. Trusted verification path via record_domain_verification yields VERIFIED."""
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            with WorkspaceLock(workspace):
                coord = TransactionCoordinator(workspace=workspace)
                coord.begin()
                coord.stage_create("legit.txt", "legit content")
                coord.execute()

                # Record domain verification evidence
                coord.record_domain_verification(
                    command="pytest -v",
                    passed=True,
                    returncode=0,
                    output_summary="10 passed in 0.2s",
                    details={"tests_passed": True},
                )
                self.assertTrue(coord.tx.verification_result.get("domain_verified"))
                self.assertTrue(coord.tx.verification_result.get("passed"))

                # Commit with status=VERIFIED succeeds
                res = coord.commit(status=TransactionStatus.VERIFIED)
                self.assertEqual(res.status, TransactionStatus.VERIFIED)
                self.assertEqual(res.verification_outcome, "verified")
                self.assertTrue(res.verification.get("domain_verified"))

                # Record failing domain verification -> commit fails closed to COMMITTED_UNTESTED
                coord_fail = TransactionCoordinator(workspace=workspace)
                coord_fail.begin()
                coord_fail.stage_create("fail.txt", "fail content")
                coord_fail.execute()
                coord_fail.record_domain_verification(
                    command="pytest -v",
                    passed=False,
                    returncode=1,
                    output_summary="1 failed",
                )
                res_fail = coord_fail.commit(status=TransactionStatus.VERIFIED)
                self.assertEqual(res_fail.status, TransactionStatus.COMMITTED_UNTESTED)
                self.assertEqual(res_fail.verification_outcome, "untested")
                self.assertFalse(res_fail.verification.get("domain_verified"))


if __name__ == "__main__":
    unittest.main()

