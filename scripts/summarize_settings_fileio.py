"""Filter Kernel-File ETW to the diagnostic's private usage DB operations."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
NAMES = {12: "Create", 13: "Cleanup", 14: "Close", 15: "Read", 16: "Write", 17: "SetInformation",
         18: "SetDelete", 19: "Rename", 20: "DirEnum", 21: "Flush", 22: "QueryInformation",
         23: "FSCTL", 24: "OperationEnd", 25: "DirNotify", 29: "Rename", 30: "CreateNewFile"}


def number(value: str | int) -> int:
    if isinstance(value, int):
        return value
    return int(value, 16 if value.lower().startswith("0x") else 10)


def timestamp_ns(text: str) -> int:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = value - epoch
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def summarize(xml: Path, records: list[dict], output: Path) -> dict:
    pids = {r["pid"]: r["execution"] for r in records}
    threads = {}
    for file in output.glob("execution-*/observations/*/observations-session-finish.json"):
        data = json.loads(file.read_text(encoding="utf-8-sig"))
        for event in data["events"]:
            threads[event["native_thread"]] = event["pid"]
    files: dict[str, str] = {}
    pending = {}
    operations = []
    scoped_events = []
    types = Counter()
    total = 0
    unresolved = 0
    for _, element in ET.iterparse(xml, events=("end",)):
        if element.tag != "{" + NS["e"] + "}Event":
            continue
        provider = element.find("e:System/e:Provider", NS)
        if provider is None or provider.get("Name") != "Microsoft-Windows-Kernel-File":
            element.clear()
            continue
        total += 1
        event_id = int(element.findtext("e:System/e:EventID", namespaces=NS))
        types[event_id] += 1
        execution = element.find("e:System/e:Execution", NS)
        clock = element.find("e:System/e:TimeCreated", NS)
        header_pid = number(execution.get("ProcessID"))
        header_tid = number(execution.get("ThreadID"))
        fields = {d.get("Name"): d.text or "" for d in element.findall("e:EventData/e:Data", NS)}
        timestamp = timestamp_ns(clock.get("SystemTime"))
        filename = fields.get("FileName", "")
        is_database = "\\runtime-data\\" in filename.lower() and "llm_usage.sqlite3" in filename.lower()
        for key in ("FileObject", "FileKey"):
            if fields.get(key) and filename:
                if is_database:
                    files[fields[key]] = filename
                else:
                    files.pop(fields[key], None)
        if event_id == 11:
            files.pop(fields.get("FileKey"), None)
        if event_id == 24:
            previous = pending.pop(fields.get("Irp"), None)
            if previous:
                previous.update({"end_unix_ns": timestamp, "duration_ms": (timestamp - previous["start_unix_ns"]) / 1_000_000,
                                 "status": fields.get("NtStatus"), "completion_pid": header_pid})
                operations.append(previous)
        else:
            thread = number(fields.get("IssuingThreadId", fields.get("ThreadId", header_tid)))
            pid = threads.get(thread, header_pid)
            name = filename if is_database else files.get(fields.get("FileObject"), files.get(fields.get("FileKey"), ""))
            if pid in pids and name:
                record = {"execution": pids[pid], "pid": pid, "native_thread": thread,
                          "operation": NAMES.get(event_id, str(event_id)), "event_id": event_id,
                          "path": name, "start_unix_ns": timestamp, "size": fields.get("IOSize")}
                scoped_events.append(record.copy())
                if fields.get("Irp"):
                    if fields["Irp"] in pending:
                        unresolved += 1
                    pending[fields["Irp"]] = record
        element.clear()
    return {"provider": "Microsoft-Windows-Kernel-File", "total_provider_events": total,
            "provider_event_types": dict(types), "scoped_start_events": scoped_events,
            "operations": operations, "unmatched_scoped_starts": len(pending) + unresolved,
            "meaning": "IRP start-to-OperationEnd elapsed time, not physical-disk-only latency"}
