"""Temporary, opt-in observation of the original Settings CI request."""

from __future__ import annotations

import asyncio
import cProfile
import faulthandler
import hashlib
import json
import os
import platform
import pstats
import subprocess
import sys
import threading
import time
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

import pytest

_TARGET = (
    "nanobot/channels/websocket/tests/test_websocket_channel.py"
    "::test_settings_api_returns_safe_subset_and_updates_whitelist"
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--settings-diagnostic-dir", help="Write Settings timing and thread observations")


def pytest_configure(config: pytest.Config) -> None:
    output = config.getoption("--settings-diagnostic-dir")
    if output:
        worker = getattr(config, "workerinput", {}).get("workerid", "controller")
        config.pluginmanager.register(_SettingsProbe(Path(output) / worker), "settings-request-probe")


class _SettingsProbe:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.active = threading.local()
        self.route_active = ContextVar("settings_diagnostic_route", default=False)
        self.events: list[dict] = []
        self.reports: list[dict] = []
        self.caches: dict = {}
        self.started = 0.0
        self.seen = False
        self.sync_complete = False
        self.profile: cProfile.Profile | None = None

    def mark(self, name: str, event: str) -> None:
        self.events.append({
            "name": name,
            "event": event,
            "seconds": time.perf_counter() - self.started,
            "thread_cpu_seconds": time.thread_time(),
            "process_cpu_seconds": time.process_time(),
            "thread": threading.get_ident(),
        })

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: pytest.Item):
        if item.nodeid != _TARGET:
            return (yield)

        from nanobot.llm_usage import service
        from nanobot.llm_usage.store import LLMUsageStore
        from nanobot.webui import (
            settings_api,
            settings_capabilities,
            settings_models,
            settings_system,
        )
        from nanobot.webui.settings_routes import WebUISettingsRouter

        self.seen = True
        self.started = time.perf_counter()
        self.output.mkdir(parents=True, exist_ok=True)
        stack_file = (self.output / "thread-stacks.txt").open("w", encoding="utf-8")
        patches = []
        connect_code = LLMUsageStore._connect.__code__

        def trace_sqlite(frame, event, _arg):
            if frame.f_code is not connect_code:
                return None
            if event == "call":
                self.mark("sqlite_init_enter", "point")
            elif event == "line":
                self.mark(f"sqlite_init_line:{frame.f_lineno}", "point")
            elif event == "return":
                self.mark("sqlite_init_exit", "point")
            elif event == "exception":
                self.mark(f"sqlite_init_exception:{frame.f_lineno}", "point")
            return trace_sqlite

        def patch(owner, name, replacement):
            patches.append((owner, name, getattr(owner, name)))
            setattr(owner, name, replacement)

        def observe(owner, name, label):
            original = getattr(owner, name)

            @wraps(original)
            def call(*args, **kwargs):
                if not getattr(self.active, "enabled", False):
                    return original(*args, **kwargs)
                self.mark(label, "start")
                try:
                    return original(*args, **kwargs)
                finally:
                    self.mark(label, "end")

            patch(owner, name, call)

        for owner, name in (
            (settings_api, "_load_settings_config"),
            (settings_models, "model_settings_payload"),
            (settings_capabilities, "capability_settings_payload"),
            (settings_system, "system_settings_payload"),
            (settings_system, "workspace_sandbox_status"),
            (settings_system, "llm_usage_payload"),
            (service, "get_llm_usage_store"),
            (LLMUsageStore, "_connect"),
            (LLMUsageStore, "_read_connection"),
        ):
            observe(owner, name, owner.__name__ + "." + name)
        for name in ("python_version", "system", "release", "machine", "_wmi_query"):
            observe(platform, name, "platform." + name)

        original_sync = WebUISettingsRouter._handle_settings
        original_dispatch = WebUISettingsRouter.dispatch
        original_to_thread = asyncio.to_thread

        def sync(router):
            self.mark("settings_sync", "start")
            self.active.enabled = True
            self.profile = cProfile.Profile()
            previous_trace = sys.gettrace()
            self.caches["sync_had_trace"] = previous_trace is not None
            sys.settrace(trace_sqlite)
            self.profile.enable()
            try:
                return original_sync(router)
            finally:
                self.profile.disable()
                sys.settrace(previous_trace)
                self.active.enabled = False
                self.sync_complete = True
                self.mark("settings_sync", "end")

        async def to_thread(func, /, *args, **kwargs):
            if not self.route_active.get():
                return await original_to_thread(func, *args, **kwargs)
            self.mark("executor_await", "start")
            try:
                return await original_to_thread(func, *args, **kwargs)
            finally:
                self.mark("executor_await", "end")

        async def dispatch(router, connection, request, path):
            if path != "/api/settings":
                return await original_dispatch(router, connection, request, path)
            self.caches = {
                "uname_cached_at_route": platform._uname_cache is not None,
                "wmi_available": bool(platform._wmi),
            }
            token = self.route_active.set(True)
            faulthandler.dump_traceback_later(0.5, repeat=True, file=stack_file)
            self.mark("settings_async", "start")
            try:
                return await original_dispatch(router, connection, request, path)
            finally:
                self.mark("settings_async", "end")
                faulthandler.cancel_dump_traceback_later()
                self.route_active.reset(token)

        patch(WebUISettingsRouter, "_handle_settings", sync)
        patch(WebUISettingsRouter, "dispatch", dispatch)
        patch(asyncio, "to_thread", to_thread)
        self.mark("test_call", "start")
        try:
            return (yield)
        finally:
            self.mark("test_call", "end")
            faulthandler.cancel_dump_traceback_later()
            stack_file.close()
            for owner, name, original in reversed(patches):
                setattr(owner, name, original)
            self.save("after-call")

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.nodeid == _TARGET:
            self.reports.append({
                "phase": report.when,
                "outcome": report.outcome,
                "seconds": report.duration,
            })

    def save(self, phase: str) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        if self.profile is not None and self.sync_complete:
            with (self.output / f"profile-{phase}.txt").open("w", encoding="utf-8") as stream:
                pstats.Stats(self.profile, stream=stream).sort_stats("cumulative").print_stats(80)
        (self.output / f"observations-{phase}.json").write_text(json.dumps({
            "python": sys.version,
            "cpu_count": os.cpu_count(),
            "profile_complete": self.sync_complete,
            "caches": self.caches,
            "events": self.events,
            "target_reports": self.reports,
        }, indent=2), encoding="utf-8")

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        if not self.seen:
            return
        self.save("session-finish")
        files = (
            "nanobot/webui/settings_routes.py",
            "nanobot/webui/settings_api.py",
            "nanobot/webui/settings_system.py",
            "nanobot/llm_usage/store.py",
            "nanobot/channels/websocket/tests/test_websocket_channel.py",
            "nanobot/channels/websocket/tests/ws_test_client.py",
            "conftest.py",
            "pyproject.toml",
            ".diagnostics/settings/base.uv.lock",
            ".diagnostics/settings/base-packages.json",
            ".diagnostics/settings/final-packages.json",
        )
        (self.output / "snapshot.json").write_text(json.dumps({
            "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "parents": subprocess.check_output(["git", "show", "-s", "--format=%P", "HEAD"], text=True).strip(),
            "exitstatus": int(exitstatus),
            "files": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in files},
        }, indent=2), encoding="utf-8")
