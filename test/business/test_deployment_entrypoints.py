from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_docker_install_has_workspace_sources_before_pip_and_no_model_sdks():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    workspace_paths = [
        line.split(None, 1)[1].removeprefix("./")
        for line in requirements
        if line.strip().startswith("-e ./packages/")
    ]
    assert set(workspace_paths) == {
        "packages/core",
        "packages/agent",
        "packages/collector",
        "packages/media",
    }
    assert all((ROOT / path).is_dir() for path in workspace_paths)
    for sdk in ("openai", "litellm", "dashscope", "google-genai", "twelvelabs"):
        assert not any(line.strip().lower().startswith(sdk) for line in requirements)

    for dockerfile in ("Dockerfile", "Dockerfile.gpu", "Dockerfile.claude"):
        contents = (ROOT / dockerfile).read_text(encoding="utf-8")
        assert contents.index("COPY requirements.txt ./") < contents.index("COPY packages ./packages")
        assert contents.index("COPY packages ./packages") < contents.index("-r requirements.txt")


def test_compose_builds_point_at_existing_dockerfiles():
    expected = {
        "docker-compose.yml": "Dockerfile",
        "docker-compose.gpu.yml": "Dockerfile.gpu",
        "docker-compose.claude.yml": "Dockerfile.claude",
    }
    for compose_file, dockerfile in expected.items():
        compose = yaml.safe_load((ROOT / compose_file).read_text(encoding="utf-8"))
        assert compose["services"]
        for service in compose["services"].values():
            build = service["build"]
            assert build["dockerfile"] == dockerfile
            assert (ROOT / dockerfile).is_file()


def test_claude_compose_explicitly_enables_upstream_compatibility():
    compose = yaml.safe_load((ROOT / "docker-compose.claude.yml").read_text(encoding="utf-8"))
    for service in compose["services"].values():
        assert service["environment"]["MPT_UPSTREAM_COMPATIBILITY"] == "1"

    default = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for service in default["services"].values():
        assert service.get("environment", {}).get("MPT_UPSTREAM_COMPATIBILITY") != "1"
