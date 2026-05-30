# Lattice Python

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Built with UV](https://img.shields.io/badge/built%20with-uv-purple.svg)](https://github.com/astral-sh/uv)

> **Token-efficient hybrid retrieval and freshness-aware memory for Claude Code.**
> **3 MCP tools. Hooks-first. Persistent background daemon. Local-only. Python port.**

`lattice-python` is the Python port of `lattice`, a hybrid retrieval and token-efficient memory plugin built to supercharge **Claude Code**. It indexes your local codebase using Abstract Syntax Tree (AST) structures, provides high-accuracy semantic search, automatically captures long command outputs, and strategically blocks redundant reads of large files to dramatically lower token usage.

---

## 🚀 Key Features

*   **Hybrid Retrieval Cascade**: Fuses Symbol Resolution, BM25 Keyword Search, Vector Semantic Search, and Graph-based Personalized PageRank (HippoRAG) in a multi-stage pipeline, reranking outputs with a high-performance cross-encoder.
*   **AST-Based Chunking & Graph Indexing**: Uses `tree-sitter` (supporting Python, Rust, Go, JavaScript, TypeScript) to parse code semantics, resolve import paths, and build a dependency graph of imports, calls, exports, and class hierarchies, enabling instant local Personalized PageRank (PPR) traversal.
*   **Automated Lifecycle Hooks**: Low-latency thin CLI clients communicate with a background daemon to intercept reads, auto-capture large tool results, and re-index modified files.
*   **Redundant Read Interception**: Detects when Claude Code or shell scripts try to read large, already-indexed files. Blocks the read to conserve tokens, directing the agent to use fast, cached retrieval tools instead.
*   **Freshness-Aware Scoring**: Decays note and code indices exponentially based on their source type (`code_index` is static, `human_note` decays slowly, `auto_capture` expires quickly), with full support for pinning critical files.
*   **Context Token Budgeting**: Automatically packs results into strict, user-defined token budgets, providing Base64 continuation tokens for pagination.

---

## 📦 Getting Started

### 1. Installation

Install `lattice` globally or within your environment using `uv`:

```bash
uv tool install lattice
```

### 2. Project Initialization

Initialize Lattice inside your target repository. This creates the `.lattice/` directory structure containing note directories and configuration:

```bash
cd your-project-repository
lattice init
```

*Tip: You may want to add `.lattice/` (excluding `.lattice/notes/` if you want to share custom facts across developers) to your project's `.gitignore`.*

### 3. Start the MCP Server Daemon

Start the FastMCP background daemon. This hosts both the stdio MCP server for Claude Code and the lightweight HTTP port for hooks:

```bash
lattice mcp-server
```

---

## 🛠️ MCP Tools

Lattice registers 3 core tools to your Claude Code interface:

### 1. `recall(query, budget_tokens, kind, path_scope, since, continuation_token)`
Search the codebase and persistent notes with token budgets.
*   `query`: Natural-language search string or exact symbol identifier.
*   `budget_tokens`: Strict limit on returned tokens (default `2500`).
*   `kind`: Filter search scope: `'code'`, `'notes'`, or `'auto'`.
*   `path_scope`: Restricts search to a specific directory path prefix.
*   `continuation_token`: Base64 pagination token to fetch the next set of results.

### 2. `recall_expand(chunk_id, mode, budget_tokens, offset)`
Expand a code chunk or traverse its relationships in the dependency graph.
*   `chunk_id`: The 16-character identifier of the target chunk.
*   `mode`:
    *   `'body'`: Returns the complete source code or note text of the chunk alongside adjacent sibling previews.
    *   `'callers'`: Finds other functions, classes, or files invoking the symbol.
    *   `'imports'`: Lists dependencies imported by this chunk.
    *   `'dependents'`: Finds other modules importing this chunk.
*   `budget_tokens`: Token ceiling for expansion (default `1500`).

### 3. `write(heading, body, tags, supersedes, source, pinned)`
Persist a new fact, architectural choice, or project rule.
*   `heading`: Title of the note (max 120 chars).
*   `body`: Content of the note (max 4000 chars).
*   `tags`: List of descriptive string tags.
*   `supersedes`: Optional `chunk_id` of a past decision this note replaces.
*   `pinned`: Set `true` to bypass freshness scoring decay.

*Note: Custom writeups are saved as markdown files with YAML frontmatter in `.lattice/notes/{chunk_id}.md` (your human source of truth) and synchronized into the SQLite index database.*

---

## ⚓ Lifecycle Hooks

Lattice integrates directly into Claude Code's terminal workflow via the following CLI hooks:

*   **`lattice hook session-start`**: Summarizes the repository's status, index count, and displays the contents of `.lattice/notes/_summary.md` (the global project description) to Claude.
*   **`lattice hook pre-tool-use`**:
    *   Intercepts the `Read` tool and `Bash(cat ...)` commands. If a requested file is already indexed and fresh, it denies the execution and suggests using `recall_expand(chunk_id)` to save token space.
    *   Recommends `recall` when running `Grep`/`Glob` queries that match existing database chunks.
*   **`lattice hook post-tool-use`**:
    *   **Auto-indexing**: Automatically re-parses and indices changed files immediately after an `Edit`, `Write`, or `MultiEdit` command.
    *   **Auto-capture**: Captures output from `Read`/`Grep`/`Bash` tools that exceed 1000 tokens, storing them as searchable chunks so you don't waste tokens reading them again.
*   **`lattice hook pre-compact`**: Executes database optimization and vacuum tasks.
*   **`lattice hook stop`**: Cleans up temporary resources and runs general garbage collection.

---

## 💻 Command Line Interface (CLI)

```bash
# General help
lattice --help

# Initialize Lattice storage in the current directory
lattice init

# Start the background FastMCP daemon
lattice mcp-server

# Verify database integrity, tree-sitter grammars, and hook health
lattice doctor

# Re-read all markdown notes and source files to rebuild the SQLite index
lattice rebuild

# Scan chunks for overlapping symbols or duplicate definitions
lattice audit

# Import persistent memories from basic-memory, memsearch, or claude-md
lattice import --from memsearch
```

---

## 🛠️ Development

To set up a local development environment:

```bash
# Clone the repository
git clone https://github.com/your-username/lattice-python.git
cd lattice-python

# Create virtual environment and install dependencies
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Run tests
uv run pytest
```

---

## 🏛️ Architecture & Deep-Dive

For a complete breakdown of the system components, database schemas, AST parsers, RRF fusion formulas, and lazy loading strategies, see [architecture.md](docs/architecture.md).
