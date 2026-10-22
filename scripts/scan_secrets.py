"""Pre-commit / CI secret scanner. Exits 1 and prints file:line (never the matched value) on any hit."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

PATTERNS: dict[str, re.Pattern[str]] = {
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "aws access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "bitget-style key": re.compile(r"\bbg_[0-9a-f]{24,}\b"),
    "secret-like assignment": re.compile(
        r"""(?ix)
        (?:api[_-]?key|secret|passphrase|password|token|consumer[_-]?(?:key|secret))
        ["']?\s*[:=]\s*["']?
        (?=[A-Za-z0-9+/_\-]*[0-9])(?=[A-Za-z0-9+/_\-]*[A-Za-z])
        [A-Za-z0-9+/_\-]{20,}
        """
    ),
}

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".woff", ".woff2", ".lock"}
ALLOW_MARKER = "scan-secrets: allow"


def scan_text(text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for name, pattern in PATTERNS.items():
            if pattern.search(line):
                hits.append((lineno, name))
    return hits


def scan_files(paths: Iterable[Path]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(f"{path}:{lineno}: possible secret ({name})" for lineno, name in scan_text(text))
    return findings


def main(argv: Sequence[str]) -> int:
    findings = scan_files(Path(a) for a in argv)
    for finding in findings:
        print(finding, file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
