"""Tests for asker.ask — same unguarded-content / uncaught-urlopen crash class as
synthesizer (would make /api/ask, /api/ask-pg, /api/ask-combined return HTTP 500 →
Foundry agent "Sorry, something went wrong"). Must degrade gracefully.
"""
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from transform import asker


@pytest.fixture(autouse=True)
def _fake_endpoint(monkeypatch):
    monkeypatch.setattr(asker, "AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setattr(asker, "AZURE_OPENAI_API_KEY", "k")


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_CHUNKS = [{"account_id": "A1", "chunk_seq": 0, "content": "x", "similarity": 0.9}]


def test_content_none_does_not_crash(monkeypatch):
    monkeypatch.setattr(
        asker.urllib.request, "urlopen",
        lambda req: _FakeResp({"choices": [{"message": {"content": None}}]}),
    )
    out = asker.ask("q", _CHUNKS)
    assert isinstance(out["answer"], str) and out["answer"].strip()
    assert out["sources"][0]["account_id"] == "A1"


def test_httperror_does_not_crash(monkeypatch):
    def _boom(req):
        raise urllib.error.HTTPError(url="u", code=429, msg="throttled", hdrs=None, fp=None)

    monkeypatch.setattr(asker.urllib.request, "urlopen", _boom)
    out = asker.ask("q", _CHUNKS)
    assert isinstance(out["answer"], str) and out["answer"].strip()


def test_normal_content_returns_answer(monkeypatch):
    monkeypatch.setattr(
        asker.urllib.request, "urlopen",
        lambda req: _FakeResp({"choices": [{"message": {"content": "คำตอบจริง"}}],
                               "usage": {"total_tokens": 10}}),
    )
    out = asker.ask("q", _CHUNKS)
    assert out["answer"] == "คำตอบจริง"
