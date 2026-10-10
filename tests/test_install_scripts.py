"""Exercise the public installers against real, offline package managers.

The tiny wheel reports its runtime identity; it is not the nanobot application.
No gateway, model, user configuration or public package index is used by these tests.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
WINDOWS = os.name == "nt"
BASE_PYTHON = sys._base_executable


def _wheel(directory, version):
    metadata = f"nanobot_ai-{version}.dist-info"
    files = {
        "nanobot/__init__.py": f'__version__ = "{version}"\n',
        "nanobot/__main__.py": """
import json
import sys
import nanobot

def main():
    print("INSTALLER_PROBE " + json.dumps({
        "version": nanobot.__version__, "package": nanobot.__file__,
        "python": sys.executable, "prefix": sys.prefix,
        "isolated": sys.flags.isolated, "args": sys.argv[1:],
    }))

if __name__ == "__main__":
    main()
""",
        f"{metadata}/METADATA": f"Metadata-Version: 2.1\nName: nanobot-ai\nVersion: {version}\nRequires-Python: >=3.11\n",
        f"{metadata}/WHEEL": "Wheel-Version: 1.0\nGenerator: installer-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{metadata}/entry_points.txt": "[console_scripts]\nnanobot = nanobot.__main__:main\n",
    }
    record = []
    path = directory / f"nanobot_ai-{version}-py3-none-any.whl"
    with ZipFile(path, "w") as wheel:
        for name, content in files.items():
            data = content.encode()
            wheel.writestr(name, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record.append(f"{name},sha256={digest},{len(data)}\n")
        wheel.writestr(f"{metadata}/RECORD", "".join(record) + f"{metadata}/RECORD,,\n")
    return path


def _python(environment):
    return environment / ("Scripts/python.exe" if WINDOWS else "bin/python")


def _run(command, sandbox, *, check=True):
    result = subprocess.run(command, cwd=sandbox["cwd"], env=sandbox["env"],
                            capture_output=True, text=True, encoding="utf-8", timeout=90)
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def _installer(sandbox, *args, check=True):
    if WINDOWS:
        shell = shutil.which("powershell")
        if not shell:
            pytest.skip("Windows PowerShell is required")
        command = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str(SCRIPTS / "install.ps1")]
    else:
        command = ["/bin/sh", str(SCRIPTS / "install.sh")]
    return _run([*command, *args], sandbox, check=check)


def _identity(result):
    reports = [json.loads(line.removeprefix("INSTALLER_PROBE "))
               for line in result.stdout.splitlines() if line.startswith("INSTALLER_PROBE ")]
    assert len(reports) == 1, result.stdout
    return reports[0]


def _expose(sandbox, name):
    # Explicit overrides let local verification use disposable manager installations.
    source = os.environ.get(f"NANOBOT_TEST_{name.upper()}") or shutil.which(name)
    if not source:
        pytest.skip(f"{name} is required for this real installer check")
    destination = sandbox["bin"] / (f"{name}.exe" if WINDOWS else name)
    if WINDOWS:
        shutil.copy2(source, destination)
    else:
        destination.symlink_to(source)
    return str(destination)


@pytest.fixture
def sandbox(tmp_path):
    root = tmp_path.resolve() / "user's 数据 $safe"
    root.mkdir()
    command_bin = root / "commands"
    command_bin.mkdir()
    wheels = root / "wheels"
    wheels.mkdir()
    # A checkout and PYTHONPATH must not replace the package just installed.
    checkout = root / "checkout"
    (checkout / "nanobot").mkdir(parents=True)
    (checkout / "nanobot/__init__.py").write_text('raise RuntimeError("wrong checkout imported")\n')
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}}
    system_path = (str(Path(env["SYSTEMROOT"]) / "System32") if WINDOWS else "/usr/bin:/bin")
    env.update({
        "HOME": str(root), "USERPROFILE": str(root),
        "PATH": str(command_bin) + os.pathsep + system_path,
        "PYTHON": BASE_PYTHON, "PYTHONPATH": str(checkout), "PYTHONUTF8": "1",
        "NANOBOT_SKIP_WIZARD": "1", "NANOBOT_VENV": str(root / "managed"),
        "NANOBOT_BIN_DIR": str(root / "launchers"),
        "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_DATA_HOME": str(root / "data"), "APPDATA": str(root / "config"),
        "LOCALAPPDATA": str(root / "data"), "TMPDIR": str(root), "TMP": str(root), "TEMP": str(root),
        "UV_TOOL_DIR": str(root / "uv-tools"), "UV_TOOL_BIN_DIR": str(root / "uv-bin"),
        "UV_CACHE_DIR": str(root / "uv-cache"), "UV_NO_CONFIG": "1", "UV_NO_INDEX": "1",
        "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never", "UV_FIND_LINKS": wheels.as_uri(),
        "PIPX_HOME": str(root / "pipx"), "PIPX_BIN_DIR": str(root / "pipx-bin"),
        "PIPX_MAN_DIR": str(root / "man"), "PIPX_DEFAULT_BACKEND": "pip",
        "PIPX_DISABLE_SHARED_LIBS_AUTO_UPGRADE": "1", "PIPX_DEFAULT_PYTHON": BASE_PYTHON,
        "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": wheels.as_uri(),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1",
    })
    return {"root": root, "bin": command_bin, "wheels": wheels, "cwd": checkout, "env": env}


@pytest.mark.parametrize("manager", ["uv", "pipx"])
def test_installer_starts_and_upgrades_same_persistent_manager_environment(sandbox, manager):
    _expose(sandbox, manager)
    _wheel(sandbox["wheels"], "0.0.1")
    prefix = (sandbox["root"] / "uv-tools/nanobot-ai" if manager == "uv"
              else sandbox["root"] / "pipx/venvs/nanobot-ai")
    for version in ("0.0.1", "0.0.2"):
        if version == "0.0.2":
            _wheel(sandbox["wheels"], version)
        identity = _identity(_installer(sandbox))
        assert identity["version"] == version
        assert Path(identity["prefix"]).resolve() == prefix
        assert Path(identity["package"]).resolve().is_relative_to(prefix)
        assert Path(identity["python"]).absolute() == _python(prefix)
        assert identity["isolated"] == 1
        assert identity["args"] == ["--version"]
    assert not (sandbox["root"] / "managed").exists()


def test_rerunning_installer_preserves_uv_version_constraint(sandbox):
    uv = _expose(sandbox, "uv")
    _wheel(sandbox["wheels"], "0.0.1")
    _wheel(sandbox["wheels"], "0.0.2")
    _run([uv, "tool", "install", "--python", BASE_PYTHON, "nanobot-ai==0.0.1"], sandbox)
    assert _identity(_installer(sandbox))["version"] == "0.0.1"
    receipt = sandbox["root"] / "uv-tools/nanobot-ai/uv-receipt.toml"
    assert "==0.0.1" in receipt.read_text()


@pytest.mark.parametrize("original", ["pipx", "managed"])
def test_existing_owner_wins_when_uv_is_installed_later(sandbox, original):
    if original == "pipx":
        _expose(sandbox, "pipx")
    _wheel(sandbox["wheels"], "0.0.1")
    before = _identity(_installer(sandbox))
    _expose(sandbox, "uv")
    _wheel(sandbox["wheels"], "0.0.2")
    after = _identity(_installer(sandbox))
    assert after["version"] == "0.0.2"
    assert after["prefix"] == before["prefix"]
    assert after["python"] == before["python"]
    assert not (sandbox["root"] / "uv-tools/nanobot-ai").exists()


def test_multiple_existing_installations_need_an_explicit_owner(sandbox):
    uv, pipx = _expose(sandbox, "uv"), _expose(sandbox, "pipx")
    _wheel(sandbox["wheels"], "0.0.1")
    _run([uv, "tool", "install", "--python", BASE_PYTHON, "nanobot-ai"], sandbox)
    _run([pipx, "install", "--python", BASE_PYTHON, "nanobot-ai"], sandbox)
    _wheel(sandbox["wheels"], "0.0.2")
    result = _installer(sandbox, check=False)
    assert result.returncode != 0
    assert "Multiple nanobot installations" in result.stdout + result.stderr
    for prefix in (sandbox["root"] / "uv-tools/nanobot-ai", sandbox["root"] / "pipx/venvs/nanobot-ai"):
        identity = _identity(_run([str(_python(prefix)), "-I", "-m", "nanobot", "--version"], sandbox))
        assert identity["version"] == "0.0.1"


def test_activated_manager_environment_is_not_updated_with_pip(sandbox):
    _expose(sandbox, "uv")
    _wheel(sandbox["wheels"], "0.0.1")
    before = _identity(_installer(sandbox))
    sandbox["env"]["PYTHON"] = before["python"]
    _wheel(sandbox["wheels"], "0.0.2")
    result = _installer(sandbox, check=False)
    assert result.returncode != 0
    assert "active Python belongs to uv or pipx" in result.stdout + result.stderr
    assert _identity(_run([before["python"], "-I", "-m", "nanobot", "--version"], sandbox))["version"] == "0.0.1"


@pytest.mark.parametrize("active", [True, False], ids=["active-venv", "managed-venv"])
def test_python_install_uses_same_environment_and_quoted_launcher(sandbox, active):
    _wheel(sandbox["wheels"], "0.0.1")
    prefix = sandbox["root"] / ("active" if active else "managed")
    if active:
        _run([BASE_PYTHON, "-I", "-m", "venv", str(prefix)], sandbox)
        sandbox["env"]["PYTHON"] = str(_python(prefix))
    identity = _identity(_installer(sandbox))
    assert Path(identity["prefix"]).resolve() == prefix
    assert identity["isolated"] == 1
    if not WINDOWS and not active:
        wrapper = sandbox["root"] / "launchers/nanobot"
        launched = _identity(_run([str(wrapper), "--version"], sandbox))
        assert launched == identity


@pytest.mark.parametrize("manager", ["uv", "pipx"])
def test_failed_install_does_not_fall_through_to_another_environment(sandbox, manager):
    _expose(sandbox, manager)
    # Offline and no candidate: this runs the manager's real failed-install path.
    result = _installer(sandbox, check=False)
    assert result.returncode != 0
    assert "no other installation was attempted" in result.stdout + result.stderr
    assert not (sandbox["root"] / "managed").exists()
    assert "INSTALLER_PROBE" not in result.stdout


def test_dry_run_does_not_create_tool_environment(sandbox):
    _expose(sandbox, "uv")
    result = _installer(sandbox, "--dry-run")
    assert "persistent uv tool environment" in result.stdout
    assert not (sandbox["root"] / "uv-tools").exists()
    assert "INSTALLER_PROBE" not in result.stdout
