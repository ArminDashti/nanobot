"""Render the initial manual install handoff for a verified release candidate.

This is not a channel resolver or an updater. The caller must verify the candidate
bytes first; public availability, migration notes and native acceptance are release
gates, not properties inferred from a manifest.
"""

from __future__ import annotations

import shlex
from typing import Literal

from nanobot.releases import PLATFORMS, ReleaseArtifact, ReleaseManifest

Owner = Literal["uv", "pipx", "pip"]
PLATFORM_LABELS = {
    "darwin-arm64": "macOS 13+ · Apple silicon",
    "darwin-x64": "macOS 13+ · Intel x64",
    "linux-arm64": "Linux · glibc 2.17+ · ARM64",
    "linux-x64": "Linux · glibc 2.17+ · x64",
    "win32-x64": "Windows · x64",
}

# Uses only Python's standard library, so the first preview can be installed before
# the user's nanobot has a channel-aware updater. The downloaded file is retained:
# managers can record its location as the installation source.
VERIFY_AND_INSTALL = '''from pathlib import Path
import hashlib
import subprocess
import sys
wheel = Path(sys.argv[1]).resolve(strict=True)
with wheel.open("rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
if wheel.stat().st_size != int(sys.argv[2]) or digest != sys.argv[3]:
    raise SystemExit("Wheel size or SHA-256 mismatch; no package manager was started.")
command = sys.argv[4:]
if command[0] == "python":
    command[0] = sys.executable
subprocess.run([*command, "nanobot-ai @ " + wheel.as_uri()], check=True)
'''


def install_command(manifest: ReleaseManifest, artifact: ReleaseArtifact, *,
                    owner: Owner, existing: bool) -> list[str]:
    """Verify saved bytes before asking the original owner to install that file."""
    if not artifact.filename.endswith(".whl"):
        raise ValueError("Install instructions require a platform wheel")
    if artifact not in manifest.artifacts:
        raise ValueError("Artifact does not belong to this release")
    if owner == "uv":
        command = ["uv", "tool", "upgrade" if existing else "install", "--no-python-downloads"]
    elif owner == "pipx":
        command = ["pipx", "install", "--skip-maintenance", *(["--upgrade"] if existing else [])]
    elif owner == "pip":
        command = ["python", "-I", "-m", "pip", "install", *(["--upgrade"] if existing else [])]
    else:
        raise ValueError(f"Unsupported installation owner: {owner}")
    return ["python", "-I", "-c", VERIFY_AND_INSTALL, artifact.filename,
            str(artifact.size), artifact.sha256, *command]


def verification_command(owner: Owner, *, windows: bool) -> str:
    """Inspect the persistent environment, never an ephemeral uvx/pipx run cache."""
    if owner == "pip":
        return "python -I -m nanobot --version"
    directory = "uv tool dir" if owner == "uv" else "pipx environment --value PIPX_LOCAL_VENVS"
    if windows:
        return f"& (Join-Path ({directory}) 'nanobot-ai\\Scripts\\python.exe') -I -m nanobot --version"
    return f'"$({directory})/nanobot-ai/bin/python" -I -m nanobot --version'


