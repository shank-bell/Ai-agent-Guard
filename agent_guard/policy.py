"""The rules the agents must follow and the monitor enforces, loaded from policies/policy.json."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "policy.json"


@dataclass
class AgentPolicy:
    purpose: str = ""
    allowed_data: dict[str, list[str]] = field(default_factory=dict)   # resource -> allowed actions
    monitor_duration: bool = False
    expected_task_seconds: dict[str, float] = field(default_factory=dict)


@dataclass
class Policy:
    # Loans at or above this amount need a human. The brief says "below 1000" is autonomous and
    # "more than 1000" needs a human; exactly 1000 is unspecified, so the cautious side is used.
    human_approval_required_from: float = 1000
    authorized_approvers: list[str] = field(default_factory=list)
    approval_max_age_seconds: float = 86400
    split_window_seconds: float = 3600
    min_history: int = 5
    too_fast_ratio: float = 0.2
    too_slow_ratio: float = 3.0
    resource_classification: dict[str, str] = field(default_factory=dict)
    agents: dict[str, AgentPolicy] = field(default_factory=dict)

    def needs_human(self, amount: float) -> bool:
        return amount >= self.human_approval_required_from

    @staticmethod
    def load(path: str | Path | None = None) -> "Policy":
        raw = json.loads(Path(path or DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
        loan, dur = raw.get("loan", {}), raw.get("duration", {})
        return Policy(
            human_approval_required_from=loan.get("human_approval_required_from", 1000),
            authorized_approvers=list(loan.get("authorized_approvers", [])),
            approval_max_age_seconds=loan.get("approval_max_age_seconds", 86400),
            split_window_seconds=loan.get("split_window_seconds", 3600),
            min_history=dur.get("min_history", 5),
            too_fast_ratio=dur.get("too_fast_ratio", 0.2),
            too_slow_ratio=dur.get("too_slow_ratio", 3.0),
            resource_classification=dict(raw.get("resource_classification", {})),
            agents={name: AgentPolicy(**cfg) for name, cfg in raw.get("agents", {}).items()},
        )
