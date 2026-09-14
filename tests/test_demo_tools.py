"""Exercise demo entrypoints offline, with isolated fake clients and credentials."""
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["smoke_baidu.py", "smoke_baidu_api.py"])
def test_direct_script_help_from_other_directory(tmp_path, name):
    result = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / name), "--help"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


@pytest.mark.parametrize("name", ["smoke_baidu.py", "smoke_baidu_api.py"])
@pytest.mark.parametrize("has_hits", [False, True])
def test_probe_reports_empty_results_and_closes_client(monkeypatch, name, has_hits):
    closed = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): closed.append(True)
    class Provider:
        def __init__(self, *args, **kwargs): pass
        def search(self, query, limit):
            return [SimpleNamespace(title="Test", url="https://example.test", snippet="test", published_at=None)] if has_hits else []
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=Client))
    monkeypatch.setitem(sys.modules, "longflow.search_provider", SimpleNamespace(BaiduSearchProvider=Provider, BaiduApiSearchProvider=Provider))
    monkeypatch.setenv("LONGFLOW_SEARCH_BAIDU_KEY", "fake-test-key")
    monkeypatch.setenv("LONGFLOW_SEARCH_ENDPOINT", "https://example.test/search")
    monkeypatch.setattr(sys, "argv", [name, "test"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    if has_hits:
        runpy.run_path(str(ROOT / "tools" / name), run_name="__main__")
    else:
        with pytest.raises(SystemExit, match="probe not verified"):
            runpy.run_path(str(ROOT / "tools" / name), run_name="__main__")
    assert closed == [True]


def test_api_probe_requires_explicit_endpoint(monkeypatch):
    monkeypatch.setenv("LONGFLOW_SEARCH_BAIDU_KEY", "fake-test-key")
    monkeypatch.delenv("LONGFLOW_SEARCH_ENDPOINT", raising=False)
    monkeypatch.delenv("LONGFLOW_SEARCH_BAIDU_ENDPOINT", raising=False)
    monkeypatch.setattr(sys, "argv", ["smoke_baidu_api.py"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(SystemExit, match="real LONGFLOW_SEARCH_ENDPOINT"):
        runpy.run_path(str(ROOT / "tools/smoke_baidu_api.py"), run_name="__main__")


def test_bootstrap_portable_secure_entrypoint():
    script = (ROOT / "tools/api_keys_bootstrap.ps1").read_text(encoding="utf-8")
    assert "C:\\Users\\" not in script
    assert "Get-Command python" in script
    assert "-AsSecureString" in script
    assert "ZeroFreeBSTR" in script
    assert "-m longflow.cli serve" in script
    assert "longflow.api:app" not in script
    assert "ark-code-latest" not in script
