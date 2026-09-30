#!/usr/bin/env python3
"""Pre-stale protection script.

Inspects all open PRs in the repository.
If a PR does NOT match test/ephemeral verification branch patterns,
it automatically adds the 'keep-open' label to protect it from actions/stale.
PRs with test branch patterns (test/*, chore/test-*, verify/*, etc.)
are left unlabeled so that actions/stale can mark and close them.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

TEST_BRANCH_PATTERNS = [
    r"^test(/.*|-.*)?$",          # test, test/*, test-*
    r"^chore/test-.*",            # chore/test-*
    r"^verify(/.*|-.*)?$",        # verify, verify/*, verify-*
    r"^verification(/.*|-.*)?$",  # verification, verification/*
    r"^testing(/.*|-.*)?$",       # testing, testing/*
]


def is_test_branch(branch_name: str) -> bool:
    """Return True if branch name matches known test/verification patterns."""
    branch = branch_name.strip()
    return any(re.match(p, branch, re.IGNORECASE) for p in TEST_BRANCH_PATTERNS)


def main() -> int:
    print("=== [stale-guard] Scanning open PRs for active work protection ===")
    
    # 1. Fetch all open PRs
    res = subprocess.run(
        ["gh", "pr", "list", "--state", "open", "--json", "number,headRefName,title,labels"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        print(f"Error fetching open PRs via gh CLI: {res.stderr}", file=sys.stderr)
        return 1

    try:
        prs = json.loads(res.stdout)
    except Exception as e:
        print(f"Failed to parse gh output as JSON: {e}", file=sys.stderr)
        return 1

    print(f"Found {len(prs)} open PR(s).")
    
    # 2. Evaluate each open PR
    for pr in prs:
        num = pr.get("number")
        branch = pr.get("headRefName", "")
        title = pr.get("title", "")
        labels = [l.get("name", "") if isinstance(l, dict) else str(l) for l in pr.get("labels", [])]

        if is_test_branch(branch):
            print(f"  -> PR #{num} (branch: '{branch}', title: '{title}') matches test/verification pattern.")
            print(f"     No 'keep-open' label added. Eligible for automated stale lifecycle.")
        else:
            print(f"  -> PR #{num} (branch: '{branch}', title: '{title}') is ACTIVE WORK.")
            if "keep-open" in labels:
                print(f"     PR #{num} already has 'keep-open' label.")
            else:
                print(f"     Adding 'keep-open' label to protect PR #{num} from automated stale...")
                add_res = subprocess.run(
                    ["gh", "pr", "edit", str(num), "--add-label", "keep-open"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if add_res.returncode == 0:
                    print(f"     [SUCCESS] 'keep-open' label added to PR #{num}.")
                else:
                    print(f"     [WARNING] Could not add label to PR #{num}: {add_res.stderr.strip()}", file=sys.stderr)

    print("=== [stale-guard] Protection scan complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
