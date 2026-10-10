"""Exercise the public installers against real, offline package managers.

The tiny wheel reports its runtime identity; it is not the nanobot application.
No gateway, model, user configuration or public package index is used by these tests.
"""

import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from zipfile import ZipFile

import pytest
from packaging.tags import sys_tags

from nanobot import releases
from scripts.release_install_guide import install_command, verification_command

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
WINDOWS = os.name == "nt"
BASE_PYTHON = sys._base_executable


def _wheel(directory, version, *, platform="any", package="nanobot-ai", extras=()):
    module = package.replace("-", "_") if package != "nanobot-ai" else "nanobot"
    wheel_name = package.replace("-", "_")
    metadata = f"{wheel_name}-{version}.dist-info"
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
    files = {name.replace("nanobot/", f"{module}/"): content for name, content in files.items()}
    files[f"{module}/__main__.py"] = files[f"{module}/__main__.py"].replace("nanobot", module)
    files[f"{metadata}/METADATA"] = files[f"{metadata}/METADATA"].replace("Name: nanobot-ai", f"Name: {package}")
    files[f"{metadata}/METADATA"] += "".join(f"Provides-Extra: {extra}\n" for extra in extras)
    files[f"{metadata}/WHEEL"] = files[f"{metadata}/WHEEL"].replace("py3-none-any", f"py3-none-{platform}")
    files[f"{metadata}/entry_points.txt"] = f"[console_scripts]\n{module} = {module}.__main__:main\n"
    record = []
    path = directory / f"{wheel_name}-{version}-py3-none-{platform}.whl"
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


def _preview_manifest(sandbox, *, damaged_hash=False, extras=()):
    version = "0.0.2.dev1"
    platform = next(tag.platform for tag in sys_tags()
                    if tag.platform in releases.PLATFORMS.values())
    wheel = _wheel(sandbox["cwd"], version, platform=platform, extras=extras)
    digest = "0" * 64 if damaged_hash else hashlib.sha256(wheel.read_bytes()).hexdigest()
    artifacts = [releases.ReleaseArtifact(
        filename=name, size=wheel.stat().st_size if name == wheel.name else 123,
        sha256=digest if name == wheel.name else "c" * 64,
    ) for name in sorted(releases.release_filenames(version))]
    manifest = releases.ReleaseManifest(version=version, source_commit="a" * 40,
                                         candidate_commit="b" * 40, requires_python=">=3.11",
                                         artifacts=artifacts)
    return manifest, next(item for item in artifacts if item.filename == wheel.name)


def _owner_environment(sandbox, owner, *, existing):
    _wheel(sandbox["wheels"], "0.0.1")
    if owner == "pip":
        prefix = sandbox["root"] / "ordinary-venv"
        _run([BASE_PYTHON, "-I", "-m", "venv", str(prefix)], sandbox)
        executable = str(_python(prefix))
        if existing:
            _run([executable, "-I", "-m", "pip", "install", "nanobot-ai"], sandbox)
    else:
        executable = _expose(sandbox, owner)
        prefix = sandbox["root"] / ("uv-tools/nanobot-ai" if owner == "uv" else "pipx/venvs/nanobot-ai")
        sandbox["env"]["UV_PYTHON"] = BASE_PYTHON
        if existing:
            _installer(sandbox)
    return executable, prefix


def _verify_owner(sandbox, owner, prefix):
    if owner == "pip":
        return _identity(_run([str(_python(prefix)), "-I", "-m", "nanobot", "--version"], sandbox))
    command = verification_command(owner, windows=WINDOWS)
    shell = ["powershell", "-NoProfile", "-NonInteractive", "-Command"] if WINDOWS else ["/bin/sh", "-c"]
    return _identity(_run([*shell, command], sandbox))


def _run_guide_command(sandbox, command, python, *, check=True):
    """Run the same multiline command users copy, including shell quoting."""
    if WINDOWS:
        value = str(python).replace("'", "''")
        prelude = f"Set-Alias -Name python -Value '{value}'\n"
        shell = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    else:
        prelude = f'python() {{ {shlex.quote(str(python))} "$@"; }}\n'
        shell = ["/bin/sh", "-c"]
    return _run([*shell, prelude + shlex.join(command)], sandbox, check=check)


@pytest.mark.parametrize("owner", ["uv", "pipx", "pip"])
@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing"])
def test_generated_exact_preview_commands_use_persistent_owner(sandbox, owner, existing):
    _, prefix = _owner_environment(sandbox, owner, existing=existing)
    manifest, artifact = _preview_manifest(sandbox)
    command = install_command(manifest, artifact, owner=owner, existing=existing)
    _run_guide_command(sandbox, command, _python(prefix) if owner == "pip" else BASE_PYTHON)
    identity = _verify_owner(sandbox, owner, prefix)
    assert identity["version"] == manifest.version
    assert Path(identity["prefix"]).resolve() == prefix
    assert Path(identity["package"]).resolve().is_relative_to(prefix)
    assert identity["isolated"] == 1


