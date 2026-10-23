"""Inspect native CI build bytes without changing the release verifier or its limits."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

from nanobot.cli import tui_launcher

ROOT = Path(__file__).resolve().parents[1]


def file_info(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": digest}


def pe_layout(path: Path) -> dict[str, object] | None:
    """Read the Windows image section table, not executable code."""
    with path.open("rb") as stream:
        header = stream.read(64)
        if header[:2] != b"MZ" or len(header) < 64:
            return None
        pe_offset = struct.unpack_from("<I", header, 60)[0]
        stream.seek(pe_offset)
        header = stream.read(24)
        if header[:4] != b"PE\x00\x00":
            raise ValueError(f"Invalid PE signature: {path.name}")
        machine, count = struct.unpack_from("<HH", header, 4)
        optional_size = struct.unpack_from("<H", header, 20)[0]
        stream.seek(optional_size, 1)
        sections = []
        image_end = pe_offset + 24 + optional_size + 40 * count
        for _ in range(count):
            section = stream.read(40)
            virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from("<IIII", section, 8)
            sections.append({"name": section[:8].rstrip(b"\x00").decode("ascii", errors="replace"),
                             "virtual_bytes": virtual_size, "virtual_address": virtual_address,
                             "raw_bytes": raw_size, "raw_offset": raw_offset})
            image_end = max(image_end, raw_offset + raw_size)
    return {"machine": hex(machine), "sections": sections,
            "bytes_after_last_section": path.stat().st_size - image_end}


def inspect_build(tui_dir: Path, target: str) -> dict[str, object]:
    asset = f"nanobot-tui-{target}" + (".exe" if target.startswith("win32-") else "")
    executable = tui_dir / asset
    archive_path = tui_dir / f"{asset}.zip"
    with zipfile.ZipFile(archive_path) as archive:
        entries = []
        for entry in archive.infolist():
            with archive.open(entry) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            entries.append({"name": entry.filename, "uncompressed_bytes": entry.file_size,
                            "compressed_bytes": entry.compress_size, "sha256": digest,
                            "compression": entry.compress_type, "crc32": hex(entry.CRC)})
        manifest = archive.read("MANIFEST.sha256").decode("utf-8")
    binary = file_info(executable)
    distribution = importlib.metadata.distribution("nanobot-ai")
    bun_path = shutil.which("bun")
    if bun_path is None:
        raise RuntimeError("The native build's Bun executable is not on PATH")
    bun = Path(bun_path)
    return {
        "target": target,
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "ci": {key: os.environ.get(key) for key in (
            "GITHUB_SHA", "GITHUB_REF", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_OS", "ImageVersion",
        )},
        "python": {"version": sys.version, "executable": sys.executable,
                   "platform": platform.platform(), "machine": platform.machine(),
                   "pointer_bits": struct.calcsize("P") * 8},
        "bun": {**file_info(bun), "pe": pe_layout(bun),
                "version": subprocess.check_output([str(bun), "--version"], text=True).strip(),
                "revision": subprocess.check_output([str(bun), "--revision"], text=True).strip()},
        "executable": {**binary, "pe": pe_layout(executable)},
        "archive": {**file_info(archive_path), "entries": entries, "manifest": manifest,
                    "checksum_file": archive_path.with_suffix(".zip.sha256").read_text(encoding="utf-8")},
        "verifier": {
            "loaded_module": file_info(Path(tui_launcher.__file__)),
            "installed_distribution_version": distribution.version,
            "installed_module": file_info(Path(distribution.locate_file("nanobot/cli/tui_launcher.py"))),
            # The binary limit is local to this function: retain the loaded source,
            # rather than duplicating a threshold that could drift from the guard.
            "loaded_archive_verifier_source": inspect.getsource(tui_launcher._verified_release_archive),
            "companion_limits": tui_launcher._TUI_RELEASE_LIMITS,
        },
        "build_inputs": {relative: file_info(ROOT / relative) for relative in (
            "tui/scripts/build.ts", "tui/scripts/package-release.py", "tui/package.json", "tui/bun.lock",
            "scripts/build_tui_wheels.py", "nanobot/cli/tui_launcher.py", "pyproject.toml",
        )},
        "build_script": (ROOT / "tui/scripts/build.ts").read_text(encoding="utf-8"),
        "opentui_version": json.loads((ROOT / "tui/node_modules/@opentui/core/package.json").read_text())["version"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("win32-x64", "linux-x64"), required=True)
    parser.add_argument("--tui-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = inspect_build(args.tui_dir, args.target)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)


if __name__ == "__main__":
    main()
