"""The local admin token is a 0600 file. The CLI sends it. A wrong or missing value does not match."""
import os
import stat
from pathlib import Path

from qos import local_token
from qos.install import api_call


def test_token_file_is_user_only_and_stable(tmp_path, monkeypatch):
    path = tmp_path / "data" / "local_token"
    monkeypatch.setenv("QOS_LOCAL_TOKEN_FILE", str(path))
    first = local_token.ensure_local_token()
    assert first == local_token.read_token() == local_token.ensure_local_token()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    os.chmod(path, 0o644)
    assert local_token.read_token() == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert local_token.matches(first)
    assert not local_token.matches(first[:-1])
    assert not local_token.matches(first + "x")
    assert not local_token.matches("")
    assert not local_token.matches(None)
    assert not local_token.matches("x" * 300)
    missing = tmp_path / "absent"
    monkeypatch.setenv("QOS_LOCAL_TOKEN_FILE", str(missing))
    assert local_token.read_token() is None and not local_token.matches(first)
    assert local_token.client_headers() == {}


def test_cli_sends_the_token_header(tmp_path, monkeypatch):
    monkeypatch.setenv("QOS_LOCAL_TOKEN_FILE", str(tmp_path / "local_token"))
    tok = local_token.ensure_local_token()
    seen = {}

    class _Resp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=0):
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    assert api_call("GET", "/health", api="http://127.0.0.1:9") == {}
    assert seen["headers"]["x-qos-local-token"] == tok
    assert seen["headers"]["x-source"] == "qos-cli"


def test_default_path_follows_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("QOS_LOCAL_TOKEN_FILE", raising=False)
    monkeypatch.setattr(local_token.paths, "data_dir", lambda: tmp_path)
    assert local_token.token_path() == tmp_path / "local_token"
    monkeypatch.setenv("QOS_LOCAL_TOKEN_FILE", str(tmp_path / "explicit"))
    assert local_token.token_path() == tmp_path / "explicit"


def test_reset_script_sends_the_token(tmp_path, monkeypatch):
    import importlib.util
    monkeypatch.setenv("QOS_LOCAL_TOKEN_FILE", str(tmp_path / "local_token"))
    tok = local_token.ensure_local_token()
    spec = importlib.util.spec_from_file_location("reset_node_books", Path(__file__).resolve().parents[1] / "scripts" / "reset_node_books.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = {}

    class _Resp:
        def read(self):
            return b'{"account_id": "DU1"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=0):
        seen["header"] = req.get_header("X-qos-local-token")
        return _Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_open)
    assert mod.api_get("http://127.0.0.1:9", "/account")["account_id"] == "DU1"
    assert seen["header"] == tok