@pytest.mark.parametrize("owner", ["uv", "pipx", "pip"])
def test_generated_command_hash_failure_leaves_existing_version(sandbox, owner):
    _, prefix = _owner_environment(sandbox, owner, existing=True)
    manifest, artifact = _preview_manifest(sandbox, damaged_hash=True)
    command = install_command(manifest, artifact, owner=owner, existing=True)
    result = _run_guide_command(sandbox, command, _python(prefix) if owner == "pip" else BASE_PYTHON, check=False)
    assert result.returncode != 0
    assert "no package manager was started" in result.stderr
    assert _verify_owner(sandbox, owner, prefix)["version"] == "0.0.1"


@pytest.mark.parametrize("owner", ["uv", "pipx"])
def test_exact_preview_does_not_silently_remove_manager_pin(sandbox, owner):
    executable, prefix = _owner_environment(sandbox, owner, existing=False)
    if owner == "uv":
        _run([executable, "tool", "install", "nanobot-ai==0.0.1"], sandbox)
    else:
        _run([executable, "install", "--skip-maintenance", "nanobot-ai"], sandbox)
        _run([executable, "pin", "nanobot-ai"], sandbox)
    manifest, artifact = _preview_manifest(sandbox)
    command = install_command(manifest, artifact, owner=owner, existing=True)
    result = _run_guide_command(sandbox, command, BASE_PYTHON, check=False)
    # uv rejects the constraint conflict; pipx successfully skips a pinned tool.
    assert (result.returncode == 0) == (owner == "pipx")
    assert _verify_owner(sandbox, owner, prefix)["version"] == "0.0.1"


def test_exact_pipx_target_preserves_injected_package_and_records_new_spec(sandbox):
    pipx, prefix = _owner_environment(sandbox, "pipx", existing=True)
    _wheel(sandbox["wheels"], "1.0.0", package="fixture-helper")
    _run([pipx, "inject", "--skip-maintenance", "nanobot-ai", "fixture-helper"], sandbox)
    receipt = prefix / "pipx_metadata.json"
    before = json.loads(receipt.read_text())
    manifest, artifact = _preview_manifest(sandbox)
    command = install_command(manifest, artifact, owner="pipx", existing=True)
    _run_guide_command(sandbox, command, BASE_PYTHON)
    after = json.loads(receipt.read_text())
    assert after["injected_packages"] == before["injected_packages"]
    assert after["source_interpreter"] == before["source_interpreter"]
    assert after["main_package"]["package_or_url"] == f"nanobot-ai @ {(sandbox['cwd'] / artifact.filename).as_uri()}"
    _run([str(_python(prefix)), "-I", "-c", "import fixture_helper"], sandbox)
    assert _verify_owner(sandbox, "pipx", prefix)["version"] == manifest.version


def _wheel_arguments(sandbox, artifact):
    return ["-Wheel" if WINDOWS else "--wheel", str(sandbox["cwd"] / artifact.filename),
            "-Sha256" if WINDOWS else "--sha256", artifact.sha256]


@pytest.mark.parametrize("owner", ["uv", "pipx", "pip", "managed"])
@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing"])
def test_public_installer_accepts_verified_exact_preview(sandbox, owner, existing):
    if owner == "managed":
        prefix = sandbox["root"] / "managed"
        if existing:
            _wheel(sandbox["wheels"], "0.0.1")
            _installer(sandbox)
    else:
        executable, prefix = _owner_environment(sandbox, owner, existing=existing)
        if owner == "pip":
            sandbox["env"]["PYTHON"] = executable
    manifest, artifact = _preview_manifest(sandbox)
    result = _installer(sandbox, *_wheel_arguments(sandbox, artifact))
    identity = _identity(result)
    assert identity["version"] == manifest.version
    assert Path(identity["prefix"]).resolve() == prefix
    assert Path(identity["python"]).absolute() == _python(prefix)
    assert (sandbox["cwd"] / artifact.filename).is_file()


def test_public_installer_rejects_damaged_wheel_before_environment_creation(sandbox):
    _expose(sandbox, "uv")
    _, artifact = _preview_manifest(sandbox, damaged_hash=True)
    result = _installer(sandbox, *_wheel_arguments(sandbox, artifact), check=False)
    assert result.returncode != 0
    assert "SHA-256 mismatch" in result.stdout + result.stderr
    assert not (sandbox["root"] / "uv-tools/nanobot-ai").exists()
    assert not (sandbox["root"] / "managed").exists()


