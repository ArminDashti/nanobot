import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace

import pytest

from nanobot.api.runtime import ApiRuntime, ApiStartOptions, api_runtime_paths
from nanobot.gateway.runtime import GatewayInstance, GatewayRuntime
from nanobot.process_runtime import RuntimeOwnershipConflictError


@pytest.fixture(params=["gateway", "api"])
def component(request, tmp_path):
    config = tmp_path / "custom.json"
    workspace = str(tmp_path / "workspace")
    if request.param == "gateway":
        instance = GatewayInstance.resolve(home=tmp_path, config_path=config, workspace=workspace)
        runtime = GatewayRuntime(paths=instance.paths)
        options = instance.start_options(port=18790)
        suffix = hashlib.sha1(f"{workspace}|{config}".encode()).hexdigest()[:16]
    else:
        runtime = ApiRuntime(paths=api_runtime_paths(tmp_path))
        options = ApiStartOptions(
            port=18791, config_path=str(config), workspace=workspace, home=str(tmp_path),
        )
        suffix = hashlib.sha256(str(config).encode()).hexdigest()[:16]
    return runtime, options, tmp_path / "run" / f"{request.param}.{suffix}.json"


@contextmanager
def live_record(runtime, options, path, *, command=()):
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", "print('ready'); input()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": child.pid,
            "port": options.port,
            "config_path": options.config_path,
            "workspace": options.workspace,
            "launch_mode": "background",
            "command": command,
            **runtime.process_identity_record(child.pid),
        }))
        yield child
    finally:
        if child.poll() is None:
            child.terminate()
        child.communicate(timeout=5)


def test_upgrade_discovers_blocks_and_stops_live_legacy_owner(component):
    runtime, options, legacy_path = component
    with live_record(runtime, options, legacy_path) as child:
        status = runtime.status()
        assert status.running and status.pid == child.pid
        assert status.state_path == legacy_path
        assert status.log_path == runtime.paths.logs_dir / f"{legacy_path.stem}.log"
        assert status.config_path == options.config_path
        assert status.workspace == options.workspace
        assert not runtime.start_background(options).ok
        with pytest.raises(RuntimeError):
            with runtime.foreground_instance(options):
                pytest.fail("claimed a home with a live legacy owner")
        assert child.poll() is None
        assert not runtime.paths.state_path.exists()

        result = runtime.stop(timeout_s=2)

        assert result.ok
        child.wait(timeout=5)
        assert not runtime.process_is_running(child.pid)
        assert not legacy_path.exists()
        with runtime.foreground_instance(options):
            assert runtime.status().state_path == runtime.paths.state_path
            assert runtime.paths.state_path.exists()


@pytest.mark.parametrize("second_is_canonical", [False, True])
def test_upgrade_refuses_multiple_live_owners(component, second_is_canonical):
    runtime, options, legacy_path = component
    second_path = (
        runtime.paths.state_path if second_is_canonical
        else legacy_path.with_name(f"{runtime.service_name}.0123456789abcdef.json")
    )
    with live_record(runtime, options, legacy_path) as first:
        with live_record(runtime, options, second_path) as second:
            for operation in (runtime.status, runtime.stop, lambda: runtime.start_background(options)):
                with pytest.raises(RuntimeOwnershipConflictError) as conflict:
                    operation()
                message = str(conflict.value)
                assert str(legacy_path) in message and str(first.pid) in message
                assert str(second_path) in message and str(second.pid) in message
                assert "Stop them from their original terminals or process supervisors" in message
            with pytest.raises(RuntimeOwnershipConflictError):
                with runtime.foreground_instance(options):
                    pytest.fail("claimed a home with conflicting owners")
            assert first.poll() is None and second.poll() is None


