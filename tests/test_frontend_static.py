"""Offline browser module linking checks (Node required in release CI)."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is required for frontend static checks")


def check(root):
    return subprocess.run([NODE, str(ROOT / "tools/check_web.mjs"), str(root)],
                          capture_output=True, text=True, encoding="utf-8", timeout=30)


def test_real_frontend_links():
    result = check(ROOT / "longflow/web")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not executed" in result.stdout


@pytest.mark.parametrize("app,html,dependency,expected", [
    ("const a = ;", '<script type="module" src="app.js"></script>', None, "SyntaxError"),
    ("", '<script src="app.js"></script>', None, "type=module"),
    ("", '<script type="module" src="app.js"></script>' * 2, None, "Duplicate"),
    ('import "./missing.js";', '<script type="module" src="app.js"></script>', None, "ENOENT"),
    ('import { missing } from "./dep.js";', '<script type="module" src="app.js"></script>', "export const exists = 1;", "missing"),
    ('throw new Error("must not execute");', '<script type="module" src="app.js"></script>', None, None),
])
def test_checker_detects_loading_failures_without_executing(tmp_path, app, html, dependency, expected):
    (tmp_path / "app.js").write_text(app, encoding="utf-8")
    (tmp_path / "index.html").write_text(html, encoding="utf-8")
    if dependency is not None:
        (tmp_path / "dep.js").write_text(dependency, encoding="utf-8")
    result = check(tmp_path)
    if expected:
        assert result.returncode != 0
        assert expected in result.stderr
    else:
        assert result.returncode == 0, result.stderr
