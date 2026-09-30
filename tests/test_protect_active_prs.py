"""Unit tests for the pre-stale protection script."""
import unittest
from pathlib import Path
import sys

# Add .github/scripts to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".github" / "scripts"))
from protect_active_prs import is_test_branch  # type: ignore[import-not-found]



class TestProtectActivePrs(unittest.TestCase):
    def test_test_branches_identified(self):
        test_branches = [
            "test/verify-stale-bot",
            "test/pr-proof-frontend-change",
            "test/pr-proof-backend-only",
            "test/pr-proof-orphan-live",
            "chore/test-protection-verification",
            "chore/test-something",
            "test-pr-proof",
            "test",
            "verify/proof",
            "verification/quality-gate",
            "testing/flow",
        ]
        for b in test_branches:
            with self.subTest(branch=b):
                self.assertTrue(is_test_branch(b), f"Branch {b} should be recognized as test branch")

    def test_active_branches_not_identified_as_test(self):
        active_branches = [
            "feat/dummy-active-work",
            "feat/quality-gate-pr-proof",
            "feat/pr-proof-assets-orphan-storage",
            "fix/login-crash",
            "refactor/mcp-client",
            "chore/cleanup-dependencies",
            "docs/update-architecture",
            "ci/add-stale-workflow",
            "main",
        ]
        for b in active_branches:
            with self.subTest(branch=b):
                self.assertFalse(is_test_branch(b), f"Branch {b} should NOT be recognized as test branch")


if __name__ == "__main__":
    unittest.main()
