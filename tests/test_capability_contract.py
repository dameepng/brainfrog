"""Phase 15A adversarial capability and approval-integrity regressions."""
import copy
import json
import os
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from core.runtime.approval import (
    ApprovalRequest, ApprovalService, ApprovalStatus, CanonicalOperation,
    FileApprovalStore, InMemoryApprovalStore,
)
from core.runtime.capabilities import (
    Capabilities, FilesystemPolicy, GitPolicy, NetworkPolicy, ShellPolicy,
)
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.runtime import BrainFrogRuntime
from orchestrator import Orchestrator, RunConfig, _write_files
from tests.test_approval_execution_contract import MockSystem1, MockSystem2


class CapabilityContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.caps = Capabilities(FilesystemPolicy(read=("src/auth.py",), write=("src/auth.py",)),
                                 network=NetworkPolicy(True))
        self.service = ApprovalService(InMemoryApprovalStore())

    def request(self, service=None, capabilities=None):
        return (service or self.service).create_request(
            session_id="session", session_incarnation_id="incarnation", channel="telegram",
            user_id="alice", conversation_id="chat", operation_type="write_file",
            canonical_operation=CanonicalOperation("write_file", "src/auth.py"),
            capabilities=capabilities or self.caps, workspace_root=str(self.root),
        )

    def consume(self, request, service=None):
        return (service or self.service).verify_and_consume(
            request.request_id, request.operation_digest, "session", "telegram",
            session_incarnation_id="incarnation", requester_id="alice",
        )

    def contract(self):
        req = self.request()
        self.assertTrue(self.service.approve(req.request_id, "bob", "telegram")[0])
        self.assertTrue(self.consume(req)[0])
        return ApprovedExecutionContract.from_approval_request(req, self.root)

    def test_default_deny(self):
        for name in ("filesystem.read", "filesystem.write", "shell.execute", "network.access",
                     "git.read", "git.commit", "git.push", "unknown", "shell.anything"):
            with self.subTest(name=name), self.assertRaises(PermissionError):
                Capabilities().require(name, "src/auth.py", self.root)

    def test_explicit_vocabulary_recognized(self):
        caps = Capabilities(FilesystemPolicy(("src/auth.py",), ("src/auth.py",)),
                            ShellPolicy(True), NetworkPolicy(True), GitPolicy(True, True, True))
        for name in ("filesystem.read", "filesystem.write", "shell.execute", "network.access",
                     "git.read", "git.commit", "git.push"):
            caps.require(name, "src/auth.py", self.root)
        with self.assertRaises(PermissionError):
            caps.validate_executable()

    def test_unknown_malformed_and_non_boolean(self):
        for data in ({"shell.execute": True}, {"root": True}, {"shell": True},
                     {"shell": {"execute": 1}}, {"network": {"access": "true"}},
                     {"git": {"admin": True}}, {"filesystem": {"write": "src/auth.py"}},
                     {"filesystem": {"write": [12]}}, [], None):
            with self.subTest(data=data), self.assertRaises(ValueError):
                Capabilities.from_dict(data)

    def test_capability_round_trip_and_order(self):
        caps = Capabilities(FilesystemPolicy(write=["b.py", "a.py", "b.py"]))
        self.assertEqual(caps.filesystem.write, ("a.py", "b.py"))
        self.assertEqual(Capabilities.from_dict(caps.to_dict()), caps)

    def test_scope_read_write_and_zero_partial_write(self):
        contract = self.contract()
        contract.require_filesystem("read", "src/auth.py", self.root)
        with self.assertRaises(PermissionError):
            contract.require_filesystem("read", "private.py", self.root)
        with self.assertRaises(PermissionError):
            _write_files(self.root, {"src/auth.py": "safe", "private.py": "bad"}, contract=contract)
        self.assertFalse((self.root / "src/auth.py").exists())
        _write_files(self.root, {"src/auth.py": "safe"}, contract=contract)
        self.assertEqual((self.root / "src/auth.py").read_text(), "safe")

    def test_target_presence_never_grants_write(self):
        contract = ApprovedExecutionContract("r", "write_file", frozenset({"safe.py"}),
                                             "digest", "cli", expires_at=time.time() + 30)
        with self.assertRaises(PermissionError):
            _write_files(self.root, {"safe.py": "bad"}, contract=contract)

    def test_write_scope_cannot_exceed_operation(self):
        with self.assertRaises(ValueError):
            ApprovedExecutionContract("r", "write_file", frozenset({"safe.py"}), "d", "cli",
                                      capabilities=Capabilities(FilesystemPolicy(write=("evil.py",))))

    def test_immutable_and_detached_serialization(self):
        contract = self.contract()
        with self.assertRaises(FrozenInstanceError):
            contract.channel = "cli"
        with self.assertRaises(FrozenInstanceError):
            contract.capabilities.shell.execute = True
        contract.canonical_operation.parameters["targets"] = ["evil.py"]
        encoded = contract.to_dict()
        encoded["capabilities"]["filesystem"]["write"].append("evil.py")
        self.assertEqual(contract.capabilities.filesystem.write, ("src/auth.py",))
        contract.validate_integrity()

    def test_deterministic_round_trip(self):
        contract = self.contract()
        text = contract.to_canonical_json()
        self.assertEqual(ApprovedExecutionContract.from_dict(json.loads(text)), contract)
        self.assertEqual(text, contract.to_canonical_json())

    def test_contract_deserialization_rejects_escalation(self):
        contract = self.contract()
        mutations = {
            "channel": "cli", "actor": "mallory", "session_id": "other",
            "session_incarnation_id": "new", "operation_digest": "forged",
            "action_type": "git_push", "approved_targets": ["evil.py"],
            "expires_at": time.time() + 10000, "allow_remote_git_push": True,
            "workspace_root": str(self.root.parent), "unknown": True,
        }
        for key, value in mutations.items():
            data = contract.to_dict()
            data[key] = value
            with self.subTest(field=key), self.assertRaises(ValueError):
                ApprovedExecutionContract.from_dict(data)
        for group, action, value in (("shell", "execute", True), ("git", "push", True),
                                     ("filesystem", "write", ["**"]),
                                     ("filesystem", "read", ["private.py"])):
            data = contract.to_dict()
            data["capabilities"][group][action] = value
            with self.subTest(group=group), self.assertRaises(ValueError):
                ApprovedExecutionContract.from_dict(data)

    def test_expiry_and_binding(self):
        contract = self.contract()
        args = dict(actor="alice", channel="telegram", session_id="session",
                    session_incarnation_id="incarnation", repo_dir=self.root)
        contract.validate(**args)
        for key in ("actor", "channel", "session_id", "session_incarnation_id"):
            with self.subTest(key=key), self.assertRaises(PermissionError):
                contract.validate(**{**args, key: "other"})
        with patch("core.runtime.contract.time.time", return_value=contract.expires_at):
            with self.assertRaises(ValueError):
                ApprovedExecutionContract.from_dict(contract.to_dict())
            with self.assertRaises(PermissionError):
                contract.validate(**args)
            with self.assertRaises(PermissionError):
                _write_files(self.root, {"src/auth.py": "late"}, contract=contract)

    def test_absolute_path_inside_workspace_is_denied(self):
        contract = self.contract()
        with self.assertRaises(PermissionError):
            _write_files(self.root, {str(self.root / "src/auth.py"): "bad"}, contract=contract)

    def test_approver_binding_cannot_change(self):
        req = self.request()
        self.service.approve(req.request_id, "bob", "telegram")
        req.approver_id = "mallory"
        self.assertFalse(self.consume(req)[0])

    def test_capability_mutation_before_approval_fails(self):
        req = self.request()
        req.capabilities = Capabilities(shell=ShellPolicy(True))
        self.assertFalse(self.service.approve(req.request_id, "bob", "telegram")[0])

    def test_approved_operation_mutation_fails(self):
        req = self.request()
        self.service.approve(req.request_id, "bob", "telegram")
        req.canonical_operation.action_type = "git_push"
        self.assertFalse(self.consume(req)[0])

    def test_network_scope_cannot_expand(self):
        for scope in ("all", "https://example.com", True, None):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                Capabilities.from_dict({"network": {"access": True, "scope": scope}})

    def test_unsupported_git_and_shell_grants_cannot_execute(self):
        for caps in (Capabilities(shell=ShellPolicy(True)), Capabilities(git=GitPolicy(read=True)),
                     Capabilities(git=GitPolicy(commit=True)), Capabilities(git=GitPolicy(push=True))):
            with self.subTest(caps=caps), self.assertRaises(PermissionError):
                caps.validate_executable()

    def test_single_segment_symlink_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root / "safe.py").symlink_to(Path(outside) / "secret.py")
            except OSError:
                self.skipTest("Symlink creation unavailable")
            with self.assertRaises(PermissionError):
                Capabilities(FilesystemPolicy(read=("safe.py",))).require("filesystem.read", "safe.py", self.root)

    def test_replay(self):
        req = self.request()
        self.service.approve(req.request_id, "bob", "telegram")
        self.assertTrue(self.consume(req)[0])
        self.assertFalse(self.consume(req)[0])

    def test_legacy_records_fail_closed(self):
        req = self.request()
        data = req.to_dict()
        data["schema_version"] = 1
        data.pop("capabilities")
        data.pop("authorization_digest")
        legacy = ApprovalRequest.from_dict(data)
        self.service.store.save(legacy)
        self.assertFalse(self.service.approve(req.request_id, "bob", "telegram")[0])

    def test_unknown_persisted_fields_rejected(self):
        data = self.request().to_dict()
        data["shell.execute"] = True
        with self.assertRaises(ValueError):
            ApprovalRequest.from_dict(data)

    def test_secret_fields_rejected_before_persistence(self):
        for params in ({"password": "never-store-this"}, {"api_key": "private"},
                       {"token": "opaque-credential"}, {"Authorization": "Basic opaque"},
                       {"nested": {"credential": "private"}}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.service.create_request("s", "cli", "alice", "c", "write_file",
                                            CanonicalOperation("write_file", "safe.py", params))
        self.assertEqual(self.service.store.list_requests(), [])

    def test_known_environment_secret_rejected(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "unique-private-material"}):
            with self.assertRaises(ValueError):
                self.service.create_request("s", "cli", "alice", "c", "write_file",
                    CanonicalOperation("write_file", "safe.py", {"value": "unique-private-material"}))

    def test_remote_git_policy_cannot_be_overridden(self):
        contract = ApprovedExecutionContract("r", "write_file", frozenset(), "d", "telegram",
                    allow_remote_git_push=True,
                    capabilities=Capabilities(network=NetworkPolicy(True), git=GitPolicy(push=True)))
        self.assertFalse(contract.allows_remote_git_push())

    def test_runtime_policy_ignores_proposed_capabilities(self):
        op = CanonicalOperation("write_file", "src/auth.py", {"capabilities": {"shell.execute": True}})
        self.assertFalse(BrainFrogRuntime._approval_capabilities(op).shell.execute)

    def test_no_shell_git_or_unscoped_context_in_orchestrator(self):
        contract = self.contract()
        (self.root / "private.py").write_text("private")
        config = RunConfig(repo_dir=self.root, test_command=["forbidden"], task="write_file src/auth.py",
                           execution_contract=contract, origin_channel="telegram", actor=contract.actor, session_id=contract.session_id, session_incarnation_id=contract.session_incarnation_id)
        with patch("orchestrator._run", side_effect=AssertionError("Unexpected subprocess")), \
             patch("orchestrator.load_project_guidelines", side_effect=AssertionError("Unscoped read")):
            engine = Orchestrator(MockSystem1(), MockSystem2({"src/auth.py": "bounded"}), config)
            result = engine.run()
        self.assertEqual(result[0].outcome, "verified")
        self.assertEqual((self.root / "src/auth.py").read_text(), "bounded")

    def test_missing_network_stops_before_provider(self):
        req = self.request(capabilities=Capabilities(self.caps.filesystem))
        self.service.approve(req.request_id, "bob", "telegram")
        self.consume(req)
        contract = ApprovedExecutionContract.from_approval_request(req, self.root)
        config = RunConfig(repo_dir=self.root, test_command=["forbidden"], task="write_file src/auth.py",
                           execution_contract=contract, origin_channel="telegram", actor=contract.actor, session_id=contract.session_id, session_incarnation_id=contract.session_incarnation_id)
        with patch.object(MockSystem2, "plan_task", side_effect=AssertionError("Provider contacted")):
            engine = Orchestrator(MockSystem1(), MockSystem2(), config)
            with self.assertRaises(PermissionError):
                engine.run()

    def test_symlink_escape(self):
        (self.root / "src").mkdir()
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root / "src/auth.py").symlink_to(Path(outside) / "secret.py")
            except OSError:
                self.skipTest("Symlink creation unavailable")
            with self.assertRaises(ValueError):
                self.contract()

    def test_orchestrator_actor_mismatch_denied(self):
        contract = self.contract()
        config = RunConfig(
            repo_dir=self.root, test_command=["forbidden"], task="write_file src/auth.py",
            execution_contract=contract, origin_channel="telegram",
            actor="mallory", session_id=contract.session_id,
            session_incarnation_id=contract.session_incarnation_id,
        )
        engine = Orchestrator(MockSystem1(), MockSystem2(), config)
        with self.assertRaises(PermissionError) as ctx:
            engine.run()
        self.assertIn("Contract session/actor binding mismatch", str(ctx.exception))

    def test_orchestrator_session_id_mismatch_denied(self):
        contract = self.contract()
        config = RunConfig(
            repo_dir=self.root, test_command=["forbidden"], task="write_file src/auth.py",
            execution_contract=contract, origin_channel="telegram",
            actor=contract.actor, session_id="stolen_session",
            session_incarnation_id=contract.session_incarnation_id,
        )
        engine = Orchestrator(MockSystem1(), MockSystem2(), config)
        with self.assertRaises(PermissionError) as ctx:
            engine.run()
        self.assertIn("Contract session/actor binding mismatch", str(ctx.exception))

    def test_orchestrator_session_incarnation_mismatch_denied(self):
        contract = self.contract()
        config = RunConfig(
            repo_dir=self.root, test_command=["forbidden"], task="write_file src/auth.py",
            execution_contract=contract, origin_channel="telegram",
            actor=contract.actor, session_id=contract.session_id,
            session_incarnation_id="stale_incarnation",
        )
        engine = Orchestrator(MockSystem1(), MockSystem2(), config)
        with self.assertRaises(PermissionError) as ctx:
            engine.run()
        self.assertIn("Contract session/actor binding mismatch", str(ctx.exception))

    def test_orchestrator_missing_identity_denied(self):
        contract = self.contract()
        config = RunConfig(
            repo_dir=self.root, test_command=["forbidden"], task="write_file src/auth.py",
            execution_contract=contract, origin_channel="telegram",
        )
        engine = Orchestrator(MockSystem1(), MockSystem2(), config)
        with self.assertRaises(PermissionError) as ctx:
            engine.run()
        self.assertIn("Explicit execution identity", str(ctx.exception))

    def test_orchestrator_matching_identity_allowed(self):
        contract = self.contract()
        config = RunConfig(
            repo_dir=self.root, test_command=["forbidden"], task="write_file src/auth.py",
            execution_contract=contract, origin_channel="telegram",
            actor=contract.actor, session_id=contract.session_id,
            session_incarnation_id=contract.session_incarnation_id,
        )
        engine = Orchestrator(MockSystem1(), MockSystem2({"src/auth.py": "updated"}), config)
        results = engine.run()
        self.assertEqual(results[0].outcome, "verified")
        self.assertEqual((self.root / "src/auth.py").read_text(), "updated")

    def test_runtime_approved_execution_with_explicit_identity_e2e(self):
        from core.runtime.messages import IncomingMessage
        runtime = BrainFrogRuntime(
            repo_dir=self.root,
            approval_service=self.service,
            system1_factory=lambda b: MockSystem1(),
            system2_factory=lambda **kw: MockSystem2({"src/auth.py": "e2e_pass"}),
            require_approval=True,
        )
        req_msg = IncomingMessage(
            text="write_file src/auth.py", channel="telegram", user_id="alice",
            conversation_id="chat",
        )
        res_req = runtime.process_message(req_msg)
        self.assertEqual(res_req.status, "pending_approval")
        req_id = res_req.metadata["request_id"]
        self.assertTrue(self.service.approve(req_id, approver_id="bob", channel="telegram")[0])
        exec_msg = IncomingMessage(
            text=f"/exec {req_id}", channel="telegram", user_id="alice",
            conversation_id="chat",
        )
        res_exec = runtime.process_message(exec_msg)
        self.assertTrue(res_exec.success)
        self.assertEqual((self.root / "src/auth.py").read_text(), "e2e_pass")

    def test_runtime_remote_execution_binding_and_antispoofing(self):
        from core.runtime.messages import IncomingMessage
        runtime = BrainFrogRuntime(
            repo_dir=self.root,
            approval_service=self.service,
            system1_factory=lambda b: MockSystem1(),
            system2_factory=lambda **kw: MockSystem2({"src/auth.py": "safe"}),
            require_approval=True,
        )
        req_msg = IncomingMessage(
            text="write_file src/auth.py", channel="whatsapp", user_id="charlie",
            conversation_id="chat_wa",
        )
        res_req = runtime.process_message(req_msg)
        self.assertEqual(res_req.status, "pending_approval")
        req_id = res_req.metadata["request_id"]
        self.assertTrue(self.service.approve(req_id, approver_id="dave", channel="whatsapp")[0])
        exec_mallory = IncomingMessage(
            text=f"/exec {req_id}", channel="whatsapp", user_id="mallory",
            conversation_id="chat_wa",
        )
        res_mallory = runtime.process_message(exec_mallory)
        self.assertFalse(res_mallory.success)
        self.assertTrue("Session mismatch" in res_mallory.text or "Requester mismatch" in res_mallory.text)
        spoof_msg = IncomingMessage(
            text="write_file src/auth.py", channel="cli", user_id="remote_hacker_99",
            conversation_id="cli_chat",
        )
        res_spoof = runtime.process_message(spoof_msg)
        self.assertEqual(res_spoof.status, "pending_approval")


