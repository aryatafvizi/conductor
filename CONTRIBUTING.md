# Contributing to Conductor

Thank you for your interest in contributing to Conductor! This guide will help you get started.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/aryatafvizi/conductor.git
cd conductor

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install in development mode
pip install -e ".[dev]"

# Run tests
pytest
```

## Project Structure

```
conductor/
├── conductor/           # Core package
│   ├── __init__.py
│   ├── models.py        # Dataclasses for all entities
│   ├── db.py            # SQLite database layer
│   ├── logger.py        # 3-layer structured logging
│   ├── task_manager.py  # Task state machine
│   ├── workspace_manager.py
│   ├── agent_manager.py # Gemini agent lifecycle
│   ├── quota_manager.py # Rate limit tracking
│   ├── guardrails.py    # Safety enforcement
│   ├── rules_engine.py  # YAML trigger/action rules
│   ├── github_monitor.py# GitHub polling via gh CLI
│   ├── pr_lifecycle.py  # PR automation pipeline
│   ├── planner.py       # Conversational planning
│   ├── server.py        # FastAPI + WebSocket server
│   └── cli.py           # CLI entrypoint
├── static/              # Dashboard frontend
├── tests/               # Test suite
├── docs/                # Documentation
├── pyproject.toml       # Package configuration
└── README.md
```

## How to Contribute

### Reporting Bugs

1. Check [existing issues](https://github.com/aryatafvizi/conductor/issues) first
2. Include your Python version, OS, and Conductor version
3. Provide steps to reproduce the issue
4. Include relevant log output (`con logs --level ERROR`)

### Suggesting Features

Open an issue with the `enhancement` label. Include:
- **Use case** — What problem does this solve?
- **Proposed solution** — How should it work?
- **Alternatives** — What other approaches did you consider?

### Pull Requests

1. Fork the repo and create your branch from `main`
2. Write or update tests for your changes
3. Ensure tests pass: `pytest`
4. Run linting: `ruff check .`
5. Format code: `ruff format .`
6. Update documentation if needed
7. Open a PR with a clear description

### Code Style

- Python 3.10+ with type hints
- Follow [PEP 8](https://peps.python.org/pep-0008/) conventions
- Use `ruff` for linting and formatting
- Docstrings on all public functions
- Keep modules focused — one concern per file

### Commit Messages

Use clear, descriptive commit messages:

```
feat: add batch workspace rollback command
fix: resolve tilde expansion in workspace discovery
docs: update CLI reference with new log commands
test: add quota manager reset tests
```

## Architecture Guidelines

- **Modules are loosely coupled** — each manager has a clear responsibility
- **SQLite for persistence** — no external database required
- **Async where needed** — agent spawning and GitHub polling are async
- **CLI is a thin layer** — all logic lives in the managers
- **Dashboard is optional** — CLI works independently

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=conductor

# Run specific test file
pytest tests/test_task_manager.py

# Run with verbose output
pytest -v
```

## Questions?

Open an issue or start a discussion. We're happy to help!
