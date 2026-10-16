from pathlib import Path

import pytest

from nanobot.api.runtime import ApiRuntime, ApiStartOptions, api_runtime_paths


class FakeProcess:
    pid = 23456


def test_api_runtime_uses_home_paths(tmp_path: Path) -> None:
    paths = api_runtime_paths(tmp_path)

    assert paths.state_path.parent == tmp_path / "run"
    assert paths.state_path.name == "api.json"
    assert paths.log_path.parent == tmp_path / "logs"
    assert paths.log_path.name == "api.log"


def test_api_runtime_builds_detached_serve_command(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        calls.append(command)
        return FakeProcess()

    runtime = ApiRuntime(
        paths=api_runtime_paths(tmp_path),
        platform_name="Linux",
        python_executable="/python",
        popen=fake_popen,
        sleep=lambda _seconds: None,
    )
    monkeypatch.setattr(runtime, "_is_pid_running", lambda _pid: True)
    monkeypatch.setattr(runtime, "_process_identity", lambda _pid: 23456)

    result = runtime.start_background(ApiStartOptions(
        host="0.0.0.0",
        port=9900,
        workspace="/tmp/workspace",
        config_path="/tmp/config.json",
        home=str(tmp_path),
    ))

    assert result.ok is True
    assert result.message == "api_started_background"
    assert calls == [[
        "/python",
        "-m",
        "nanobot",
        "--home",
        str(tmp_path),
        "serve",
        "--host",
        "0.0.0.0",
        "--port",
        "9900",
        "--workspace",
        "/tmp/workspace",
        "--config",
        "/tmp/config.json",
    ]]


def test_api_home_rejects_changed_config_without_restarting(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        calls.append(command)
        return FakeProcess()

    runtime = ApiRuntime(
        paths=api_runtime_paths(tmp_path), popen=fake_popen,
        sleep=lambda _seconds: None,
    )
    monkeypatch.setattr(runtime, "_is_pid_running", lambda _pid: True)
    monkeypatch.setattr(runtime, "_process_identity", lambda _pid: 23456)
    first = ApiStartOptions(
        port=9900, home=str(tmp_path), config_path=str(tmp_path / "first.json"),
        workspace=str(tmp_path / "workspace"),
    )
    changed = ApiStartOptions(
        port=9900, home=str(tmp_path), config_path=str(tmp_path / "second.json"),
        workspace=str(tmp_path / "workspace"),
    )
    assert runtime.start_background(first).ok

    result = runtime.restart(changed)

    assert result.message == "api_instance_mismatch"
    assert result.status.config_path == first.config_path
    assert result.status.running
    assert len(calls) == 1


def test_foreground_api_claim_is_home_scoped(tmp_path: Path, monkeypatch) -> None:
    runtime = ApiRuntime(paths=api_runtime_paths(tmp_path))
    monkeypatch.setattr(runtime, "_is_pid_running", lambda _pid: True)
    monkeypatch.setattr(runtime, "_process_identity", lambda _pid: 23456)
    first = ApiStartOptions(
        port=9900, home=str(tmp_path), config_path=str(tmp_path / "first.json"),
        workspace=str(tmp_path / "workspace"),
    )
    changed = ApiStartOptions(
        port=9900, home=str(tmp_path), config_path=str(tmp_path / "second.json"),
        workspace=str(tmp_path / "workspace"),
    )

    with runtime.foreground_instance(first):
        status = runtime.status()
        assert status.running and status.home == str(tmp_path)
        assert runtime.restart(first).message == "api_foreground_restart_required"
        with pytest.raises(RuntimeError, match="api_instance_mismatch"):
            with runtime.foreground_instance(changed):
                pass

    assert not runtime.paths.state_path.exists()
