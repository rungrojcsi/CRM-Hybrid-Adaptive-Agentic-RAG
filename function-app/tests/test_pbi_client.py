"""Tests for pbi_client — JWT exp parsing + token cache + HTTP error handling."""
import base64
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from transform import pbi_client


def _make_jwt(exp_offset_seconds: int) -> str:
    """Build an unsigned JWT with given exp offset from now (signature ignored)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    payload_data = {"exp": int(time.time()) + exp_offset_seconds}
    payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


class TestJwtExp:
    def test_extracts_valid_exp(self):
        tok = _make_jwt(3600)
        exp = pbi_client._jwt_exp(tok)
        assert abs(exp - (time.time() + 3600)) < 5

    def test_returns_zero_for_garbage(self):
        assert pbi_client._jwt_exp("not.a.jwt") == 0.0

    def test_returns_zero_for_empty(self):
        assert pbi_client._jwt_exp("") == 0.0


class TestTokenCache:
    def setup_method(self):
        pbi_client._TOKEN_CACHE["token"] = None
        pbi_client._TOKEN_CACHE["exp"] = 0.0

    def test_uses_cached_token_when_fresh(self):
        pbi_client._TOKEN_CACHE["token"] = "cached-token"
        pbi_client._TOKEN_CACHE["exp"] = time.time() + 3600
        assert pbi_client._get_access_token() == "cached-token"

    def test_skips_expired_cache(self):
        pbi_client._TOKEN_CACHE["token"] = "stale-token"
        pbi_client._TOKEN_CACHE["exp"] = time.time() - 10
        # Cache miss → falls through; with no refresh config raises RuntimeError
        with patch.object(pbi_client, "PBI_ACCESS_TOKEN", ""), \
             patch.object(pbi_client, "PBI_REFRESH_TOKEN", ""), \
             patch.object(pbi_client, "PBI_CLIENT_ID", ""):
            try:
                pbi_client._get_access_token()
                raised = False
            except RuntimeError:
                raised = True
            assert raised

    def test_env_access_token_with_valid_jwt(self):
        fresh = _make_jwt(3600)
        with patch.object(pbi_client, "PBI_ACCESS_TOKEN", fresh):
            assert pbi_client._get_access_token() == fresh
            assert pbi_client._TOKEN_CACHE["token"] == fresh
