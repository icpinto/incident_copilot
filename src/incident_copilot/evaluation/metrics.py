def top_k_hit(actual_file: str, ranked_files: list[str], k: int = 5) -> bool:
    return actual_file in ranked_files[:k]
