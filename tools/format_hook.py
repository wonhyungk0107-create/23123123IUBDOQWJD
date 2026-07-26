"""PostToolUse hook: format the Python file that was just edited.

Reads the hook payload from stdin, pulls out the edited path, and runs ruff on it if
it is a Python file inside this repository.

Written in Python rather than as a shell one-liner for two reasons: it needs no
``jq`` (which is not present on a default Windows install), and it behaves
identically under bash and PowerShell. The hook is deliberately narrow -- it formats
one file and never runs the test suite, so editing stays fast.

Always exits 0. A formatter that blocks the session because it could not parse a
half-written file is worse than one that stays quiet.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _edited_path(payload: dict[str, object]) -> Path | None:
    tool_response = payload.get("tool_response")
    tool_input = payload.get("tool_input")
    for source in (tool_response, tool_input):
        if isinstance(source, dict):
            for key in ("filePath", "file_path"):
                value = source.get(key)
                if isinstance(value, str) and value:
                    return Path(value)
    return None


def main() -> int:
    # PowerShell prepends a UTF-8 BOM when piping to a native process, and
    # json.loads rejects a leading U+FEFF. Stripping it here is the difference
    # between a hook that works on Windows and one that silently does nothing.
    raw = sys.stdin.read().lstrip("﻿").strip()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0

    path = _edited_path(payload)
    if path is None or path.suffix != ".py" or not path.exists():
        return 0

    try:
        path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        # Edited a Python file outside this repository. Not ours to reformat.
        return 0

    for args in (["format", str(path)], ["check", "--fix", "--quiet", str(path)]):
        try:
            subprocess.run(
                ["uv", "run", "ruff", *args],
                cwd=REPO_ROOT,
                capture_output=True,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # uv or ruff unavailable. Editing must not stop because tooling is missing.
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
