"""
Pin the shape of the per-provider /v1/models adapters. We can't make
live network calls in CI, so the tests stub ``_stdlib_get_json`` with
captured response payloads from each provider's docs and assert the
helper produces a ``[{value, label}]`` list of the right shape.

Each provider's response format is slightly different — this test
catches a regression if the user's vendor changes their schema or if
a future refactor breaks normalization.
"""
from unittest.mock import patch

import pytest

# Import via AST extraction to avoid pulling in the full server.py
# import surface (it imports a lot of ML/IO modules that aren't
# available in the lightweight test venv).
from pathlib import Path
import ast
import sys


def _load_fetchers():
    src = Path(__file__).resolve().parents[1] / "server.py"
    code = src.read_text(encoding="utf-8")
    tree = ast.parse(code)
    wanted = {
        "_fetch_anthropic_models",
        "_fetch_openai_compat_models",
        "_fetch_gemini_models",
        "_fetch_ollama_local_models",
        "_stdlib_get_json",
    }
    bodies = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            bodies.append(node)
    mod = ast.Module(body=bodies, type_ignores=[])
    ns = {"Optional": __import__("typing").Optional}
    exec(compile(mod, str(src), "exec"), ns)
    return ns


_F = _load_fetchers()


def test_anthropic_models_returns_id_and_display_name():
    """Anthropic schema: { data: [{ id, display_name, type, created_at }] }.
    We filter to type=="model" and surface display_name as the label."""
    fake = {
        "data": [
            {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6", "type": "model"},
            {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5", "type": "model"},
            {"id": "deprecated-alias", "type": "alias"},  # filtered
        ],
    }
    with patch.dict(_F, {"_stdlib_get_json": lambda url, headers=None, timeout=8.0: fake}):
        out = _F["_fetch_anthropic_models"]("fake-key")
    ids = [m["value"] for m in out]
    assert "claude-sonnet-4-6" in ids
    assert "claude-haiku-4-5" in ids
    assert "deprecated-alias" not in ids
    # Label is display_name, not id
    snt = next(m for m in out if m["value"] == "claude-sonnet-4-6")
    assert snt["label"] == "Claude Sonnet 4.6"


def test_anthropic_returns_empty_without_key():
    """A missing API key isn't an error — it just means we have nothing
    to ask Anthropic with. UI falls back to ANTHROPIC_MODELS."""
    out = _F["_fetch_anthropic_models"]("")
    assert out == []


def test_openai_compat_models_handles_owned_by():
    """Standard OpenAI shape: { data: [{ id, object, owned_by }] }.
    Some providers return owned_by; we append it as "id · owner"."""
    fake = {
        "data": [
            {"id": "gpt-4", "object": "model", "owned_by": "openai"},
            {"id": "llama-3.1-70b", "object": "model"},  # no owner
        ],
    }
    with patch.dict(_F, {"_stdlib_get_json": lambda url, headers=None, timeout=8.0: fake}):
        out = _F["_fetch_openai_compat_models"]("https://api.example.com/v1", "fake-key")
    by_id = {m["value"]: m["label"] for m in out}
    assert by_id["gpt-4"] == "gpt-4 · openai"
    assert by_id["llama-3.1-70b"] == "llama-3.1-70b"  # no separator when owner empty


def test_gemini_models_strips_model_prefix_and_filters_to_chat():
    """Gemini's native API returns names like "models/gemini-2.5-flash"
    and a supportedGenerationMethods list. We strip the prefix and
    filter to entries that support generateContent (the chat surface)."""
    fake = {
        "models": [
            {
                "name": "models/gemini-2.5-flash",
                "displayName": "Gemini 2.5 Flash",
                "supportedGenerationMethods": ["generateContent", "countTokens"],
            },
            {
                "name": "models/embedding-001",  # filtered — not chat
                "displayName": "Embedding 001",
                "supportedGenerationMethods": ["embedContent"],
            },
            {
                "name": "models/gemini-2.5-pro",
                "displayName": "Gemini 2.5 Pro",
                "supportedGenerationMethods": ["generateContent"],
            },
        ],
    }
    with patch.dict(_F, {"_stdlib_get_json": lambda url, headers=None, timeout=8.0: fake}):
        out = _F["_fetch_gemini_models"]("fake-key")
    ids = [m["value"] for m in out]
    assert "gemini-2.5-flash" in ids
    assert "gemini-2.5-pro" in ids
    assert "embedding-001" not in ids
    labels = {m["value"]: m["label"] for m in out}
    assert labels["gemini-2.5-flash"] == "Gemini 2.5 Flash"


def test_ollama_local_models_shows_size_in_gb():
    """Ollama's /api/tags returns LOCALLY INSTALLED models. We label
    with the size in GB so the user knows what's eating disk."""
    fake = {
        "models": [
            {"name": "llama3.1:8b", "size": 4_700_000_000},  # ~4.4 GB
            {"name": "gemma2:27b",  "size": 16_000_000_000},  # ~14.9 GB
        ],
    }
    with patch.dict(_F, {"_stdlib_get_json": lambda url, headers=None, timeout=8.0: fake}):
        out = _F["_fetch_ollama_local_models"]("http://localhost:11434/v1")
    by_id = {m["value"]: m["label"] for m in out}
    assert "llama3.1:8b" in by_id
    assert "4.4 GB" in by_id["llama3.1:8b"]
    assert "14.9 GB" in by_id["gemma2:27b"]


def test_empty_base_url_returns_empty_list():
    """Custom provider without a base URL → no fetch attempted. UI
    falls back to ANTHROPIC_MODELS / hardcoded behavior."""
    out = _F["_fetch_openai_compat_models"]("", "any-key")
    assert out == []
    out2 = _F["_fetch_ollama_local_models"]("")
    assert out2 == []
