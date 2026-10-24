# Indexing

Fast structural indexing stores file metadata, symbols, imports, routes, env refs, and service hints in SQLite.

Install `pip install -e ".[parsing]"` to enable Tree-sitter. It parses Python,
JavaScript/JSX, TypeScript, TSX, Go, Java, JSON, and YAML files with their respective
grammars. Parsers are loaded once per language for each indexing run; TSX uses its
own grammar while retaining `typescript` as the stored file language.

Function, method, class, and named type records come from the syntax tree, with
one-based start/end line numbers. Named JavaScript/TypeScript arrow functions and
function expressions are included. Recoverable syntax errors do not discard valid
symbols elsewhere in the file. JSON and YAML are parsed but produce no code symbols.

If the runtime or a grammar cannot load, indexing falls back to Python's `ast` for
Python files and lightweight symbol extraction for other files. Grammar failures
are logged. A successfully parsed file with zero symbols does not trigger fallback.
Imports, environment references, and route hints still use text-based extraction.

Correlation reads the stored symbol ranges to locate the enclosing function for a
stack-frame line and load that function's snippet. Candidate ranking and downstream
diagnosis consume these records without needing to parse the repository again.
