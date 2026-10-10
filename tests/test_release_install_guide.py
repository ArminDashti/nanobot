"""The manual handoff binds commands to the verified version and platform bytes."""

import shlex

import pytest

from nanobot.releases import PLATFORMS, ReleaseArtifact, ReleaseManifest, release_filenames
from scripts.release_install_guide import install_command, render_install_guide


@pytest.fixture
def manifest():
    return ReleaseManifest(
        version="0.4.0.dev123", source_commit="a" * 40, candidate_commit="b" * 40,
        requires_python=">=3.11",
        artifacts=[ReleaseArtifact(filename=name, size=123, sha256="c" * 64)
                   for name in sorted(release_filenames("0.4.0.dev123"))],
    )


def test_handoff_commands_bind_every_platform_to_its_exact_release(manifest):
    guide = render_install_guide(manifest)
    assert manifest.source_commit in guide
    assert manifest.candidate_commit in guide
    blocks = [block.split("\n", 1)[1].strip() for block in guide.split("```")[1::2]]
    commands = [shlex.split(block) for block in blocks if block.startswith("python -I -c ")]
    assert len(commands) == 5 * 3 * 2
    for platform in PLATFORMS.values():
        artifact = next(item for item in manifest.artifacts if item.filename.endswith(f"-{platform}.whl"))
        assert manifest.artifact_url(artifact) in guide
        expected = [artifact.filename, str(artifact.size), artifact.sha256]
        assert sum(command[4:7] == expected for command in commands) == 6
    assert all("--pre" not in command and "--force" not in command for command in commands)


def test_handoff_cannot_generate_install_commands_for_source_or_foreign_artifact(manifest):
    source = next(item for item in manifest.artifacts if item.filename.endswith(".tar.gz"))
    with pytest.raises(ValueError, match="platform wheel"):
        install_command(manifest, source, owner="pip", existing=False)
    wheel = next(item for item in manifest.artifacts if item.filename.endswith(".whl"))
    with pytest.raises(ValueError, match="does not belong"):
        install_command(manifest, wheel.model_copy(update={"sha256": "d" * 64}), owner="uv", existing=True)
