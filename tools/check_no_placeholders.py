"""Acceptance gate G13: no concealed incompleteness in production code.

Searches ``src/`` for the markers that hide unfinished work behind something that
looks finished: ``TODO``, ``FIXME``, ``XXX``, ``NotImplementedError``, bare ``pass``
in a function body, and commented-out implementations.

A genuinely unsupported operation is not a placeholder. It returns a typed
:class:`~tradeup.domain.execution.CapabilityResult` refusal with a specific reason,
which is a complete implementation of "we cannot do this" — and that is exactly the
distinction this check exists to enforce.

Usage::

    uv run python tools/check_no_placeholders.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

SOURCE_ROOT = Path("src")

MARKER_PATTERN = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")

#: Lines that look like commented-out code rather than prose.
COMMENTED_CODE_PATTERN = re.compile(
    r"^\s*#\s*(return\s|if\s+.*:|for\s+.*:|def\s+\w+\s*\(|class\s+\w+|await\s|import\s+\w)"
)


def _find_marker_lines(path: Path) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if MARKER_PATTERN.search(line):
            findings.append((number, f"placeholder marker: {line.strip()}"))
        if COMMENTED_CODE_PATTERN.match(line):
            findings.append((number, f"commented-out code: {line.strip()}"))
    return findings


def _find_stub_bodies(path: Path) -> list[tuple[int, str]]:
    """Functions whose entire body is ``pass``, ``...`` or ``raise NotImplementedError``.

    Protocol methods legitimately use ``...``; they are excluded by checking whether
    the enclosing class derives from ``Protocol``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    protocol_functions: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            is_protocol = any(
                (isinstance(base, ast.Name) and base.id == "Protocol")
                or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
                for base in node.bases
            )
            if is_protocol:
                for child in ast.walk(node):
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                        protocol_functions.add(child.lineno)

    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.lineno in protocol_functions:
            continue
        body = [
            statement
            for statement in node.body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            )
        ]
        if len(body) != 1:
            continue
        only = body[0]
        if isinstance(only, ast.Pass):
            findings.append((node.lineno, f"stub body (pass): {node.name}"))
        elif isinstance(only, ast.Expr) and isinstance(only.value, ast.Constant):
            if only.value.value is Ellipsis:
                findings.append((node.lineno, f"stub body (...): {node.name}"))
        elif isinstance(only, ast.Raise):
            exception = only.exc
            name = None
            if isinstance(exception, ast.Call) and isinstance(exception.func, ast.Name):
                name = exception.func.id
            elif isinstance(exception, ast.Name):
                name = exception.id
            if name == "NotImplementedError":
                findings.append((node.lineno, f"NotImplementedError: {node.name}"))
    return findings


def main() -> int:
    if not SOURCE_ROOT.exists():
        print(f"{SOURCE_ROOT} not found; run from the repository root", file=sys.stderr)
        return 2

    problems: list[str] = []
    scanned = 0
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        scanned += 1
        for line_number, message in _find_marker_lines(path) + _find_stub_bodies(path):
            problems.append(f"{path.as_posix()}:{line_number}: {message}")

    print(f"scanned {scanned} source files under {SOURCE_ROOT}/")
    if problems:
        print(f"\n{len(problems)} placeholder(s) found:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("no placeholders, stubs or commented-out implementations found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
