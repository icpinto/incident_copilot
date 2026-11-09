"""Compose validation does not require a running Docker daemon."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_gateway_observability_config_preserves_inference_contract():
    baseline = yaml.safe_load((ROOT / "configs/litellm.yaml").read_text())
    observed = yaml.safe_load((ROOT / "configs/litellm.langfuse.yaml").read_text())
    assert observed["litellm_settings"].pop("callbacks") == ["langfuse_otel"]
    assert observed == baseline
    assert observed["litellm_settings"]["turn_off_message_logging"] is True


def test_overlay_services_isolate_storage_and_keep_export_optional(tmp_path):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is not installed")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("IC_", "LANGFUSE_", "LITELLM_", "COMPOSE_"))}
    for name in ("PUBLIC_KEY", "SECRET_KEY", "POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_PASSWORD",
                 "MINIO_PASSWORD", "SALT", "ENCRYPTION_KEY", "NEXTAUTH_SECRET", "INIT_USER_EMAIL", "INIT_USER_PASSWORD"):
        environment["LANGFUSE_" + name] = "compose-test-value"
    environment["LITELLM_MASTER_KEY"] = "sk-compose-test"
    empty = tmp_path / "empty.env"
    empty.write_text("")
    result = subprocess.run([docker, "--config", str(tmp_path / "docker"), "compose", "--env-file", str(empty),
                             "-f", str(ROOT / "docker-compose.dev.yml"),
                             "-f", str(ROOT / "docker-compose.langfuse.yml"), "config", "--format", "json"],
                            cwd=ROOT, env=environment, check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert services["langfuse-web"]["image"].endswith(":4.76.0")
    assert services["langfuse-worker"]["image"].endswith(":4.76.0")
    assert services["langfuse-web"]["ports"][0]["host_ip"] == "127.0.0.1"
    for name in ("langfuse-worker", "langfuse-postgres", "langfuse-clickhouse", "langfuse-redis", "langfuse-minio"):
        assert not services[name].get("ports")
    assert services["postgres"]["image"] == "postgres:13"
    assert services["langfuse-postgres"]["image"].startswith("postgres:17.")
    for name in ("api", "worker"):
        env = services[name]["environment"]
        assert env["IC_LANGFUSE_ENABLED"] == "true"
        assert env["IC_LANGFUSE_CAPTURE_CONTENT"] == "false"
        assert "langfuse-web" not in services[name].get("depends_on", {})
    gateway = services["litellm"]
    assert "langfuse-web" not in gateway.get("depends_on", {})
    assert gateway["environment"]["OTEL_IGNORE_CONTEXT_PROPAGATION"] == "false"
    assert gateway["environment"]["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "no_content"
    assert len([v for v in gateway["volumes"] if v["target"] == "/app/config.yaml"]) == 1


def test_bootstrap_never_overwrites_or_prints_secrets(tmp_path):
    import sys
    output = tmp_path / "credentials.env"
    command = [sys.executable, str(ROOT / "scripts/setup_langfuse.py"), "--output", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    original = output.read_text()
    for line in original.splitlines():
        if "SECRET" in line or "PASSWORD" in line:
            assert line.split("=", 1)[1] not in result.stdout
    repeated = subprocess.run(command, capture_output=True, text=True, check=False)
    assert repeated.returncode != 0
    assert output.read_text() == original
