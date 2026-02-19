# Agent Principles

These principles are **mandatory** for all agents orchestrated by Conductor.
Agents that violate these principles will have their tasks rejected.

---

## 1. Modular Architecture

- Every change must respect the existing module boundaries.
- New functionality belongs in a focused, single-responsibility module.
- Avoid god files — if a module exceeds ~300 lines, split it.
- Shared logic goes in utility modules, not duplicated across files.

## 2. 100% Unit Test Coverage

- Every new function, class, or method **must** have corresponding unit tests.
- Tests live in `tests/` mirroring the source structure.
- Coverage is measured per-PR — no new code ships without tests.
- Edge cases, error paths, and boundary conditions must be covered.

## 3. Model Separation by Role

Use different AI models for each phase to avoid self-reinforcing blind spots:

| Phase | Model | Rationale |
|-------|-------|-----------|
| **Writing code** | Primary model | Optimized for generation |
| **Writing tests** | Secondary model | Independent perspective catches assumptions |
| **Reviewing PRs** | Tertiary model | Fresh eyes for logic, security, and style |

Agents must **never** use the same model to both write and review the same code.

## 4. Small, Targeted PRs

- Each PR addresses **one concern** — a single bug fix, feature, or refactor.
- Target: < 200 lines changed, < 10 files modified.
- If a task is too large, break it into a pipeline of sequential PRs.
- PR title must clearly describe the single change being made.
- No "drive-by" fixes — unrelated changes go in separate PRs.
