"""Unit tests for auth_manager.py profile vault and Google account switching."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auth_manager


class TestAuthManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="gemini_test_")
        self.gemini_dir = Path(self.temp_dir).resolve()
        self.vault_dir = self.gemini_dir / "profiles"

        # Patch GEMINI_HOME to temporary directory
        self.env_patch = patch.dict(os.environ, {"GEMINI_HOME": str(self.gemini_dir)})
        self.env_patch.start()
        self.keyring_patch = patch.object(auth_manager, "read_windows_keyring_account", return_value=None)
        self.keyring_patch.start()
        self.keyring_write_patch = patch.object(auth_manager, "write_windows_keyring_account", return_value=True)
        self.keyring_write_patch.start()

    def tearDown(self):
        self.keyring_write_patch.stop()
        self.keyring_patch.stop()
        self.env_patch.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_jwt_payload_decoding(self):
        # Header: {"alg":"none"} -> eyJhbGciOiJub25lIn0
        # Payload: {"email":"test@example.com","sub":"12345"}
        payload_b64 = "eyJlbWFpbCI6InRlc3RAZXhhbXBsZS5jb20iLCJzdWIiOiIxMjM0NSJ9"
        dummy_jwt = f"eyJhbGciOiJub25lIn0.{payload_b64}."
        decoded = auth_manager.decode_jwt_payload(dummy_jwt)
        self.assertEqual(decoded.get("email"), "test@example.com")
        self.assertEqual(decoded.get("sub"), "12345")

    def test_list_accounts_and_vault_save(self):
        # Create dummy active creds for user1@example.com
        payload_b64 = "eyJlbWFpbCI6InVzZXIxQGV4YW1wbGUuY29tIn0"
        dummy_jwt = f"header.{payload_b64}."
        creds = {"access_token": "token1", "id_token": dummy_jwt}
        (self.gemini_dir / "oauth_creds.json").write_text(json.dumps(creds), encoding="utf-8")
        (self.gemini_dir / "google_accounts.json").write_text(
            json.dumps({"active": "user1@example.com", "old": ["user2@example.com"]}),
            encoding="utf-8"
        )

        accts = auth_manager.list_accounts()
        self.assertEqual(len(accts), 2)
        active_acct = next(a for a in accts if a["is_active"])
        self.assertEqual(active_acct["email"], "user1@example.com")
        self.assertTrue(active_acct["has_creds"])

        # Check vault file was created
        vault_file = self.vault_dir / "user1@example.com" / "oauth_creds.json"
        self.assertTrue(vault_file.exists())

    def test_switch_account(self):
        # Setup user1 active in main dir and vault
        payload_1 = "eyJlbWFpbCI6InVzZXIxQGV4YW1wbGUuY29tIn0"
        creds_1 = {"access_token": "token1", "id_token": f"h.{payload_1}."}
        (self.gemini_dir / "oauth_creds.json").write_text(json.dumps(creds_1), encoding="utf-8")
        (self.gemini_dir / "google_accounts.json").write_text(
            json.dumps({"active": "user1@example.com", "old": []}),
            encoding="utf-8"
        )
        auth_manager.save_current_account_to_vault()

        # Setup user2 in vault
        payload_2 = "eyJlbWFpbCI6InVzZXIyQGV4YW1wbGUuY29tIn0"
        creds_2 = {"access_token": "token2", "id_token": f"h.{payload_2}."}
        user2_dir = self.vault_dir / "user2@example.com"
        user2_dir.mkdir(parents=True, exist_ok=True)
        (user2_dir / "oauth_creds.json").write_text(json.dumps(creds_2), encoding="utf-8")

        # Switch to user2
        success, msg = auth_manager.switch_account("user2@example.com")
        self.assertTrue(success)
        self.assertIn("user2@example.com", msg)

        # Verify active account is now user2
        self.assertEqual(auth_manager.get_active_account(), "user2@example.com")

        # Verify oauth_creds.json in main dir has token2
        main_creds = json.loads((self.gemini_dir / "oauth_creds.json").read_text(encoding="utf-8"))
        self.assertEqual(main_creds["access_token"], "token2")

        # Verify google_accounts.json updated
        ga = json.loads((self.gemini_dir / "google_accounts.json").read_text(encoding="utf-8"))
        self.assertEqual(ga["active"], "user2@example.com")
        self.assertIn("user1@example.com", ga["old"])

        # Switch back to user1
        success, msg = auth_manager.switch_account("user1@example.com")
        self.assertTrue(success)
        self.assertEqual(auth_manager.get_active_account(), "user1@example.com")

    def test_remove_account(self):
        # Setup user2 in vault
        user2_dir = self.vault_dir / "user2@example.com"
        user2_dir.mkdir(parents=True, exist_ok=True)
        (user2_dir / "oauth_creds.json").write_text("{}", encoding="utf-8")

        # Cannot remove active account
        with patch.object(auth_manager, "get_active_account", return_value="user2@example.com"):
            ok, msg = auth_manager.remove_account("user2@example.com")
            self.assertFalse(ok)
            self.assertTrue(user2_dir.exists())

        # Can remove non-active account
        with patch.object(auth_manager, "get_active_account", return_value="user1@example.com"):
            ok, msg = auth_manager.remove_account("user2@example.com")
            self.assertTrue(ok)
            self.assertFalse(user2_dir.exists())

    def test_windows_keyring_sync(self):
        fake_creds = {
            "access_token": "keyring_token",
            "refresh_token": "keyring_refresh",
            "scope": "openid",
            "token_type": "Bearer",
            "id_token": "fake_id_token",
            "expiry_date": 123456789,
        }
        with patch.object(auth_manager, "read_windows_keyring_account", return_value=("keyring_user@example.com", fake_creds)):
            email = auth_manager.sync_windows_keyring_to_vault()
            self.assertEqual(email, "keyring_user@example.com")
            vault_file = self.vault_dir / "keyring_user@example.com" / "oauth_creds.json"
            self.assertTrue(vault_file.exists())
            saved = json.loads(vault_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["access_token"], "keyring_token")


if __name__ == "__main__":
    unittest.main()
