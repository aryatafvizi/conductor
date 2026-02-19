# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-02-19

### Added

- **Core**: Task management with priority queue, dependency resolution, and state machine
- **Core**: Workspace auto-discovery, assignment, git snapshots, and one-click rollback
- **Core**: Agent lifecycle management — spawn, monitor, and kill Gemini agents
- **Core**: 3-layer structured logging (system log, session logs, summary log)
- **Core**: SQLite persistence with WAL mode for concurrent access
- **Safety**: Branch protection, filesystem sandbox, output scanning, diff limits
- **Safety**: Auto-rollback on agent failure
- **Safety**: Emergency kill switch (CLI + dashboard)
- **Quota**: Google AI Ultra quota tracking (200 agents/day, 1500 prompts/day, 3 concurrent)
- **Quota**: Auto-pause at configurable threshold, auto-resume at midnight PT
- **PR Lifecycle**: Full automation — coding → prechecks → PR → CI → Greptile → merge
- **PR Lifecycle**: Auto-fix CI failures, auto-address Greptile review comments
- **Rules**: YAML-defined automation rules with regex pattern matching
- **GitHub**: PR/CI status polling, review comment detection, PR creation via `gh` CLI
- **Planning**: Conversational task design via Gemini with structured plan extraction
- **CLI**: 17 commands — `start`, `plan`, `add`, `list`, `kill`, `quota`, `batch`, `logs`, etc.
- **Dashboard**: Real-time dark-themed web UI with WebSocket updates
- **Dashboard**: Quota bars, task tracker, agent status, workspace health, PR pipeline, logs
- **Config**: YAML configuration for all settings (`~/.conductor/config.yaml`)