def test_upgrade_discards_reused_pid_without_signalling_process(component):
    runtime, options, legacy_path = component
    with live_record(runtime, options, legacy_path) as unrelated:
        record = json.loads(legacy_path.read_text())
        record.update(identity="replaced-process", stable_identity="replaced-process")
        legacy_path.write_text(json.dumps(record))

        result = runtime.stop(timeout_s=1)

        assert not result.ok
        assert result.status.running is False
        assert unrelated.poll() is None
        assert not legacy_path.exists()
        with runtime.foreground_instance(options):
            assert runtime.status().running
        assert unrelated.poll() is None


def test_upgrade_keeps_unverifiable_live_owner_without_signalling(component):
    runtime, options, legacy_path = component
    with live_record(runtime, options, legacy_path) as child:
        record = json.loads(legacy_path.read_text())
        record.pop("identity", None)
        record.pop("stable_identity", None)
        legacy_path.write_text(json.dumps(record))

        status = runtime.status()
        result = runtime.stop(timeout_s=1)

        assert status.running and status.reason == "identity_unavailable"
        assert result.message == f"{runtime.service_name}_identity_unavailable"
        assert not runtime.start_background(options).ok
        assert child.poll() is None
        assert legacy_path.exists()


@pytest.mark.parametrize("rotate", [
    False,
    pytest.param(True, marks=pytest.mark.skipif(os.name == "nt", reason="Windows readers prevent log rename")),
])
def test_upgrade_reads_and_follows_selected_owners_logs(component, monkeypatch, capsys, rotate):
    runtime, options, legacy_path = component
    with live_record(runtime, options, legacy_path):
        log_path = runtime.status().log_path
        log_path.parent.mkdir(parents=True)
        backup = log_path.with_name(f"{log_path.name}.1")
        backup.write_text("older\n", encoding="utf-8")
        log_path.write_text("current\n", encoding="utf-8")
        assert runtime.read_log_tail(tail=2) == ["older", "current"]
        rotated = False

        def rotate_then_interrupt(_seconds):
            nonlocal rotated
            if rotated:
                raise KeyboardInterrupt
            if rotate:
                log_path.replace(backup)
            with log_path.open("w" if rotate else "a", encoding="utf-8") as stream:
                stream.write("new-output\n")
            rotated = True

        monkeypatch.setattr(runtime, "_sleep", rotate_then_interrupt)
        assert runtime.follow_logs(tail=2) == 130

    assert capsys.readouterr().out.splitlines() == ["older", "current", "new-output"]
    assert not runtime.paths.log_path.exists()


@pytest.fixture
def external_component(component, tmp_path):
    runtime, options, legacy_path = component
    config = tmp_path / "external" / "custom.json"
    options = replace(options, config_path=str(config))
    if runtime.service_name == "gateway":
        instance = GatewayInstance.resolve(
            home=tmp_path, config_path=config, workspace=options.workspace,
        )
        runtime = GatewayRuntime(paths=instance.paths)
    else:
        runtime = ApiRuntime(paths=api_runtime_paths(tmp_path, config_path=config))
    return runtime, options, config.parent / "run" / legacy_path.name


def test_upgrade_manages_external_gateway_owner(tmp_path):
    config = tmp_path / "external" / "custom.json"
    instance = GatewayInstance.resolve(home=tmp_path, config_path=config)
    runtime = GatewayRuntime(paths=instance.paths)
    options = instance.start_options(port=18790)
    legacy_path = config.parent / "run" / "gateway.0123456789abcdef.json"
    command = ["python", "-m", "nanobot", "--home", str(tmp_path), "gateway", "--config", str(config)]
    with live_record(runtime, options, legacy_path, command=command) as child:
        log_path = config.parent / "logs" / f"{legacy_path.stem}.log"
        log_path.parent.mkdir()
        log_path.write_text("external owner\n", encoding="utf-8")

        status = runtime.status()
        assert status.pid == child.pid and status.state_path == legacy_path
        assert status.home is None
        assert runtime.selectors_mismatch(status, options)
        assert status.log_path == log_path
        assert runtime.read_log_tail() == ["external owner"]
        assert not runtime.start_background(options).ok
        with pytest.raises(RuntimeError):
            with runtime.foreground_instance(options):
                pytest.fail("claimed a home with a live external owner")
        assert not runtime.paths.state_path.exists()
        assert runtime.stop(timeout_s=2).ok
        child.wait(timeout=5)
        assert not runtime.process_is_running(child.pid)
        with runtime.foreground_instance(options):
            assert runtime.status().state_path == runtime.paths.state_path


