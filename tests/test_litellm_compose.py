"""Validate the deployed client/gateway contract using Compose without a daemon."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from incident_copilot.core.settings import Settings
from incident_copilot.llm.client import get_client

ROOT = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker")
pytestmark = pytest.mark.skipif(DOCKER is None, reason="Docker Compose CLI is not installed")


@pytest.fixture
def compose(tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")

    def render(*, gateway=False, variables=None):
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith(("IC_", "LITELLM_", "COMPOSE_"))}
        environment.update(variables or {})
        command = [DOCKER, "compose", "--env-file", str(empty_env),
                   "-f", str(ROOT / "docker-compose.dev.yml")]
        if gateway:
            command.extend(["-f", str(ROOT / "docker-compose.litellm.yml")])
        return subprocess.run(command + ["config", "--format", "json"], check=False, cwd=ROOT,
                              env=environment, capture_output=True, text=True, timeout=30)

    return render


def test_default_stack_still_uses_direct_ollama_without_gateway_key(compose):
    result = compose()
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert "litellm" not in services
    for name in ("api", "worker"):
        assert services[name]["environment"]["IC_LLM_PROVIDER"] == "ollama"


def test_external_gateway_settings_reach_both_processes(compose):
    variables = {
        "IC_LLM_PROVIDER": "openai_compatible",
        "IC_OPENAI_COMPATIBLE_BASE_URL": "https://gateway.example/v1",
        "IC_OPENAI_COMPATIBLE_MODEL": "incident-diagnosis",
        "IC_OPENAI_COMPATIBLE_API_KEY": "sk-test-virtual-key",
    }
    result = compose(variables=variables)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for name in ("api", "worker"):
        environment = services[name]["environment"]
        assert all(environment[key] == value for key, value in variables.items())


def test_bundled_gateway_routes_both_processes_to_alias_and_waits_for_health(compose):
    result = compose(gateway=True, variables={"LITELLM_MASTER_KEY": "sk-compose-test-key"})
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    gateway = services["litellm"]
    assert gateway["ports"][0]["host_ip"] == "127.0.0.1"
    assert gateway["environment"]["LITELLM_MODEL"] == "ollama_chat/llama3.1:8b"
    assert gateway["volumes"][0]["read_only"] is True
    for name in ("api", "worker"):
        service = services[name]
        config = Settings(_env_file=None, **{
            key.removeprefix("IC_").lower(): value
            for key, value in service["environment"].items() if key.startswith("IC_")
        })
        client = get_client(config)
        assert client.base_url == "http://litellm:4000/v1"
        assert client.model == "incident-diagnosis"
        assert client.api_key == gateway["environment"]["LITELLM_MASTER_KEY"]
        assert service["depends_on"]["litellm"]["condition"] == "service_healthy"
        assert "temporal" in service["depends_on"]
        assert "LITELLM_API_KEY" not in service["environment"]


@pytest.mark.parametrize("variables", [{}, {"LITELLM_MASTER_KEY": ""}])
def test_gateway_overlay_requires_nonempty_authentication_key(compose, variables):
    result = compose(gateway=True, variables=variables)
    assert result.returncode != 0
    assert "LITELLM_MASTER_KEY" in result.stderr
