#!/usr/bin/env python3
"""Read-only candidate scan for obvious placeholder UI; human review required."""

import argparse
from pathlib import Path
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


CHECKS = (
    ("placeholder-link", re.compile(r'href\s*=\s*["\x27](?:#|javascript:void\(0\)|)["\x27]', re.I)),
    ("placeholder-copy", re.compile(r'\b(?:lorem ipsum|coming soon|insert (?:text|copy) here)\b', re.I)),
    ("potential-unverified-claim", re.compile(r'\b(?:\d+(?:\.\d+)?%|\d+[kKmM]\+?)\s+(?:of\s+)?(?:users|customers|teams|faster|growth|satisfaction)\b', re.I)),
    ("prohibited-emoji-or-emoticon", re.compile(r'[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|(?::\)|:-\)|;\)|:D|<3|\b(?:XD|XP)\b)')),
)


def scan(path: Path) -> int:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        print(f"{path}: cannot read: {exc}")
        return 1
    for number, line in enumerate(lines, 1):
        for label, pattern in CHECKS:
            if pattern.search(line):
                print(f"{path}:{number}: {label}: {line.strip()[:140]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    return int(any(scan(path) for path in args.paths))


if __name__ == "__main__":
    raise SystemExit(main())
