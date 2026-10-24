import sys
from pathlib import Path

import pytest

from incident_copilot.correlation.candidate_ranker import rank_candidates
from incident_copilot.indexing import isolated_parser, repo_indexer, treesitter_indexer
from incident_copilot.indexing.isolated_parser import IsolatedTreeSitterFacade
from incident_copilot.indexing.storage import IndexStorage
from incident_copilot.indexing.treesitter_indexer import TreeSitterFacade


def _unavailable_worker(connection):
    connection.send(False)
    connection.close()


@pytest.fixture
def parsing_dependencies():
    for module in (
        "tree_sitter",
        "tree_sitter_python",
        "tree_sitter_javascript",
        "tree_sitter_typescript",
        "tree_sitter_go",
        "tree_sitter_java",
        "tree_sitter_json",
        "tree_sitter_yaml",
    ):
        pytest.importorskip(module)


@pytest.mark.parametrize(
    "suffix,grammar,source,expected",
    [
        (
            ".py",
            "python",
            (
                "class Gateway:\n"
                "    async def charge(self):\n"
                "        def inner():\n"
                "            return 1\n"
                "        return inner()\n"
            ),
            [("Gateway", "class", 1, 5), ("charge", "function", 2, 5), ("inner", "function", 3, 4)],
        ),
        (
            ".js",
            "javascript",
            (
                "export const charge = async () => {\n  return 1;\n};\n"
                "class Gateway {\n  refund() {\n    return 2;\n  }\n}\n"
            ),
            [
                ("charge", "function", 1, 3),
                ("Gateway", "class", 4, 8),
                ("refund", "function", 5, 7),
            ],
        ),
        (
            ".jsx",
            "javascript",
            "export const Receipt = () => (\n  <div>Paid</div>\n);\n",
            [("Receipt", "function", 1, 3)],
        ),
        (
            ".ts",
            "typescript",
            "export const charge = (amount: number): number => {\n  return amount;\n};\n",
            [("charge", "function", 1, 3)],
        ),
        (
            ".tsx",
            "tsx",
            "export const Receipt = () => (\n  <div>Paid</div>\n);\n",
            [("Receipt", "function", 1, 3)],
        ),
        (
            ".go",
            "go",
            (
                'package payments\n\nfunc charge() {\n  panic("failed")\n}\n'
                "type Gateway struct {}\nfunc (g Gateway) refund() {}\n"
            ),
            [("charge", "function", 3, 5), ("Gateway", "type", 6, 6), ("refund", "function", 7, 7)],
        ),
        (
            ".java",
            "java",
            (
                "public class Gateway {\n  public Gateway() {}\n  public void charge() {\n"
                "    throw new IllegalStateException();\n  }\n}\n"
            ),
            [
                ("Gateway", "class", 1, 6),
                ("Gateway", "function", 2, 2),
                ("charge", "function", 3, 5),
            ],
        ),
        (".json", "json", '{"message": "def fake():"}\n', []),
        (".yaml", "yaml", "message: |\n  def fake():\n    pass\n", []),
        (".yml", "yaml", "message: |\n  def fake():\n    pass\n", []),
    ],
)
def test_repository_indexes_real_syntax_trees(
    tmp_path,
    monkeypatch,
    parsing_dependencies,
    suffix,
    grammar,
    source,
    expected,
):
    root = tmp_path / "repo"
    root.mkdir()
    path = root / f"source{suffix}"
    path.write_text(source, encoding="utf-8")
    tree = TreeSitterFacade().parse(source, grammar)
    assert tree is not None
    assert not tree.root_node.has_error

    def unexpected_fallback(*args):
        pytest.fail("A working Tree-sitter grammar must supply the indexed symbols")

    monkeypatch.setattr(repo_indexer, "extract_python_symbols", unexpected_fallback)
    monkeypatch.setattr(repo_indexer, "extract_lightweight_symbols", unexpected_fallback)
    db_path = tmp_path / "index.db"
    summary = repo_indexer.index_repository(root, None, db_path)
    record = IndexStorage(db_path).get_file_record(path.name)

    assert summary.indexed_files == 1
    assert summary.indexed_symbols == len(expected)
    assert [(s.name, s.kind, s.line_start, s.line_end) for s in record.symbols] == expected


