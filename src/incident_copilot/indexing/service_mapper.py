from pathlib import Path
from typing import List

from incident_copilot.schemas.index import ServiceMapping
from incident_copilot.tools.compose_parser import parse_compose_services


def build_service_map(repo_root: Path, compose_path: Path | None) -> List[ServiceMapping]:
    mappings = parse_compose_services(compose_path)
    # Normalize relative build contexts from compose file to repo-relative strings.
    normalized: List[ServiceMapping] = []
    for m in mappings:
        build_context = m.build_context
        dockerfile = m.dockerfile
        if build_context and compose_path:
            bc = (compose_path.parent / build_context).resolve()
            try:
                build_context = str(bc.relative_to(repo_root.resolve()))
            except Exception:
                build_context = str(bc)
        if dockerfile and build_context:
            df = Path(build_context) / dockerfile
            dockerfile = str(df)
        normalized.append(
            ServiceMapping(
                service_name=m.service_name,
                build_context=build_context,
                dockerfile=dockerfile,
                env_files=m.env_files,
                command=m.command,
                image=m.image,
            )
        )
    return normalized
