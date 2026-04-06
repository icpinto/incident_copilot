from pathlib import Path
from typing import List

import yaml

from incident_copilot.schemas.index import ServiceMapping


def parse_compose_services(compose_path: Path | None) -> List[ServiceMapping]:
    if not compose_path or not compose_path.exists():
        return []
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8", errors="ignore")) or {}
    services = data.get("services", {}) or {}
    mappings: List[ServiceMapping] = []
    for name, cfg in services.items():
        build = cfg.get("build")
        build_context = None
        dockerfile = None
        if isinstance(build, str):
            build_context = build
        elif isinstance(build, dict):
            build_context = build.get("context")
            dockerfile = build.get("dockerfile")
        env_files = cfg.get("env_file", [])
        if isinstance(env_files, str):
            env_files = [env_files]
        mappings.append(
            ServiceMapping(
                service_name=name,
                build_context=build_context,
                dockerfile=dockerfile,
                env_files=env_files,
                command=cfg.get("command"),
                image=cfg.get("image"),
            )
        )
    return mappings
