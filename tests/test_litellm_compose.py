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
    def render(*, variables=None, env_text=""):
        empty_env.write_text(env_text, encoding="utf-8")
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith(("IC_", "LITELLM_", "COMPOSE_"))}
        environment.update(variables or {})
        command = [DOCKER, "compose", "--env-file", str(empty_env),
                   "-f", str(ROOT / "docker-compose.dev.yml")]
        return subprocess.run(command + ["config", "--format", "json"], check=False, cwd=ROOT,
                              env=environment, capture_output=True, text=True, timeout=30)

    return render


def test_external_gateway_settings_reach_both_processes(compose):
    variables = {
        "LITELLM_MASTER_KEY": "sk-compose-test-key",
        "IC_LLM_PROVIDER": "openai_compatible",
        "IC_OPENAI_COMPATIBLE_BASE_URL": "https://gateway.example/v1",
        "IC_OPENAI_COMPATIBLE_MODEL": "incident-diagnosis",
        "IC_OPENAI_COMPATIBLE_API_KEY": "sk-test-virtual-key",
        "IC_GUARDRAILS_MODE": "observe",
        "IC_GUARDRAILS_TIMEOUT_SECONDS": "10",
    }
    result = compose(variables=variables)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for name in ("api", "worker"):
        environment = services[name]["environment"]
        assert all(environment[key] == value for key, value in variables.items() if key.startswith("IC_"))


def test_default_stack_routes_both_processes_to_gateway_and_waits_for_health(compose):
    result = compose(variables={"LITELLM_MASTER_KEY": "sk-compose-test-key"})
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
        assert config.guardrails_mode == "observe"
        client = get_client(config).raw
        assert config.llm_provider == "openai_compatible"
        assert client.base_url == "http://litellm:4000/v1"
        assert client.model == "incident-diagnosis"
        assert client.api_key == gateway["environment"]["LITELLM_MASTER_KEY"]
        assert service["depends_on"]["litellm"]["condition"] == "service_healthy"
        assert "temporal" in service["depends_on"]
        assert "LITELLM_API_KEY" not in service["environment"]


@pytest.mark.parametrize("variables", [{}, {"LITELLM_MASTER_KEY": ""}])
def test_main_stack_requires_nonempty_authentication_key(compose, variables):
    result = compose(variables=variables)
    assert result.returncode != 0
    assert "LITELLM_MASTER_KEY" in result.stderr


def test_repository_cache_is_shared_with_only_worker_write_access(compose):
    result = compose(variables={"LITELLM_MASTER_KEY": "sk-compose-test-key"})
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for name in ("worker", "api", "gitnexus"):
        service = services[name]
        assert service["environment"]["IC_REPOSITORY_CACHE_DIR"] == "/repository-cache"
        cache = next(volume for volume in service["volumes"] if volume["target"] == "/repository-cache")
        assert cache["source"] == "repository-cache"
        assert cache.get("read_only", False) is (name != "worker")


def test_example_env_routes_both_processes_with_matching_gateway_credentials(compose):
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    result = compose(env_text=example.replace("LITELLM_MASTER_KEY=", "LITELLM_MASTER_KEY=sk-example-key"))
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for name in ("api", "worker"):
        environment = services[name]["environment"]
        assert environment["IC_GUARDRAILS_MODE"] == "observe"
        assert environment["IC_LLM_PROVIDER"] == "openai_compatible"
        assert environment["IC_OPENAI_COMPATIBLE_BASE_URL"] == "http://litellm:4000/v1"
        assert environment["IC_OPENAI_COMPATIBLE_MODEL"] == "incident-diagnosis"
        assert environment["IC_OPENAI_COMPATIBLE_API_KEY"] == services["litellm"]["environment"]["LITELLM_MASTER_KEY"] == "sk-example-key"
