"""Phase 15G: Telegram buttons change presentation, never approval authority."""
from __future__ import annotations

import concurrent.futures
import tempfile
import time
import unittest
from pathlib import Path

from core.channels.telegram import MockTelegramTransport, TelegramChannel
from core.runtime.approval import (
    ApprovalService,
    ApprovalStatus,
    ApprovalTransitionContext,
    CanonicalOperation,
    FileApprovalStore,
    InMemoryApprovalStore,
)
from core.runtime.approval_presentation import (
    TELEGRAM_APPROVAL_MAX_CHARS,
    ApprovalPresentation,
    encode_telegram_callback,
    parse_telegram_callback,
    telegram_approval_keyboard,
)
from core.runtime.capabilities import Capabilities, FilesystemPolicy, GitPolicy
from core.runtime.messages import IncomingMessage
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import FileSessionStore, InMemorySessionStore, SessionManager


class TestTelegramApprovalPresentation(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryApprovalStore()
        self.service = ApprovalService(store=self.store, two_man_rule_enabled=True)

    def request(self, **kwargs):
        return self.service.create_request(
            session_id="telegram:requester:chat-1", channel="telegram",
            user_id="requester", conversation_id="chat-1",
            operation_type="write_files",
            canonical_operation=kwargs.pop("operation", CanonicalOperation(
                "write_files", "src/app.py", {"targets": ["src/app.py"]}
            )),
            capabilities=kwargs.pop("capabilities", Capabilities(
                filesystem=FilesystemPolicy(read=("src/app.py",), write=("src/app.py",))
            )),
            session_incarnation_id="inc-1", **kwargs,
        )

    def test_callback_is_bounded_deterministic_and_contains_only_action_and_id(self):
        req = self.request()
        payload = encode_telegram_callback("approve", req.request_id)
        self.assertEqual(parse_telegram_callback(payload), ("approve", req.request_id))
        self.assertLessEqual(len(payload.encode()), 64)
        self.assertNotIn("src/app.py", payload)
        self.assertNotIn(req.nonce, payload)

    def test_malformed_unknown_truncated_and_oversized_callbacks_fail_closed(self):
        bad = ["", "bf:x:req_12345678", "bf:a:req_123", "bf:a:req_12345678:extra", "x" * 65]
        for payload in bad:
            with self.subTest(payload=payload[:20]), self.assertRaises(ValueError):
                parse_telegram_callback(payload)

    def test_keyboard_has_no_embedded_authority(self):
        req = self.request()
        keyboard = telegram_approval_keyboard(req.request_id)
        payloads = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertEqual(len(payloads), 3)
        self.assertTrue(all(req.request_id in item and len(item.encode()) <= 64 for item in payloads))

    def test_secret_safe_bounded_rendering_and_target_count(self):
        token = "Bearer abcdefghijklmnopqrstuvwxyz123456"
        targets = [f"src/{i}_{token}.py" for i in range(30)]
        req = self.request()
        # Presentation is defensive even for an in-memory object corrupted after
        # canonical contract validation (normal creation already rejects this).
        req.canonical_operation = CanonicalOperation("write_files", targets[0], {"targets": targets})
        rendered = ApprovalPresentation.from_request(req, two_man_rule=True).render_telegram(now=req.created_at)
        self.assertNotIn(token, rendered)
        self.assertIn("\\[REDACTED\\_TOKEN\\]", rendered)
        self.assertLessEqual(len(rendered), TELEGRAM_APPROVAL_MAX_CHARS)
        self.assertLessEqual(rendered.count("• src/"), 8)
        self.assertIn("Targets: 30", rendered)
        self.assertIn("+22 additional targets", rendered)
        self.assertRegex(rendered, r"Scope fingerprint: [0-9a-f]{16}")
        self.assertNotIn(req.nonce, rendered)

    def test_target_scope_fidelity_long_paths_markdown_controls_and_unicode(self):
        common = "src/1_" + ("shared-prefix-" * 20)
        targets_a = [
            "src/0_alpha_*(one)[x].py",
            f"{common}alpha.py",
            "src/2_line\nfeed.py",
            "src/3_unicode-文件.py",
        ] + [f"src/{i + 4}_extra.py" for i in range(6)]
        targets_b = [*targets_a[:-1], "src/different.py"]
        req_a = self.request()
        req_b = self.request()
        req_a.canonical_operation = CanonicalOperation("write_files", targets_a[0], {"targets": targets_a})
        req_b.canonical_operation = CanonicalOperation("write_files", targets_b[0], {"targets": targets_b})
        view_a = ApprovalPresentation.from_request(req_a, two_man_rule=True)
        view_b = ApprovalPresentation.from_request(req_b, two_man_rule=True)
        rendered = view_a.render_telegram()
        self.assertEqual(view_a.target_count, 10)
        self.assertNotEqual(view_a.scope_fingerprint, view_b.scope_fingerprint)
        self.assertIn("+2 additional targets", rendered)
        self.assertIn("\\_\\*\\(one\\)\\[x\\]", rendered)
        self.assertIn("line feed.py", rendered)
        self.assertIn("unicode-文件.py", rendered)
        self.assertIn("[#", rendered)
        self.assertLessEqual(len(rendered), TELEGRAM_APPROVAL_MAX_CHARS)

    def test_private_key_variants_never_render_and_creation_fails_closed(self):
        variants = {
            key_type: f"-----BEGIN {key_type}-----\nQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n-----END {key_type}-----"
            for key_type in ("PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY",
                             "OPENSSH PRIVATE KEY", "DSA PRIVATE KEY",
                             "ENCRYPTED PRIVATE KEY")
        }
        for key_type in ("RSA PRIVATE KEY", "EC PRIVATE KEY", "DSA PRIVATE KEY"):
            variants[f"encrypted traditional {key_type}"] = (
                f"-----BEGIN {key_type}-----\n"
                "Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-256-CBC,0123456789ABCDEF\n\n"
                f"QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n-----END {key_type}-----"
            )
        for name, secret in variants.items():
            with self.subTest(key_type=name):
                with self.assertRaises(ValueError):
                    self.request(operation=CanonicalOperation("inspect", secret, {}))
                with self.assertRaises(ValueError):
                    self.request(operation=CanonicalOperation(
                        "inspect", "src/app.py", {"metadata": secret}
                    ))
                req = self.request()
                req.canonical_operation = CanonicalOperation("inspect", f"target/{secret}", {})
                rendered = ApprovalPresentation.from_request(req, two_man_rule=True).render_telegram()
                self.assertNotIn("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo", rendered)
                self.assertIn("\\[REDACTED\\_PRIVATE\\_KEY\\]", rendered)
                req.canonical_operation = CanonicalOperation(secret, "src/app.py", {})
                rendered_operation = ApprovalPresentation.from_request(req, two_man_rule=True).render_telegram()
                self.assertNotIn("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo", rendered_operation)

        multiple = f"unicode 文 {variants['PRIVATE KEY']} between {variants['ENCRYPTED PRIVATE KEY']}"
        req = self.request()
        req.canonical_operation = CanonicalOperation("inspect", multiple, {})
        rendered = ApprovalPresentation.from_request(req, two_man_rule=True).render_telegram()
        self.assertEqual(rendered.count("REDACTED"), 2)
        self.assertNotIn("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo", rendered)

        mismatched = (
            "-----BEGIN RSA PRIVATE KEY-----\nQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
            "-----END EC PRIVATE KEY-----"
        )
        with self.assertRaises(ValueError):
            self.request(operation=CanonicalOperation("inspect", mismatched, {}))

        oversized = "-----BEGIN PRIVATE KEY-----\n" + ("A" * 70000) + "\n-----END PRIVATE KEY-----"
        with self.assertRaises(ValueError):
            self.request(operation=CanonicalOperation("inspect", oversized, {}))

        example = "Documentation mentions BEGIN PRIVATE KEY without a complete PEM block."
        req = self.request()
        req.canonical_operation = CanonicalOperation("inspect", example, {})
        self.assertIn("BEGIN PRIVATE KEY", ApprovalPresentation.from_request(req, two_man_rule=True).render_telegram())

    def test_presentation_cannot_enable_git_push(self):
        with self.assertRaises(PermissionError):
            Capabilities(git=GitPolicy(push=True)).validate_executable()


class TestTelegramApprovalCallbacks(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = InMemoryApprovalStore()
        self.service = ApprovalService(store=self.store, two_man_rule_enabled=True)
        self.sessions = SessionManager(store=InMemorySessionStore())
        self.runtime = BrainFrogRuntime(
            repo_dir=Path(self.temp.name), sessions=self.sessions,
            approval_service=self.service, auto_recover_transactions=False,
        )
        self.requester_session = self.sessions.get_or_create("telegram", "requester", "chat-1")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def request(self, *, ttl=300):
        return self.service.create_request(
            session_id=self.requester_session.session_id, channel="telegram",
            user_id="requester", conversation_id="chat-1",
            operation_type="write_files",
            canonical_operation=CanonicalOperation("write_files", "src/app.py", {}),
            session_incarnation_id=self.requester_session.session_incarnation_id,
            capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/app.py",), write=("src/app.py",))
            ), workspace_root=self.temp.name, ttl_seconds=ttl,
        )

    def callback(self, req, action="approve", *, actor="approver", chat="chat-1", payload=None):
        return self.runtime.process_message(IncomingMessage(
            channel="telegram", user_id=actor, conversation_id=chat, text="",
            metadata={"telegram_callback_data": payload or encode_telegram_callback(action, req.request_id)},
        ))

    def test_valid_approve_reject_and_cancel(self):
        approve = self.request()
        self.assertTrue(self.callback(approve).success)
        self.assertEqual(self.store.get(approve.request_id).status, ApprovalStatus.APPROVED)
        reject = self.request()
        self.assertTrue(self.callback(reject, "reject").success)
        self.assertEqual(self.store.get(reject.request_id).status, ApprovalStatus.REJECTED)
        cancel = self.request()
        self.assertTrue(self.callback(cancel, "cancel", actor="requester").success)
        self.assertEqual(self.store.get(cancel.request_id).status, ApprovalStatus.CANCELLED)

    def test_second_approver_may_use_separate_private_conversation(self):
        req = self.request()
        out = self.callback(req, actor="approver", chat="approver-private-chat")
        self.assertTrue(out.success)
        self.assertEqual(self.store.get(req.request_id).status, ApprovalStatus.APPROVED)

    def test_whatsapp_second_approver_may_use_separate_private_conversation(self):
        owner = self.sessions.get_or_create("whatsapp", "111", "111")
        req = self.service.create_request(
            session_id=owner.session_id, channel="whatsapp", user_id="111",
            conversation_id="111", operation_type="write_files",
            canonical_operation=CanonicalOperation("write_files", "src/app.py", {}),
            session_incarnation_id=owner.session_incarnation_id,
        )
        out = self.runtime.process_message(IncomingMessage(
            channel="whatsapp", user_id="222", conversation_id="222",
            text=f"/approve {req.request_id}",
        ))
        self.assertTrue(out.success)
        self.assertEqual(self.store.get(req.request_id).status, ApprovalStatus.APPROVED)

    def test_missing_expired_and_replayed_callbacks_are_safe(self):
        req = self.request(ttl=-1)
        self.assertFalse(self.callback(req).success)
        self.assertEqual(self.store.get(req.request_id).status, ApprovalStatus.EXPIRED)


class TestCrossProcessApproverIncarnation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager1 = SessionManager(store=FileSessionStore(repo_dir=self.root))
        self.manager2 = SessionManager(store=FileSessionStore(repo_dir=self.root))
        self.store = FileApprovalStore(repo_dir=self.root)
        self.service = ApprovalService(store=self.store, two_man_rule_enabled=True)
        self.runtime1 = BrainFrogRuntime(
            repo_dir=self.root, sessions=self.manager1,
            approval_service=self.service, auto_recover_transactions=False,
        )
        self.runtime = self.runtime1
        self.owner = self.manager1.get_or_create("telegram", "requester", "owner-private")
        self.requester_session = self.owner
        self.stale_approver = self.manager1.get_or_create(
            "telegram", "approver", "approver-private"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def request(self, channel="telegram", *, ttl=300):
        owner = self.owner
        if channel != "telegram":
            owner = self.manager1.get_or_create(channel, "requester", "owner-private")
        return self.service.create_request(
            session_id=owner.session_id, channel=channel, user_id="requester",
            conversation_id="owner-private", operation_type="write_files",
            canonical_operation=CanonicalOperation("write_files", "src/app.py", {}),
            session_incarnation_id=owner.session_incarnation_id,
            ttl_seconds=ttl,
        )

    def callback(self, req, action="approve", *, actor="approver",
                 chat="approver-private", payload=None):
        return self.runtime.process_message(IncomingMessage(
            channel="telegram", user_id=actor, conversation_id=chat, text="",
            metadata={"telegram_callback_data": payload or encode_telegram_callback(
                action, req.request_id
            )},
        ))

    def reset_approver_elsewhere(self, channel="telegram"):
        session_id = f"{channel}:approver:approver-private"
        old = self.manager1.get_or_create(channel, "approver", "approver-private")
        self.manager2.reset(session_id)
        current = self.manager2.get_or_create(channel, "approver", "approver-private")
        self.assertNotEqual(old.session_incarnation_id, current.session_incarnation_id)

    def test_stale_cross_process_approver_fails_callback_slash_and_whatsapp(self):
        self.reset_approver_elsewhere()
        callback_req = self.request()
        callback = self.runtime1.process_message(IncomingMessage(
            channel="telegram", user_id="approver", conversation_id="approver-private", text="",
            metadata={
                "telegram_callback_data": encode_telegram_callback("approve", callback_req.request_id),
                "session_incarnation_id": "forged-client-value",
            },
        ))
        self.assertFalse(callback.success)
        self.assertEqual(self.store.get(callback_req.request_id).status, ApprovalStatus.PENDING)

        slash_req = self.request()
        slash = self.runtime1.process_message(IncomingMessage(
            channel="telegram", user_id="approver", conversation_id="approver-private",
            text=f"/approve {slash_req.request_id}",
        ))
        self.assertFalse(slash.success)
        self.assertEqual(self.store.get(slash_req.request_id).status, ApprovalStatus.PENDING)

        self.manager1.get_or_create("whatsapp", "approver", "approver-private")
        self.reset_approver_elsewhere("whatsapp")
        whatsapp_req = self.request("whatsapp")
        whatsapp = self.runtime1.process_message(IncomingMessage(
            channel="whatsapp", user_id="approver", conversation_id="approver-private",
            text=f"/approve {whatsapp_req.request_id}",
        ))
        self.assertFalse(whatsapp.success)
        self.assertEqual(self.store.get(whatsapp_req.request_id).status, ApprovalStatus.PENDING)

    def test_restarted_runtime_uses_current_incarnation_and_separate_conversation(self):
        self.reset_approver_elsewhere()
        current_manager = SessionManager(store=FileSessionStore(repo_dir=self.root))
        current_runtime = BrainFrogRuntime(
            repo_dir=self.root, sessions=current_manager,
            approval_service=self.service, auto_recover_transactions=False,
        )
        req = self.request()
        result = current_runtime.process_message(IncomingMessage(
            channel="telegram", user_id="approver", conversation_id="approver-private", text="",
            metadata={
                "telegram_callback_data": encode_telegram_callback("approve", req.request_id),
                "session_incarnation_id": "ignored-forgery",
            },
        ))
        self.assertTrue(result.success)
        self.assertEqual(self.store.get(req.request_id).status, ApprovalStatus.APPROVED)

    def test_approval_and_reset_are_serialized_without_stale_success(self):
        req = self.request()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            reset_future = pool.submit(
                self.manager2.reset, self.stale_approver.session_id
            )
            reset_future.result()
            approval_future = pool.submit(
                self.runtime1.process_message,
                IncomingMessage(
                    channel="telegram", user_id="approver",
                    conversation_id="approver-private", text=f"/approve {req.request_id}",
                ),
            )
            result = approval_future.result()
        self.assertFalse(result.success)
        self.assertEqual(self.store.get(req.request_id).status, ApprovalStatus.PENDING)


class TestTelegramApprovalCallbacksContinued(unittest.TestCase):
    setUp = TestTelegramApprovalCallbacks.setUp
    tearDown = TestTelegramApprovalCallbacks.tearDown
    request = TestTelegramApprovalCallbacks.request
    callback = TestTelegramApprovalCallbacks.callback

    def test_missing_replayed_callbacks_are_safe(self):
        req2 = self.request()
        self.assertTrue(self.callback(req2).success)
        self.assertFalse(self.callback(req2).success)
        self.assertEqual(self.store.get(req2.request_id).status, ApprovalStatus.APPROVED)
        missing = self.callback(req2, payload="bf:a:req_00000000")
        self.assertFalse(missing.success)

    def test_wrong_actor_session_channel_and_incarnation_fail_closed(self):
        req = self.request()
        self.assertFalse(self.callback(req, actor="requester", chat="wrong-chat").success)
        forged = IncomingMessage(
            channel="whatsapp", user_id="approver", conversation_id="chat-1", text="",
            metadata={"telegram_callback_data": encode_telegram_callback("approve", req.request_id)},
        )
        self.assertFalse(self.runtime.process_message(forged).success)
        self.sessions.reset(self.requester_session.session_id)
        stale = self.callback(req)
        self.assertFalse(stale.success)
        self.assertIn("stale approval rejected", stale.text)

    def test_two_man_rule_and_cancel_actor_binding(self):
        req = self.request()
        self.assertFalse(self.callback(req, actor="requester").success)
        self.assertEqual(self.store.get(req.request_id).status, ApprovalStatus.PENDING)
        self.assertTrue(self.callback(req, actor="approver").success)
        cancel = self.request()
        self.assertFalse(self.callback(cancel, "cancel", actor="intruder").success)

    def test_stale_ui_observes_canonical_external_state(self):
        req = self.request()
        self.service.reject(req.request_id, "operator", "telegram")
        out = self.callback(req)
        self.assertFalse(out.success)
        self.assertEqual(self.store.get(req.request_id).status, ApprovalStatus.REJECTED)

    def test_callback_cannot_alter_contract_fields_or_git_policy(self):
        req = self.request()
        before = req.authorization_payload()
        out = self.callback(req)
        self.assertTrue(out.success)
        after = self.store.get(req.request_id)
        self.assertEqual(after.authorization_payload(), before)
        self.assertFalse(after.capabilities.git.push)
        self.assertTrue(after.integrity_valid())

    def test_concurrent_approve_reject_has_exactly_one_winner(self):
        req = self.request()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda action: self.callback(req, action, actor="approver"),
                ("approve", "reject"),
            ))
        self.assertEqual(sum(result.success for result in results), 1)
        self.assertIn(self.store.get(req.request_id).status, {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED})

    def test_transport_authenticates_callback_and_routes_compact_payload(self):
        req = self.request()
        transport = MockTelegramTransport()
        channel = TelegramChannel(
            bot_token="test", allowed_users=["approver"], rate_limit_seconds=0,
            transport=transport,
        )
        channel.set_handler(self.runtime.process_message)
        out = channel.process_update({
            "update_id": 9,
            "callback_query": {
                "id": "cb-1", "from": {"id": "approver"},
                "message": {"chat": {"id": "chat-1", "type": "private"}},
                "data": encode_telegram_callback("approve", req.request_id),
            },
        })
        self.assertTrue(out.success)
        self.assertEqual(self.store.get(req.request_id).status, ApprovalStatus.APPROVED)
        self.assertEqual(transport.answered_callbacks[0]["callback_query_id"], "cb-1")
        self.assertIn("Approval granted", transport.answered_callbacks[0]["text"])

        blocked = self.request()
        denied = channel.process_update({
            "update_id": 10,
            "callback_query": {
                "id": "cb-2", "from": {"id": "intruder"},
                "message": {"chat": {"id": "chat-1", "type": "private"}},
                "data": encode_telegram_callback("approve", blocked.request_id),
            },
        })
        self.assertFalse(denied.success)
        self.assertEqual(self.store.get(blocked.request_id).status, ApprovalStatus.PENDING)

    def test_expired_replays_never_rewrite_terminal_states(self):
        context = ApprovalTransitionContext(actor_id="approver", channel="telegram")
        consumed = self.request(ttl=0.01)
        self.service.transition(consumed.request_id, "approve", context)
        self.service.verify_and_consume(
            consumed.request_id, consumed.operation_digest, consumed.session_id,
            consumed.channel, consumed.session_incarnation_id, consumed.user_id,
        )
        rejected = self.request(ttl=0.01)
        self.service.transition(rejected.request_id, "reject", context)
        cancelled = self.request(ttl=0.01)
        self.service.transition(
            cancelled.request_id, "cancel",
            ApprovalTransitionContext(actor_id="requester", channel="telegram"),
        )
        expired = self.request(ttl=-1)
        self.service.transition(expired.request_id, "approve", context)
        time.sleep(0.02)

        cases = [
            (consumed, ApprovalStatus.CONSUMED, "reject"),
            (consumed, ApprovalStatus.CONSUMED, "approve"),
            (rejected, ApprovalStatus.REJECTED, "approve"),
            (cancelled, ApprovalStatus.CANCELLED, "reject"),
            (expired, ApprovalStatus.EXPIRED, "approve"),
        ]
        for req, expected, action in cases:
            with self.subTest(status=expected, action=action):
                ok, _, _ = self.service.transition(req.request_id, action, context)
                self.assertFalse(ok)
                self.assertEqual(self.store.get(req.request_id).status, expected)

    def test_concurrent_expired_transitions_remain_expired(self):
        req = self.request(ttl=-1)
        context = ApprovalTransitionContext(actor_id="approver", channel="telegram")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda action: self.service.transition(req.request_id, action, context),
                ("approve", "reject"),
            ))
        self.assertFalse(any(result[0] for result in results))
        self.assertEqual(self.store.get(req.request_id).status, ApprovalStatus.EXPIRED)


if __name__ == "__main__":
    unittest.main()
