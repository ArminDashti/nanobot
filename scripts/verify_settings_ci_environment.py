"""Reject dependency drift in the temporary frozen Settings diagnostic."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("expected", type=Path)
    parser.add_argument("actual", type=Path)
    args = parser.parse_args()
    expected = json.loads(args.expected.read_text(encoding="utf-8-sig"))
    packages = json.loads(args.actual.read_text(encoding="utf-8-sig"))
    actual = {
        re.sub(r"[-_.]+", "-", package["name"]).lower(): package["version"]
        for package in packages
    }
    differences = {
        name: {"expected": expected.get(name), "actual": actual.get(name)}
        for name in sorted(expected.keys() | actual.keys())
        if expected.get(name) != actual.get(name)
    }
    print(json.dumps({
        "matches_original": not differences,
        "expected_count": len(expected),
        "actual_count": len(actual),
        "differences": differences,
    }, indent=2))
    return int(bool(differences))


if __name__ == "__main__":
    raise SystemExit(main())
