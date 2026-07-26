"""Regression tests for .env parsing (issue #431).

python-dotenv turns empty keys with inline comments into credential values:
  KEY=   # description  →  value = "# description"
That makes tools falsely report available. Our loader must treat those as empty.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from lib.env_loader import (
    get_env,
    load_dotenv_file,
    parse_dotenv_text,
    parse_dotenv_value,
    require_env,
)


class TestParseDotenvValue:
    def test_empty_with_inline_comment_is_empty(self):
        assert parse_dotenv_value("              # some description here") == ""

    def test_value_with_trailing_comment_strips_comment(self):
        assert parse_dotenv_value("realvalue             # some description here") == "realvalue"

    def test_glued_hash_without_space_keeps_value(self):
        # No whitespace before '#': not treated as a comment delimiter.
        assert parse_dotenv_value("realvalue# some description here") == "realvalue# some description here"

    def test_empty_bare_is_empty(self):
        assert parse_dotenv_value("") == ""

    def test_comment_only_hardening(self):
        assert parse_dotenv_value("# Pixabay stock footage") == ""

    def test_quoted_value_preserves_interior(self):
        assert parse_dotenv_value('"hello # world"') == "hello # world"
        assert parse_dotenv_value("'key=with=equals'") == "key=with=equals"


class TestParseDotenvText:
    def test_legacy_env_example_shape_yields_empty_keys(self):
        text = textwrap.dedent(
            """
            # header
            FAL_KEY=                     # FLUX images, Google Veo video
            PIXABAY_API_KEY=             # Pixabay stock footage/images (free)
            OPENAI_API_KEY=sk-real               # OpenAI TTS
            EMPTY_BARE=
            """
        )
        parsed = parse_dotenv_text(text)
        assert parsed["FAL_KEY"] == ""
        assert parsed["PIXABAY_API_KEY"] == ""
        assert parsed["OPENAI_API_KEY"] == "sk-real"
        assert parsed["EMPTY_BARE"] == ""

    def test_repo_env_example_has_no_comment_values(self):
        example = Path(__file__).resolve().parents[2] / ".env.example"
        text = example.read_text(encoding="utf-8")
        parsed = parse_dotenv_text(text)
        comment_values = {
            k: v for k, v in parsed.items() if v and v.lstrip().startswith("#")
        }
        assert comment_values == {}, f"comment-as-value keys: {comment_values}"
        # Fresh-copy keys must be empty (no accidental defaults).
        for key in ("FAL_KEY", "PIXABAY_API_KEY", "OPENAI_API_KEY", "VOLC_ACCESSKEY"):
            assert key in parsed
            assert parsed[key] == ""


class TestLoadDotenvFile:
    def test_loads_into_environ_without_comment_pollution(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "PIXABAY_API_KEY=             # Pixabay stock\n"
            "PEXELS_API_KEY=px-test\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
        monkeypatch.delenv("PEXELS_API_KEY", raising=False)

        load_dotenv_file(env_file)

        assert os.environ.get("PIXABAY_API_KEY") == ""
        assert os.environ.get("PEXELS_API_KEY") == "px-test"
        # Falsy empty string → tools report unavailable
        assert not os.environ.get("PIXABAY_API_KEY")

    def test_does_not_override_existing_env(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("OPENAI_API_KEY=from-file\n", encoding="utf-8")
        monkeypatch.setenv("OPENAI_API_KEY", "from-process")

        load_dotenv_file(env_file)
        assert os.environ["OPENAI_API_KEY"] == "from-process"


class TestGetEnv:
    def test_comment_value_treated_as_missing(self, monkeypatch):
        monkeypatch.setenv("BOGUS_KEY", "# leftover comment")
        assert get_env("BOGUS_KEY") is None
        assert get_env("BOGUS_KEY", default="x") == "x"

    def test_require_env_rejects_comment(self, monkeypatch):
        monkeypatch.setenv("BOGUS_KEY", "# leftover comment")
        with pytest.raises(EnvironmentError, match="BOGUS_KEY"):
            require_env("BOGUS_KEY")