def test_typescript_tree_symbols_localize_the_later_candidate(tmp_path, parsing_dependencies):
    root = tmp_path / "repo"
    path = root / "services" / "payments" / "handlers.ts"
    path.parent.mkdir(parents=True)
    path.write_text(
        "export function unrelated() {\n  return 0;\n}\n"
        "export const charge = () => {\n  throw new Error('payment failed');\n};\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "index.db"
    repo_indexer.index_repository(root, None, db_path)
    candidates, _ = rank_candidates(
        root,
        db_path,
        "payments | Error: payment failed\nat /app/services/payments/handlers.ts:5:3",
    )
    top = candidates.items[0]
    assert top.symbol_name == "charge"
    assert (top.line_start, top.line_end) == (4, 6)
    assert "throw new Error" in top.snippet
    assert "unrelated" not in top.snippet


def test_missing_runtime_preserves_python_ast_fallback(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "tree_sitter", None)
    assert not TreeSitterFacade().available
    monkeypatch.setattr(isolated_parser, "_parse_worker", _unavailable_worker)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("def charge():\n    return 1\n", encoding="utf-8")
    db_path = tmp_path / "index.db"
    repo_indexer.index_repository(root, None, db_path)
    record = IndexStorage(db_path).get_file_record("main.py")
    assert [(s.name, s.line_start, s.line_end) for s in record.symbols] == [("charge", 1, 2)]


@pytest.mark.parametrize("failure", [ImportError, TypeError, ValueError, OSError])
def test_unusable_grammar_is_cached_and_other_languages_still_parse(
    monkeypatch,
    parsing_dependencies,
    failure,
):
    original_import = treesitter_indexer.import_module
    attempts = []

    def import_grammar(module):
        if module == "tree_sitter_typescript":
            attempts.append(module)
            raise failure("unavailable grammar")
        return original_import(module)

    monkeypatch.setattr(treesitter_indexer, "import_module", import_grammar)
    parser = TreeSitterFacade()
    assert parser.extract_symbols(Path("one.ts"), "const x = 1;", "typescript") is None
    assert parser.extract_symbols(Path("two.ts"), "const x = 2;", "typescript") is None
    symbols = parser.extract_symbols(Path("main.py"), "def charge():\n    pass\n", "python")
    assert [s.name for s in symbols] == ["charge"]
    assert len(attempts) == 1


def test_unicode_names_and_recovered_python_syntax_errors(parsing_dependencies):
    source = "# café\ndef café():\n    return 1\n\ndef broken(\n"
    symbols = TreeSitterFacade().extract_symbols(Path("main.py"), source, "python")
    assert [(s.name, s.line_start, s.line_end) for s in symbols] == [("café", 2, 3)]


@pytest.mark.parametrize("grammar", ["python", "tsx"])
def test_large_line_and_column_offsets_do_not_corrupt_native_points(grammar, parsing_dependencies):
    if grammar == "python":
        path = Path("large.py")
        source = "\n" * 300 + "".join(
            f"def function_{i}():\n    return {i}\n\n" for i in range(200)
        )
        expected = [(f"function_{i}", 301 + 3 * i, 302 + 3 * i) for i in range(200)]
    else:
        path = Path("wide.tsx")
        source = "\n" * 300 + "".join(
            f"export const component{i} = () => <div>{'x' * 400}</div>;\n" for i in range(200)
        )
        expected = [(f"component{i}", 301 + i, 301 + i) for i in range(200)]

    # A regression must fail this assertion without taking down the test runner.
    with IsolatedTreeSitterFacade(timeout_seconds=2) as parser:
        for _ in range(3):
            symbols = parser.extract_symbols(path, source, grammar)
            assert symbols is not None
            assert [(s.name, s.line_start, s.line_end) for s in symbols] == expected
