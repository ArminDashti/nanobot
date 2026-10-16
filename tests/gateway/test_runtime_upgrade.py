import hashlib
import json
import subprocess
import sys
from contextlib import contextmanager

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
def live_record(runtime, options, path):
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


def test_upgrade_reads_and_follows_selected_owners_rotated_logs(component, monkeypatch, capsys):
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
            log_path.replace(backup)
            log_path.write_text("after-rotation\n", encoding="utf-8")
            rotated = True

        monkeypatch.setattr(runtime, "_sleep", rotate_then_interrupt)
        assert runtime.follow_logs(tail=2) == 130

    assert capsys.readouterr().out.splitlines() == ["older", "current", "after-rotation"]
    assert not runtime.paths.log_path.exists()
