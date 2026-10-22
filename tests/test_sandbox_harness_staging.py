"""Phase 1E T1 hotfix: host-side staging of the verification harness for the OCI backend.

On a Linux host the container's UID (65532) is not the staging user, so the bind-mounted harness directory
must be traversable and the script readable by "other" -- and nothing may become writable. The in-container
side (read, execute, cannot modify) is proven by
test_verifier_docker_integration.test_staged_harness_is_readable_but_not_modifiable_by_container_user.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import unittest
from pathlib import Path

from core.runtime.sandbox import HARNESS_SOURCE_PATH, stage_harness

POSIX = os.name == "posix"


class TestHarnessStaging(unittest.TestCase):
    def stage(self, **kw):
        harness_dir, sha = stage_harness(**kw)
        self.addCleanup(shutil.rmtree, harness_dir, True)
        return harness_dir, harness_dir / "verification_harness.py", sha

    def test_staged_content_is_the_trusted_harness(self) -> None:
        _, script, sha = self.stage(container_readable=True)
        data = HARNESS_SOURCE_PATH.read_bytes()
        self.assertEqual(script.read_bytes(), data)
        self.assertEqual(sha, hashlib.sha256(data).hexdigest())

    def test_container_staging_contains_only_the_harness(self) -> None:
        harness_dir, _, _ = self.stage(container_readable=True)
        self.assertEqual(sorted(p.name for p in harness_dir.iterdir()), ["verification_harness.py"])

    @unittest.skipUnless(POSIX, "POSIX permission bits")
    def test_container_staging_is_readable_by_other_and_writable_by_nobody_else(self) -> None:
        harness_dir, script, _ = self.stage(container_readable=True)
        self.assertEqual(stat.S_IMODE(harness_dir.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o644)
        for p in (harness_dir, script):
            mode = p.stat().st_mode
            self.assertFalse(mode & (stat.S_IWGRP | stat.S_IWOTH), p)
            self.assertEqual(p.stat().st_uid, os.getuid())

    @unittest.skipUnless(POSIX, "POSIX permission bits")
    def test_default_staging_stays_private(self) -> None:
        # The local backend writes a nonce file next to the script; its directory must stay owner-only.
        harness_dir, _, _ = self.stage()
        self.assertEqual(stat.S_IMODE(harness_dir.stat().st_mode), 0o700)

    def test_container_staging_is_removable_by_the_backend_cleanup(self) -> None:
        harness_dir, _ = stage_harness(container_readable=True)
        shutil.rmtree(harness_dir, ignore_errors=True)  # the backend's own cleanup call
        self.assertFalse(harness_dir.exists())


if __name__ == "__main__":
    unittest.main()
