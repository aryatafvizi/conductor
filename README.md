<p align="center">
  <img src="docs/logo.svg" width="120" alt="Conductor Logo" />
</p>

<h1 align="center">Conductor</h1>

<p align="center">
  <strong>AI-powered agent orchestration for automated software development</strong>
</p>

<p align="center">
  <a href="#features">Features</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#cli-reference">CLI</a> •
  <a href="#dashboard">Dashboard</a> •
  <a href="#configuration">Configuration</a> •
  <a href="#contributing">Contributing</a>
</p>

---

Conductor is a lightweight CLI + dashboard that orchestrates AI coding agents across multiple Git workspaces. It manages the full PR lifecycle — from planning to merge — with built-in safety guardrails, quota management, and real-time monitoring.

Think of it as a **CI/CD system for AI agents**: you define tasks, Conductor assigns them to workspaces, spawns agents, monitors output, enforces guardrails, and automates the review cycle.

## Features

### 🤖 Agent Orchestration
- Spawn and monitor [Gemini](https://ai.google.dev/gemini-api/docs/ai-studio-quickstart) agents across isolated workspaces
- Real-time output streaming via WebSocket
- Automatic workspace assignment and cleanup
- Concurrent agent management with configurable limits

### 🔄 Full PR Lifecycle Automation
- **Plan** → **Code** → **Precheck** → **PR** → **CI** → **Review** → **Merge**
- Auto-fix CI failures by analyzing logs and creating fix tasks
- Greptile review integration with automatic comment resolution
- Configurable iteration limits with human escalation

### 🛡️ Safety Guardrails
- **Branch protection** — block writes to `main`, `master`, `release/*`
- **Filesystem sandbox** — prevent access to `~/.ssh`, `~/.env`, etc.
- **Output scanning** — detect and kill agents attempting `rm -rf`, force push, pipe-to-shell
- **Diff limits** — block commits exceeding file/line thresholds
- **Auto-rollback** — restore workspace from snapshot on failure
- **Kill switch** — emergency stop all agents instantly

### 📊 Quota Management
- Track daily agent requests, prompts, and concurrent agents
- Auto-pause at configurable threshold (default 90%)
- Auto-resume after midnight PT daily reset
- Reserve requests for human-initiated tasks

### 📋 Task Management
- Priority queue with dependency resolution
- State machine: `pending` → `blocked` → `ready` → `running` → `done`/`failed`
- Automatic retry with configurable limits
- Batch operations across all workspaces

### 📈 Real-time Dashboard
- Dark-themed web UI with live WebSocket updates
- Quota bars, task tracker, agent status, workspace health
- PR lifecycle pipeline visualization
- Interactive planning chat
- Filterable log viewer

### 📜 Structured Logging
- **System log** — rotating JSON for Conductor internals
- **Session logs** — per-task capture of prompts, output, diffs, commands
- **Summary log** — one-line JSONL per task for analytics

### 🔧 Rules Engine
- YAML-defined triggers (CI failure, review comment, PR event)
- Regex pattern matching on event payloads
- Auto-create tasks from matching events
- Source filtering (e.g., Greptile vs. human reviews)

## Quick Start

### Prerequisites

- Python 3.10+
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) (authenticated)
- [GitHub CLI](https://cli.github.com/) (`gh`, authenticated)
- Git

### Install

```bash
# Clone the repo
git clone https://github.com/aryatafvizi/conductor.git
cd conductor

# Install dependencies
pip install -e .

# Initialize config
conductor init
```

This creates `~/.conductor/` with default `config.yaml` and `rules.yaml`.

### First Run

```bash
# Start the server + dashboard
con start

# Open dashboard
open http://localhost:4000

# Add your first task
con add "Fix login bug on PR #42" --branch fix/login-42 --priority high

# Interactive planning
con plan "I need to refactor the auth module to support OAuth2"
```

### Set Up Workspaces

Conductor discovers workspaces matching a glob pattern (default: `~/workspace-*`). Create multiple clones of your repo:

```bash
git clone git@github.com:you/your-repo.git ~/workspace-1
cp -r ~/workspace-1 ~/workspace-2
cp -r ~/workspace-1 ~/workspace-3
```

Each workspace is a fully independent Git clone that agents can operate in concurrently.

## CLI Reference

```
con <command> [options]
```

### Core Commands

| Command | Description |
|---------|-------------|
| `con start [--port 4000]` | Start server + dashboard |
| `con stop` | Stop server gracefully |
| `con plan [message]` | Interactive planning chat with Gemini |

### Task Management

| Command | Description |
|---------|-------------|
| `con add <title> [-b branch] [-p priority] [--depends-on ID...]` | Create a task |
| `con list [-s status]` | List tasks with status icons |
| `con done <id>` | Mark task complete |
| `con cancel <id>` | Cancel a task |

### Agent Control

| Command | Description |
|---------|-------------|
| `con agents` | Show running agents |
| `con kill [agent_id]` | Kill specific agent (or all if no ID) |

### Workspace Operations

| Command | Description |
|---------|-------------|
| `con rollback <workspace>` | Restore workspace from pre-task snapshot |
| `con batch <command> [--all]` | Run shell command across all workspaces |

### Monitoring

| Command | Description |
|---------|-------------|
| `con quota` | Show AI quota usage + reset timer |
| `con pr status` | Show PR lifecycle pipelines |
| `con logs [task_id] [--level LEVEL] [--search QUERY] [--tail N]` | View logs |
| `con logs-export [--last 7d]` | Export logs to JSON |
| `con rules list` | Show loaded automation rules |
| `con watch [--once]` | Poll GitHub for events |

### Examples

```bash
# Add a high-priority task on a feature branch
con add "Implement user authentication" --branch feature/auth --priority critical

# Add a task that depends on another
con add "Write auth tests" --depends-on 1

# Run git status across all workspaces
con batch "git status --short" --all

# Search error logs from the last 2 hours
con logs --level ERROR --since 2

# View session log for a specific task
con logs 42
```

## Dashboard

The real-time dashboard provides a complete view of your orchestration system:

| Panel | Contents |
|-------|----------|
| **Quota** | Agent/prompt usage bars, concurrent count, reset timer |
| **PR Lifecycle** | Stage pipeline (coding → CI → review → merge) |
| **Tasks** | All tasks with status icons, priority, branch, workspace |
| **Agents** | Running agents with kill buttons |
| **Workspaces** | 5 workspaces with branch, dirty status, rollback |
| **Planning Chat** | Conversational task design with plan approval |
| **Logs** | Filterable real-time log stream |

### Controls

- **🔴 STOP ALL** — Emergency kill switch for all agents
- **Kill** — Stop individual agents
- **↩️ Rollback** — Restore workspace to pre-task state
- **✅ Approve Plan** — Convert planning chat into a task + PR lifecycle

## Configuration

All configuration lives in `~/.conductor/`.

### `config.yaml`

```yaml
# Workspace discovery
workspace_pattern: "~/workspace-*"

# GitHub integration
github:
  repo: "owner/repo"
  poll_interval: 60  # seconds

# AI quota (Google AI Ultra)
quota:
  daily_agent_requests: 200
  daily_prompts: 1500
  max_concurrent_agents: 3
  pause_at_percent: 90
  reserve_requests: 20

# PR Lifecycle
pr_lifecycle:
  max_greptile_iterations: 3
  max_ci_fix_retries: 3
  pr_base_branch: main
  precheck_command: "scripts/precheck.sh"

# Safety guardrails
guardrails:
  protected_branches: ["main", "master", "release/*"]
  blocked_paths: ["~/.ssh", "~/.env"]
  max_files_changed: 50
  max_lines_changed: 2000
  task_timeout_minutes: 30
  auto_rollback_on_failure: true

# Logging
logging:
  level: INFO
```

### `rules.yaml`

Define automation rules that create tasks from events:

```yaml
rules:
  - name: auto-fix-ci
    trigger:
      type: ci_failure
      pattern: "ModuleNotFoundError|ImportError"
    action:
      type: create_task
      template: "Fix import error on PR #{pr_number}"
      priority: high

  - name: auto-address-greptile
    trigger:
      type: review_comment
      source: greptile
    action:
      type: create_task
      template: "Address Greptile comment on PR #{pr_number}"
      priority: normal
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        con CLI                              │
├─────────────────────────────────────────────────────────────┤
│                   FastAPI Server (:4000)                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │ REST API │  │WebSocket │  │Scheduler │  │Dashboard │   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘   │
├─────────────────────────────────────────────────────────────┤
│                      Core Modules                           │
│  ┌────────────────┐  ┌────────────────┐  ┌──────────────┐  │
│  │ Agent Manager  │  │ Task Manager   │  │PR Lifecycle  │  │
│  │  (Gemini CLI)  │  │ (State Machine)│  │(8 stages)    │  │
│  └────────────────┘  └────────────────┘  └──────────────┘  │
│  ┌────────────────┐  ┌────────────────┐  ┌──────────────┐  │
│  │Workspace Mgr   │  │ Quota Manager  │  │ Guardrails   │  │
│  │(5 Git clones)  │  │ (Rate Limits)  │  │ (Sandbox)    │  │
│  └────────────────┘  └────────────────┘  └──────────────┘  │
│  ┌────────────────┐  ┌────────────────┐  ┌──────────────┐  │
│  │GitHub Monitor  │  │ Rules Engine   │  │   Planner    │  │
│  │  (gh CLI)      │  │   (YAML)       │  │  (Gemini)    │  │
│  └────────────────┘  └────────────────┘  └──────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  SQLite (WAL)  │  3-Layer Logger  │  ~/.conductor/ configs  │
└─────────────────────────────────────────────────────────────┘
```

## PR Lifecycle Stages

```
 Planning → Coding → Prechecks → PR Created → CI Monitoring
                                                    │
                          ┌─────────────────────────┤
                          ↓                         │
                     CI Fixing ─────────────────────┘
                          │
                          ↓
                   Greptile Review → Addressing Comments
                          │                    │
                          │         ┌──────────┘
                          ↓         ↓
                   Ready for Review  (or Needs Human after 3 iterations)
```

## Task State Machine

```
              ┌──────────────────┐
              │     pending      │
              └────────┬─────────┘
                       │ (deps check)
              ┌────────┴─────────┐
         ┌────┤     blocked      │
         │    └────────┬─────────┘
         │             │ (deps met)
         │    ┌────────┴─────────┐
         │    │      ready       │
         │    └────────┬─────────┘
         │             │ (agent assigned)
         │    ┌────────┴─────────┐
         │    │     running      │
         │    └───┬─────────┬────┘
         │        │         │
    ┌────┴───┐  ┌─┴──┐  ┌──┴───┐
    │cancelled│  │done│  │failed│
    └────────┘  └────┘  └──────┘
```

## Data Storage

| Path | Contents |
|------|----------|
| `~/.conductor/config.yaml` | Main configuration |
| `~/.conductor/rules.yaml` | Automation rules |
| `~/.conductor/conductor.db` | SQLite database (tasks, agents, PRs) |
| `~/.conductor/logs/conductor.log` | Rotating system log |
| `~/.conductor/logs/sessions/<task-id>/` | Per-task session logs |
| `~/.conductor/logs/summaries.jsonl` | Analytics summary log |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT — see [LICENSE](LICENSE) for details.