def render_install_guide(manifest: ReleaseManifest) -> str:
    release_url = f"https://github.com/HKUDS/nanobot/releases/tag/v{manifest.version}"
    lines = [
        f"# Install nanobot {manifest.version}", "",
        "> Candidate handoff — not publication approval. Use these commands only after",
        "> the maintainer has published and publicly verified this exact release, added",
        "> its changes/migration/recovery notes, and declared its native test coverage.", "",
        f"- Release: [{manifest.version}]({release_url})",
        f"- Source commit: `{manifest.source_commit}`",
        f"- Packaged candidate: `{manifest.candidate_commit}`",
        f"- Python requirement: `{manifest.requires_python}`", "",
        "The wheel contains the built WebUI and matching native TUI. Installing nanobot",
        "itself needs no Bun, Node or Git. Python and third-party dependencies have their",
        "own requirements. Commands pin this one release; they do not subscribe to Preview.", "",
        "## Before installing", "",
        "1. Read this release's changes, known limitations and recovery instructions.",
        "   Prefer a separate environment **and separate data/profile/port** for an early",
        "   preview. A second environment alone does not isolate the default nanobot data.",
        "2. For an existing install, identify its manager, interpreter and supervisor.",
        "   Finish active tasks, stop all affected writers through that supervisor, and",
        "   back up nanobot configuration/sessions and the owner's install metadata.",
        "   Do not install into a running gateway or assume this guide performs a backup.",
        "3. Choose the target computer's platform below, then **one** owner/operation.",
        "   Fresh commands are only for a new install; existing commands must target the",
        "   same account and manager home. For pip, activate the intended ordinary venv",
        "   and verify `python` belongs to it; never use system Python or a uv/pipx venv.", "",
        "4. Download the linked wheel into a directory you own and intend to keep. Open",
        "   a terminal in that directory. Each command uses Python 3.11+ (`python`) to",
        "   verify the saved file's size and SHA-256 **before** starting the manager.",
        "   For uv/pipx this helper Python does not replace the tool's interpreter.",
        "   Keep the wheel at that path: the manager may record it as the install source.", "",
        "These commands cover a base `nanobot-ai` installation. If you use extras, a",
        "pipx suffix/lock, custom wrappers or several installations, review the recorded",
        "spec and target explicitly instead of copying a base-package command. Preserve",
        "extras, injected packages, Python and index settings. Docker, Desktop, editable",
        "checkouts and system packages use their own deployment instructions.", "",
        "Command versions checked by the installer fixtures: uv 0.11.16 and pipx 1.17.12.",
        "Check your manager's version/help; older versions may not support these flags.",
        "This guide does not upgrade the manager. uv retains existing version constraints;",
        "pipx can skip a pinned/locked package successfully. A zero exit code alone does",
        "not prove the requested version was installed. Do not force/unpin automatically.", "",
        "## Select the target platform", "",
        "Use the architecture of the target Python environment, not the browser. No musl",
        "or Windows ARM64 wheel is provided. x64 native TUI builds require SSE4.2.",
        "A platform label is not proof of a native test; consult the release's coverage.", "",
    ]
    for target, platform in PLATFORMS.items():
        artifact = next(item for item in manifest.artifacts
                        if item.filename.endswith(f"-py3-none-{platform}.whl"))
        shell = "powershell" if target == "win32-x64" else "sh"
        lines += [f"### {PLATFORM_LABELS[target]}", "",
                  f"Download: [{artifact.filename}]({manifest.artifact_url(artifact)}) ({artifact.size:,} bytes).",
                  f"SHA-256: `{artifact.sha256}`.", ""]
        owners: tuple[tuple[Owner, str], ...] = (
            ("uv", "uv tool"), ("pipx", "pipx"), ("pip", "pip in an ordinary venv"),
        )
        for owner, title in owners:
            lines += [f"#### {title}", "", "New installation:", "", f"```{shell}",
                      shlex.join(install_command(manifest, artifact, owner=owner, existing=False)),
                      "```", "", "Existing base-package installation, after stopping and backing up:", "",
                      f"```{shell}", shlex.join(install_command(manifest, artifact, owner=owner, existing=True)),
                      "```", "", "Verify the version from this owner's persistent environment:", "",
                      f"```{shell}", verification_command(owner, windows=target == "win32-x64"), "```", ""]
    lines += [
        "## Verify, restart and recover", "",
        f"The verification command must report `{manifest.version}`. If it reports another",
        "version, stop and inspect the manager result and installation identity. Do not",
        "retry through another manager, run `pipx run`/`uvx`, or clear an intentional pin.", "",
        "After package verification, restart the original gateway with its original",
        "supervisor, profile and arguments. Verify that gateway's actual running version",
        "and readiness, then check that existing sessions can be opened and appended to.",
        "CLI version output is not proof that an already-running service has updated.", "",
        "If download or installation fails, retain the error and backup, and do not start",
        "an unverified environment. Follow the release's owner-specific recovery steps.",
        "Retry this exact candidate only if it is still eligible. Never replace a backup",
        "with partially migrated state. A package downgrade is not a data rollback; stop",
        "all writers and review data compatibility before restoring or downgrading.", "",
        "GitHub-only previews are not discovered by an ordinary index-only `pip --pre`",
        "upgrade. A local wheel may remain in a manager receipt or be a one-time override;",
        "neither means a daily-update subscription has been created.", "",
    ]
    return "\n".join(lines)
