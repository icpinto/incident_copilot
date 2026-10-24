# Indexing

Fast structural indexing stores file metadata, symbols, imports, routes, env refs, and service hints in SQLite.

Install `pip install -e ".[parsing]"` to enable Tree-sitter. It parses Python,
JavaScript/JSX, TypeScript, TSX, Go, Java, JSON, and YAML files with their respective
grammars. Parsers are loaded once per language in a spawned child process for each
indexing run; TSX uses its
own grammar while retaining `typescript` as the stored file language.

Function, method, class, and named type records come from the syntax tree, with
one-based start/end line numbers. Named JavaScript/TypeScript arrow functions and
function expressions are included. Recoverable syntax errors do not discard valid
symbols elsewhere in the file. JSON and YAML are parsed but produce no code symbols.
Point coordinates are read through tuple unpacking to avoid a reference-counting
bug in Tree-sitter 0.26.0's `row`/`column` properties, which can corrupt memory on
large line or column offsets. The upstream
[Point implementation](https://github.com/tree-sitter/py-tree-sitter/blob/v0.26.0/tree_sitter/binding/point.c)
returns borrowed references from these property getters.

If the runtime or a grammar cannot load, indexing falls back to Python's `ast` for
Python files and lightweight symbol extraction for other files. Grammar failures
are logged. A successfully parsed file with zero symbols does not trigger fallback.
Imports, environment references, and route hints still use text-based extraction.

Native parsing runs outside the API and Temporal worker processes. If the parser
crashes or takes longer than 30 seconds for a file, indexing logs the file path,
grammar, and child exit code or timeout, then uses fallback extraction for that
grammar for the rest of the run. Other grammars continue in a fresh parser process.
Parser startup is also limited to 30 seconds. The child is stopped when indexing
finishes, including when indexing raises an exception. Progress is logged every
100 files and when the snapshot is complete.

### Recovering from a worker exit during indexing

A worker exit with code 139 indicates a native segmentation fault. When this
happens immediately after `tree_sitter_available=True`, Tree-sitter is a likely
source, but a fault traceback is needed to identify the cause. Temporal's later
`no poller seen for task queue recently` message means the worker is no longer
polling; API health checks can still pass independently.

Rebuild and recreate the application containers to install the isolated parser:

```bash
docker compose -f docker-compose.dev.yml up -d --build api worker
docker compose -f docker-compose.dev.yml logs -f worker
```

An existing Temporal run can resume when its activity is retried (the indexing
activity has a ten-minute start-to-close timeout). Rerun an incident that has
already failed. Compose restarts an unexpectedly exited worker, and the Python
fault handler is enabled in the image so native faults produce tracebacks.

To bypass Tree-sitter entirely, set `IC_TREE_SITTER_ENABLED=false` in the Compose
`.env` file and recreate `api worker` with the command above. For host processes,
export this environment variable before starting the API or worker. Python AST
and lightweight extraction remain available; lightweight parsing has less precise
symbol coverage and ranges than Tree-sitter.

Correlation reads the stored symbol ranges to locate the enclosing function for a
stack-frame line and load that function's snippet. Candidate ranking and downstream
diagnosis consume these records without needing to parse the repository again.