def test_public_installer_does_not_report_pinned_skip_as_preview_success(sandbox):
    pipx, prefix = _owner_environment(sandbox, "pipx", existing=True)
    _run([pipx, "pin", "nanobot-ai"], sandbox)
    _, artifact = _preview_manifest(sandbox)
    result = _installer(sandbox, *_wheel_arguments(sandbox, artifact), check=False)
    assert result.returncode != 0
    assert "requested version was not installed" in result.stdout + result.stderr
    assert "INSTALLER_PROBE" not in result.stdout
    assert _verify_owner(sandbox, "pipx", prefix)["version"] == "0.0.1"


def test_public_installer_wheel_dry_run_validates_but_does_not_install(sandbox):
    _expose(sandbox, "uv")
    manifest, artifact = _preview_manifest(sandbox)
    result = _installer(sandbox, "--dry-run", *_wheel_arguments(sandbox, artifact))
    assert f"verified wheel {manifest.version}" in result.stdout
    assert not (sandbox["root"] / "uv-tools/nanobot-ai").exists()


def test_public_installer_pipx_preview_preserves_requested_extras(sandbox):
    pipx = _expose(sandbox, "pipx")
    prefix = sandbox["root"] / "pipx/venvs/nanobot-ai"
    _wheel(sandbox["wheels"], "0.0.1", extras=("feature",))
    _run([pipx, "install", "--skip-maintenance", "nanobot-ai[feature]"], sandbox)
    _, artifact = _preview_manifest(sandbox, extras=("feature",))
    _installer(sandbox, *_wheel_arguments(sandbox, artifact))
    record = json.loads((prefix / "pipx_metadata.json").read_text())
    assert record["main_package"]["package_or_url"].startswith("nanobot-ai[feature] @ ")


def test_public_installer_uv_preview_preserves_requested_extras(sandbox):
    uv = _expose(sandbox, "uv")
    prefix = sandbox["root"] / "uv-tools/nanobot-ai"
    _wheel(sandbox["wheels"], "0.0.1", extras=("feature",))
    _run([uv, "tool", "install", "--python", BASE_PYTHON, "nanobot-ai[feature]"], sandbox)
    _, artifact = _preview_manifest(sandbox, extras=("feature",))
    _installer(sandbox, *_wheel_arguments(sandbox, artifact))
    record = tomllib.loads((prefix / "uv-receipt.toml").read_text())
    assert any(item.get("extras") == ["feature"] for item in record["tool"]["requirements"])


def test_public_installer_refuses_missing_previously_requested_pipx_extra(sandbox):
    pipx = _expose(sandbox, "pipx")
    prefix = sandbox["root"] / "pipx/venvs/nanobot-ai"
    _wheel(sandbox["wheels"], "0.0.1", extras=("feature",))
    _run([pipx, "install", "--skip-maintenance", "nanobot-ai[feature]"], sandbox)
    _, artifact = _preview_manifest(sandbox)
    result = _installer(sandbox, *_wheel_arguments(sandbox, artifact), check=False)
    assert result.returncode != 0
    assert "does not provide all existing pipx extras" in result.stdout + result.stderr
    assert _verify_owner(sandbox, "pipx", prefix)["version"] == "0.0.1"


def test_public_installer_incomplete_wheel_request_never_falls_back_to_pypi(sandbox):
    _wheel(sandbox["wheels"], "0.0.1")
    flag = "-Wheel" if WINDOWS else "--wheel"
    result = _installer(sandbox, flag, "missing.whl", check=False)
    assert result.returncode != 0
    assert "supplied together" in result.stdout + result.stderr
    assert not (sandbox["root"] / "managed").exists()


def test_public_installer_wrong_platform_keeps_original_environment(sandbox):
    python, prefix = _owner_environment(sandbox, "pip", existing=True)
    sandbox["env"]["PYTHON"] = python
    foreign = "macosx_13_0_arm64" if WINDOWS else "win_amd64"
    wheel = _wheel(sandbox["cwd"], "0.0.2.dev1", platform=foreign)
    artifact = releases.ReleaseArtifact(filename=wheel.name, size=wheel.stat().st_size,
                                        sha256=hashlib.sha256(wheel.read_bytes()).hexdigest())
    result = _installer(sandbox, *_wheel_arguments(sandbox, artifact), check=False)
    assert result.returncode != 0
    assert "retry this exact handoff" in result.stdout + result.stderr
    assert "uv tool install --force" not in result.stdout + result.stderr
    assert _verify_owner(sandbox, "pip", prefix)["version"] == "0.0.1"
