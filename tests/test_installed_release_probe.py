"""Protect the real release probe from accepting an incomplete built frontend."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.verify_installed_release import webui_assets


@pytest.fixture
def frontend(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ("entry.js", "lazy.js", "style.css"):
        (assets / name).write_text("compiled content", encoding="utf-8")
    manifest = {
        "index.html": {"file": "assets/entry.js", "css": ["assets/style.css"],
                       "dynamicImports": ["settings"]},
        "settings": {"file": "assets/lazy.js", "imports": ["index.html"]},
    }
    (tmp_path / "index.html").write_text('<script src="/assets/entry.js"></script>')
    (tmp_path / "asset-manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def test_manifest_covers_lazy_assets_not_only_initial_html(frontend):
    assert webui_assets(frontend) == {"assets/entry.js", "assets/lazy.js", "assets/style.css"}
    (frontend / "assets/lazy.js").unlink()
    with pytest.raises(ValueError, match="Missing or empty.*lazy.js"):
        webui_assets(frontend)


def test_missing_import_record_fails_even_if_entrypoint_exists(frontend):
    path = frontend / "asset-manifest.json"
    manifest = json.loads(path.read_text())
    del manifest["settings"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Missing WebUI manifest dependency"):
        webui_assets(frontend)


def test_stale_html_cannot_pass_against_an_unrelated_manifest(frontend):
    (frontend / "index.html").write_text('<script src="/assets/old.js"></script>')
    with pytest.raises(ValueError, match="HTML entrypoints"):
        webui_assets(frontend)


def test_probe_requires_isolated_python_before_importing_source(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/verify_installed_release.py"
    report = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(script), "--version", "0.3.6.dev1", "--commit", "a" * 40,
         "--report", str(report)], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert "python -I" in result.stderr
    assert not report.exists()
