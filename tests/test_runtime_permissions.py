"""Unit tests for runtime channel permissions and security boundaries."""
from __future__ import annotations

import unittest
from core.runtime.permissions import (
    ChannelTrustLevel,
    PermissionAction,
    evaluate_channel_action,
    get_default_policy,
    resolve_channel_trust_level,
)


class TestRuntimePermissions(unittest.TestCase):
    def test_trust_level_resolution(self):
        self.assertEqual(resolve_channel_trust_level("cli"), ChannelTrustLevel.LOCAL_CLI.value)
        self.assertEqual(resolve_channel_trust_level("local"), ChannelTrustLevel.LOCAL_CLI.value)
        self.assertEqual(resolve_channel_trust_level("terminal"), ChannelTrustLevel.LOCAL_CLI.value)

        self.assertEqual(resolve_channel_trust_level("telegram"), ChannelTrustLevel.REMOTE_CHANNEL.value)
        self.assertEqual(resolve_channel_trust_level("whatsapp"), ChannelTrustLevel.REMOTE_CHANNEL.value)
        self.assertEqual(resolve_channel_trust_level("discord"), ChannelTrustLevel.REMOTE_CHANNEL.value)
        self.assertEqual(resolve_channel_trust_level("unknown"), ChannelTrustLevel.REMOTE_CHANNEL.value)

    def test_cli_high_trust_permissions(self):
        pol = get_default_policy("cli")
        self.assertEqual(pol.trust_level, ChannelTrustLevel.LOCAL_CLI.value)
        self.assertTrue(pol.is_allowed(PermissionAction.READ_CODE))
        self.assertTrue(pol.is_allowed(PermissionAction.WRITE_CODE))
        self.assertTrue(pol.is_allowed(PermissionAction.SHELL_EXECUTION))
        self.assertTrue(pol.is_allowed(PermissionAction.CREDENTIAL_ACCESS))
        self.assertTrue(pol.is_allowed(PermissionAction.GIT_SAFE_OPS))

        allowed, _ = evaluate_channel_action("cli", PermissionAction.SHELL_EXECUTION)
        self.assertTrue(allowed)

    def test_remote_channel_prohibited_actions(self):
        for ch in ("telegram", "whatsapp"):
            pol = get_default_policy(ch)
            self.assertEqual(pol.trust_level, ChannelTrustLevel.REMOTE_CHANNEL.value)

            # Unconditional prohibitions
            self.assertFalse(pol.is_allowed(PermissionAction.SHELL_EXECUTION))
            self.assertFalse(pol.is_allowed(PermissionAction.GIT_DESTRUCTIVE))
            self.assertFalse(pol.is_allowed(PermissionAction.CREDENTIAL_ACCESS))
            self.assertFalse(pol.is_allowed(PermissionAction.DEPLOYMENT))

            allowed, reason = evaluate_channel_action(ch, PermissionAction.SHELL_EXECUTION)
            self.assertFalse(allowed)
            self.assertIn("strictly prohibited", reason)

            allowed, reason = evaluate_channel_action(ch, PermissionAction.GIT_DESTRUCTIVE)
            self.assertFalse(allowed)

    def test_remote_channel_read_and_diagnose_allowed(self):
        for ch in ("telegram", "whatsapp"):
            pol = get_default_policy(ch)
            self.assertTrue(pol.is_allowed(PermissionAction.READ_CODE))
            self.assertTrue(pol.is_allowed(PermissionAction.DIAGNOSE))
            self.assertTrue(pol.is_allowed(PermissionAction.PLAN))

            allowed, _ = evaluate_channel_action(ch, PermissionAction.READ_CODE)
            self.assertTrue(allowed)
            allowed, _ = evaluate_channel_action(ch, PermissionAction.DIAGNOSE)
            self.assertTrue(allowed)

    def test_remote_channel_code_edits_gating(self):
        # Default: code edits denied
        pol_default = get_default_policy("telegram", allow_code_edits=False)
        self.assertFalse(pol_default.is_allowed(PermissionAction.WRITE_CODE))
        allowed, reason = evaluate_channel_action("telegram", PermissionAction.WRITE_CODE, pol_default)
        self.assertFalse(allowed)
        self.assertIn("restricted", reason.lower())

        # Explicitly enabled
        pol_enabled = get_default_policy("telegram", allow_code_edits=True)
        self.assertTrue(pol_enabled.is_allowed(PermissionAction.WRITE_CODE))
        allowed, reason = evaluate_channel_action("telegram", PermissionAction.WRITE_CODE, pol_enabled)
        self.assertTrue(allowed)

        # Even if allow_code_edits is True, shell execution is still blocked!
        self.assertFalse(pol_enabled.is_allowed(PermissionAction.SHELL_EXECUTION))
        allowed, _ = evaluate_channel_action("telegram", PermissionAction.SHELL_EXECUTION, pol_enabled)
        self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main()
