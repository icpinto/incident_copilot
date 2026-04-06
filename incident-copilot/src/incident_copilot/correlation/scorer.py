from incident_copilot.schemas.index import FileRecord
from incident_copilot.schemas.logs import ParsedLog


def score_file(record: FileRecord, parsed_log: ParsedLog) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    path_lower = record.file_path.lower()

    for service in parsed_log.services:
        if service.lower() in path_lower or service in record.service_hints:
            score += 8.0
            reasons.append(f"service match: {service}")

    for frame in parsed_log.stack_frames:
        if frame.file_path and frame.file_path.split("/")[-1].lower() in path_lower:
            score += 10.0
            reasons.append(f"stack frame file match: {frame.file_path}")
        if frame.function_name and any(s.name == frame.function_name for s in record.symbols):
            score += 12.0
            reasons.append(f"stack frame function match: {frame.function_name}")

    for exc in parsed_log.exception_types:
        if exc.lower().replace("error", "") in path_lower:
            score += 1.5
            reasons.append(f"exception lexical hint: {exc}")

    keywords = [k.lower() for k in parsed_log.keywords]
    symbol_names = [s.name.lower() for s in record.symbols]
    imports = [i.lower() for i in record.imports]
    env_refs = [e.lower() for e in record.env_refs]
    for kw in keywords:
        if kw in path_lower:
            score += 1.2
            reasons.append(f"path keyword match: {kw}")
        if kw in symbol_names:
            score += 2.2
            reasons.append(f"symbol keyword match: {kw}")
        if kw in imports:
            score += 0.8
            reasons.append(f"import keyword match: {kw}")
        if kw in env_refs:
            score += 1.5
            reasons.append(f"env ref keyword match: {kw}")

    for entity in parsed_log.tables_or_entities:
        if entity in path_lower:
            score += 2.0
            reasons.append(f"entity/path match: {entity}")
        if entity.lower() in symbol_names:
            score += 2.5
            reasons.append(f"entity/symbol match: {entity}")

    return score, reasons
