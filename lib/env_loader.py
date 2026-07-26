"""Environment variable loader for OpenMontage.

Loads .env file and provides typed access to environment configuration.

Parsing rules (see issue #431):
- Full-line comments start with ``#``.
- Inline comments after a value require whitespace before ``#``
  (``KEY=value  # note`` → ``value``; ``KEY=   # note`` → empty).
- A value that is only a ``#`` comment (python-dotenv's empty-key quirk)
  is treated as unset / empty, never as a real credential.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional


def parse_dotenv_value(raw: str) -> str:
    """Normalize a single .env assignment value.

    Empty keys with trailing documentation comments become ``""`` so tools
    that gate on ``bool(os.environ.get(KEY))`` correctly report unavailable.
    """
    value = raw.strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end != -1 else value[1:]

    # Strip inline comment: '#' at start of value, or after whitespace.
    match = re.search(r"(^|\s)#", value)
    if match:
        value = value[: match.start()]
    value = value.strip()

    # Hardening: leftover comment-only / comment-prefixed garbage is unset.
    if value.lstrip().startswith("#"):
        return ""
    return value


def parse_dotenv_text(text: str) -> dict[str, str]:
    """Parse dotenv file contents into a key→value map (no os.environ writes)."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if not key:
            continue
        result[key] = parse_dotenv_value(raw)
    return result


def load_dotenv_file(env_path: Path, *, override: bool = False) -> None:
    """Load a .env file into ``os.environ``.

    By default does not override variables already present in the process
    environment (same contract as the previous base_tool loader).
    """
    if not env_path.is_file():
        return
    text = env_path.read_text(encoding="utf-8", errors="ignore")
    for key, value in parse_dotenv_text(text).items():
        if override or key not in os.environ:
            os.environ[key] = value


def load_env(project_root: Optional[Path] = None) -> None:
    """Load .env file from project root."""
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent
    load_dotenv_file(project_root / ".env")


def get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    """Get an environment variable with optional default.

    Comment-only values (legacy malformed .env) are treated as missing.
    """
    value = os.environ.get(key)
    if value is None:
        return default
    stripped = value.strip()
    if not stripped or stripped.startswith("#"):
        return default
    return value


def require_env(key: str) -> str:
    """Get a required environment variable. Raises if missing or comment-only."""
    value = get_env(key)
    if value is None:
        raise EnvironmentError(f"Required environment variable {key!r} is not set")
    return value