def test_upgrade_ignores_unrelated_external_config(external_component, tmp_path):
    runtime, options, legacy_path = external_component
    recorded_options = replace(
        options, config_path=str(tmp_path / "external" / "other.json"),
    )
    with live_record(runtime, recorded_options, legacy_path) as child:
        assert not runtime.status().running
        assert not runtime.stop(timeout_s=1).ok
        with runtime.foreground_instance(options):
            assert runtime.status().state_path == runtime.paths.state_path
        assert child.poll() is None
        assert legacy_path.exists()


@pytest.mark.parametrize("component", ["gateway"], indirect=True)
def test_upgrade_ignores_external_gateway_for_another_home(external_component, tmp_path):
    runtime, options, legacy_path = external_component
    command = ["python", "-m", "nanobot", "--home", str(tmp_path / "other"), "gateway"]
    with live_record(runtime, options, legacy_path, command=command) as child:
        assert not runtime.status().running
        assert not runtime.stop(timeout_s=1).ok
        with runtime.foreground_instance(options):
            assert runtime.status().state_path == runtime.paths.state_path
        assert child.poll() is None
        assert legacy_path.exists()


def test_upgrade_blocks_external_record_without_home_evidence(external_component):
    runtime, options, legacy_path = external_component
    with live_record(runtime, options, legacy_path) as child:
        for operation in (runtime.status, runtime.stop, lambda: runtime.start_background(options)):
            with pytest.raises(RuntimeOwnershipConflictError) as conflict:
                operation()
            assert "does not identify its home" in str(conflict.value)
            assert str(legacy_path) in str(conflict.value)
            assert str(child.pid) in str(conflict.value)
        with pytest.raises(RuntimeOwnershipConflictError):
            with runtime.foreground_instance(options):
                pytest.fail("claimed a home with an ambiguous external owner")
        assert child.poll() is None
        assert legacy_path.exists()
        assert not runtime.paths.state_path.exists()


def test_canonical_owner_remains_manageable_with_invalid_external_config(external_component):
    runtime, options, _ = external_component
    config = runtime.paths.legacy_config_path
    config.parent.mkdir(parents=True)
    config.write_text("{invalid json", encoding="utf-8")
    with live_record(runtime, options, runtime.paths.state_path) as child:
        assert runtime.status().pid == child.pid
        assert runtime.stop(timeout_s=2).ok
        child.wait(timeout=5)


def test_canonical_owner_is_authoritative_over_ambiguous_external_record(external_component):
    runtime, options, legacy_path = external_component
    with live_record(runtime, options, legacy_path) as ambiguous:
        with live_record(runtime, options, runtime.paths.state_path) as canonical:
            status = runtime.status()
            assert status.pid == canonical.pid
            assert status.state_path == runtime.paths.state_path
            assert not runtime.start_background(options).ok
            result = runtime.stop(timeout_s=2)
            assert result.ok and not result.status.running
            canonical.wait(timeout=5)
            assert ambiguous.poll() is None
            assert legacy_path.exists()
        with pytest.raises(RuntimeOwnershipConflictError):
            runtime.start_background(options)


@pytest.mark.parametrize("component", ["gateway"], indirect=True)
def test_canonical_owner_conflicts_with_proven_external_owner(external_component):
    runtime, options, legacy_path = external_component
    command = ["python", "-m", "nanobot", "--home", options.home, "gateway"]
    with live_record(runtime, options, legacy_path, command=command) as legacy:
        with live_record(runtime, options, runtime.paths.state_path) as canonical:
            for operation in (runtime.status, runtime.stop):
                with pytest.raises(RuntimeOwnershipConflictError):
                    operation()
            assert canonical.poll() is None
            assert legacy.poll() is None
