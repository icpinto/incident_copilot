from pathlib import Path
from typing import Optional


class TreeSitterFacade:
    """
    Optional Tree-sitter integration. If unavailable, callers should fall back
    to lightweight parsing.
    """

    def __init__(self) -> None:
        self._available = False
        self._get_language = None
        self._Parser = None
        try:
            from tree_sitter import Parser  # type: ignore
            from tree_sitter_languages import get_language  # type: ignore

            self._Parser = Parser
            self._get_language = get_language
            self._available = True
        except Exception:
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def parse(self, source: str, language_name: str):
        if not self._available or self._Parser is None or self._get_language is None:
            return None
        parser = self._Parser()
        parser.set_language(self._get_language(language_name))
        return parser.parse(source.encode("utf-8"))
