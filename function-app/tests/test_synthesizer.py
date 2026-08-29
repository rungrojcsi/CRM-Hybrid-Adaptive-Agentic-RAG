"""Tests for synthesizer — the unhandled-exception path that made /api/ask-quant
return HTTP 500 (App Insights: http_ask_quant 1/157 failed, code 500), which in
turn made the Foundry agent run fail with "Sorry, something went wrong".

Two crash modes, both must degrade gracefully (return a usable string, never raise):
  1. Azure OpenAI returns message.content = None (finish_reason=length / filter)
  2. urlopen raises (HTTPError 400/429, timeout, transport error)
"""
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from transform import synthesizer

import pytest


@pytest.fixture(autouse=True)
def _fake_endpoint(monkeypatch):
    # synthesize() builds the request URL from these module globals; without a
    # valid https endpoint urllib.request.Request() raises before we reach urlopen.
    monkeypatch.setattr(synthesizer, "AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setattr(synthesizer, "AZURE_OPENAI_API_KEY", "k")


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_content_none_does_not_crash(monkeypatch):
    monkeypatch.setattr(
        synthesizer.urllib.request, "urlopen",
        lambda req, timeout=60: _FakeResp({"choices": [{"message": {"content": None}}]}),
    )
    out = synthesizer.synthesize("ควรโฟกัสอะไรให้เข้าเป้า", None, [{"Actual": 100}])
    assert isinstance(out, str) and out.strip()


def test_httperror_does_not_crash(monkeypatch):
    def _boom(req, timeout=60):
        raise urllib.error.HTTPError(url="u", code=400, msg="bad", hdrs=None, fp=None)

    monkeypatch.setattr(synthesizer.urllib.request, "urlopen", _boom)
    out = synthesizer.synthesize("ควรโฟกัสอะไรให้เข้าเป้า", None, None)
    assert isinstance(out, str) and out.strip()


def test_normal_content_returns_answer(monkeypatch):
    monkeypatch.setattr(
        synthesizer.urllib.request, "urlopen",
        lambda req, timeout=60: _FakeResp({"choices": [{"message": {"content": "คำตอบจริง"}}]}),
    )
    out = synthesizer.synthesize("q", None, None)
    assert "คำตอบจริง" in out
