import logging
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from incident_copilot.schemas.index import SymbolRecord

if TYPE_CHECKING:
    from tree_sitter import Node, Parser, Tree

logger = logging.getLogger(__name__)

_GRAMMARS = {
    "python": ("tree_sitter_python", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "json": ("tree_sitter_json", "language"),
    "yaml": ("tree_sitter_yaml", "language"),
}

_SYMBOL_KINDS = {
    "function_definition": "function",
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "function_expression": "function",
    "generator_function": "function",
    "arrow_function": "function",
    "method_definition": "function",
    "method_declaration": "function",
    "constructor_declaration": "function",
    "class_definition": "class",
    "class_declaration": "class",
    "class": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "class",
    "type_spec": "type",
}


def _symbol_name(node: "Node") -> "Node | None":
    name = node.child_by_field_name("name")
    if name is not None:
        return name
    # Give assigned JS/TS functions their variable/property name; skip anonymous callbacks.
    parent = node.parent
    if parent is not None and node.type in {
        "arrow_function",
        "function_expression",
        "generator_function",
        "class",
    }:
        field = {
            "variable_declarator": "name",
            "pair": "key",
            "assignment_expression": "left",
            "public_field_definition": "name",
            "field_definition": "property",
        }.get(parent.type)
        if field:
            return parent.child_by_field_name(field)
    return None


class TreeSitterFacade:
    """Parse with installed grammars, returning None when a fallback is needed."""

    def __init__(self) -> None:
        self._Language = None
        self._Parser = None
        self._parsers: dict[str, Parser | None] = {}
        try:
            from tree_sitter import Language as TSLanguage
            from tree_sitter import Parser as TSParser

            self._Language = TSLanguage
            self._Parser = TSParser
        except ImportError:
            logger.info("Tree-sitter is unavailable; using fallback symbol extraction")

    @property
    def available(self) -> bool:
        return self._Parser is not None

    def parse(self, source: str, language_name: str) -> "Tree | None":
        if self._Parser is None or self._Language is None or language_name not in _GRAMMARS:
            return None
        if language_name not in self._parsers:
            module_name, factory = _GRAMMARS[language_name]
            try:
                grammar = getattr(import_module(module_name), factory)()
                self._parsers[language_name] = self._Parser(self._Language(grammar))
            except (ImportError, OSError, AttributeError, TypeError, ValueError):
                logger.warning(
                    "Tree-sitter grammar unavailable for %s; using fallback", language_name
                )
                self._parsers[language_name] = None
        parser = self._parsers[language_name]
        if parser is None:
            return None
        try:
            return parser.parse(source.encode("utf-8"))
        except (RuntimeError, ValueError):
            logger.warning("Tree-sitter parsing failed for %s; using fallback", language_name)
            self._parsers[language_name] = None
            return None

    def extract_symbols(
        self,
        path: Path,
        source: str,
        language_name: str,
    ) -> list[SymbolRecord] | None:
        tree = self.parse(source, language_name)
        if tree is None:
            return None
        if tree.root_node.has_error:
            logger.debug("Tree-sitter recovered syntax errors in %s", path)
        source_bytes = source.encode("utf-8")
        symbols = []
        pending = [tree.root_node]
        while pending:
            node = pending.pop()
            kind = _SYMBOL_KINDS.get(node.type)
            if kind:
                name = _symbol_name(node)
                if name is not None:
                    # Unpack Points as tuples. The 0.26.0 row/column accessors return
                    # borrowed references, corrupting integers beyond CPython's small-int cache.
                    start_row, _ = node.start_point
                    end_row, end_column = node.end_point
                    start = start_row + 1
                    # Tree-sitter end positions are exclusive, including at column zero.
                    end = end_row + (end_column > 0)
                    symbols.append(
                        SymbolRecord(
                            name=source_bytes[name.start_byte : name.end_byte].decode("utf-8"),
                            kind=kind,
                            line_start=start,
                            line_end=max(start, end),
                            file_path=str(path),
                        )
                    )
            pending.extend(reversed(node.named_children))
        return symbols