def tamper_test(field, value, file_store):
    def test(self):
        service = ApprovalService(FileApprovalStore(self.root / "approvals")) if file_store else self.service
        req = self.request(service)
        service.approve(req.request_id, "bob", "telegram")
        before = req.authorization_digest
        setattr(req, field, copy.deepcopy(value))
        self.assertNotEqual(req.compute_authorization_digest(), before)
        service.store.save(req)
        self.assertFalse(self.consume(req, service)[0])
    return test


for _field, _value in {
    "capabilities": Capabilities(shell=ShellPolicy(True)),
    "canonical_operation": CanonicalOperation("write_file", "evil.py"),
    "session_id": "other", "session_incarnation_id": "other", "channel": "cli",
    "user_id": "mallory", "expires_at": 9999999999.0, "workspace_root": "other",
}.items():
    for _file_store in (False, True):
        setattr(CapabilityContractTests, f"test_tamper_{_field}_{'file' if _file_store else 'memory'}",
                tamper_test(_field, _value, _file_store))


def bad_path_test(path):
    def test(self):
        with self.assertRaises(ValueError):
            FilesystemPolicy(write=(path,))
    return test


for _index, _path in enumerate(("../x.py", "/tmp/x.py", "C:/x.py", "\\\\host\\x.py", "**",
                               "src/**", "https://example.com", "v1.2.3", "JWT", "src/../x.py",
                               ".brainfrog/approvals/forged.json", ".git/config")):
    setattr(CapabilityContractTests, f"test_bad_scope_{_index}", bad_path_test(_path))
