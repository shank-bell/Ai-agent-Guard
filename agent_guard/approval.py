"""Human-in-the-loop approval service.

It is a separate component from the agents: it is the only writer of signed approval events.
`ScriptedReviewer` stands in for a real person (in production: a ticket, Slack button, or email link).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .clock import Clock
from .events import APPROVAL_SOURCE, AuditLog, approval_secret


@dataclass
class ScriptedReviewer:
    name: str = "alice.manager"
    delay_seconds: float = 180            # how long the human takes to respond
    deny_loan_ids: set = field(default_factory=set)

    def review(self, request: dict) -> bool:
        return request["loan_id"] not in self.deny_loan_ids


class ApprovalService:
    def __init__(self, log: AuditLog, clock: Clock, reviewer: ScriptedReviewer, secret: str | None = None):
        self.log, self.clock, self.reviewer = log, clock, reviewer
        self.secret = secret or approval_secret()
        self._tickets: dict[str, dict] = {}
        self._ids = itertools.count(1)

    def request(self, agent_id: str, task_id: str, loan_id: str, applicant: str, amount: float) -> str:
        ticket = f"APR-{next(self._ids):04d}"
        self._tickets[ticket] = {"agent_id": agent_id, "task_id": task_id, "loan_id": loan_id,
                                 "applicant": applicant, "amount": amount}
        self.log.emit("approval_requested", agent_id, source=APPROVAL_SOURCE, task_id=task_id, secret=self.secret,
                      data={"ticket": ticket, "loan_id": loan_id, "applicant": applicant, "amount": amount})
        return ticket

    def wait(self, ticket: str) -> dict:
        """Block (in simulated time) until the human answers, then record the answer."""
        req = self._tickets[ticket]
        self.clock.sleep(self.reviewer.delay_seconds)
        approved = self.reviewer.review(req)
        self.log.emit("approval_decided", req["agent_id"], source=APPROVAL_SOURCE, task_id=req["task_id"],
                      secret=self.secret,
                      data={"ticket": ticket, "loan_id": req["loan_id"], "amount": req["amount"],
                            "approved": approved, "approver": self.reviewer.name})
        return {"approved": approved, "approver": self.reviewer.name}
