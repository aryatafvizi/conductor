"""Rules Engine — load rules from YAML, evaluate triggers, create tasks."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from logger import log_event
from models import Rule, TaskPriority

DEFAULT_RULES_PATH = Path.home() / ".conductor" / "rules.yaml"


class RulesEngine:
    """Loads rules and evaluates triggers against events."""

    def __init__(self, rules_path: Path = DEFAULT_RULES_PATH) -> None:
        self.rules_path = rules_path
        self.rules: list[Rule] = []
        self.load_rules()

    def load_rules(self) -> list[Rule]:
        """Load rules from YAML file."""
        self.rules = []
        if not self.rules_path.exists():
            log_event("rules_engine", "no_rules_file",
                      path=str(self.rules_path))
            return self.rules

        try:
            with open(self.rules_path) as f:
                data = yaml.safe_load(f) or {}

            for rule_data in data.get("rules", []):
                trigger = rule_data.get("trigger", {})
                action = rule_data.get("action", {})
                rule = Rule(
                    name=rule_data.get("name", ""),
                    trigger_type=trigger.get("type", ""),
                    trigger_pattern=trigger.get("pattern", ""),
                    trigger_source=trigger.get("source", ""),
                    action_type=action.get("type", ""),
                    action_template=action.get("template", ""),
                    action_priority=TaskPriority(
                        action.get("priority", "normal")
                    ),
                    enabled=rule_data.get("enabled", True),
                )
                self.rules.append(rule)

            log_event("rules_engine", "rules_loaded", count=len(self.rules))
        except Exception as e:
            log_event("rules_engine", "rules_load_error", level="ERROR",
                      error=str(e))

        return self.rules

    def evaluate(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Evaluate an event against all rules. Returns list of actions."""
        actions = []
        event_type = event.get("type", "")

        for rule in self.rules:
            if not rule.enabled:
                continue

            if not self._matches_trigger(rule, event, event_type):
                continue

            action = self._build_action(rule, event)
            if action:
                actions.append(action)
                log_event("rules_engine", "rule_triggered",
                          rule=rule.name, event_type=event_type,
                          action_type=action["type"])

        return actions

    def _matches_trigger(
        self, rule: Rule, event: dict[str, Any], event_type: str,
    ) -> bool:
        """Check if an event matches a rule's trigger."""
        # Type must match
        if rule.trigger_type and rule.trigger_type != event_type:
            return False

        # Source filter (e.g., "greptile")
        if rule.trigger_source:
            event_source = event.get("source", "")
            if rule.trigger_source.lower() != event_source.lower():
                return False

        # Pattern matching (regex on event body/message)
        if rule.trigger_pattern:
            searchable = json.dumps(event)
            if not re.search(rule.trigger_pattern, searchable, re.IGNORECASE):
                return False

        return True

    def _build_action(
        self, rule: Rule, event: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Build an action from a matched rule."""
        # Template substitution
        title = rule.action_template
        for key, value in event.items():
            title = title.replace(f"{{{key}}}", str(value))

        return {
            "type": rule.action_type,
            "title": title,
            "priority": rule.action_priority.value,
            "event": event,
            "rule_name": rule.name,
        }


# Need json for _matches_trigger
import json  # noqa: E402
