from datetime import datetime, timezone

import pytest

from agent_guard.approval import ApprovalService, ScriptedReviewer
from agent_guard.clock import SimClock
from agent_guard.events import AuditLog
from agent_guard.monitor import Monitor
from agent_guard.policy import Policy

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


class Rig:
    """A fresh log + simulated clock + approval service + monitor for one test."""

    def __init__(self, tmp_path):
        self.clock = SimClock(T0)
        self.log = AuditLog(tmp_path / "audit.jsonl", self.clock)
        self.policy = Policy.load()
        self.reviewer = ScriptedReviewer()
        self.approvals = ApprovalService(self.log, self.clock, self.reviewer)

    def monitor(self, **kw) -> Monitor:
        return Monitor(self.policy, **kw)

    def scan(self):
        m = self.monitor()
        m.scan(self.log.read_all())
        return m.alerts

    # small helpers to write the events an agent would write
    def decision(self, loan_id, amount, outcome="approved", applicant="cust-1", agent="loan-agent"):
        return self.log.emit("decision", agent, task_id="t1",
                             data={"loan_id": loan_id, "applicant": applicant, "amount": amount, "outcome": outcome})

    def task(self, agent, task_type, seconds, status="completed", task_id=None):
        tid = task_id or f"t-{len(self.log.read_all())}"
        self.log.emit("task_start", agent, task_id=tid, data={"task_type": task_type})
        self.clock.sleep(seconds)
        self.log.emit("task_end", agent, task_id=tid, data={"status": status})
        self.clock.sleep(60)

    def access(self, agent, resource, action="read", task_id="t1"):
        return self.log.emit("data_access", agent, task_id=task_id,
                             data={"resource": resource, "action": action, "records": 3})


@pytest.fixture(autouse=True)
def no_real_notifications(monkeypatch):
    """Tests must never message a real Slack/webhook/mailbox, even if the developer has them configured."""
    import os
    for name in [k for k in os.environ if k.startswith("AGENT_GUARD_")]:
        monkeypatch.delenv(name)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)
