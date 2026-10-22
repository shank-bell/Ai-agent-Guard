"""The monitoring agent.

It never imports or calls the agents. Its only input is the audit log, processed one event at a time, so the
same code runs as a batch scan or as a live watcher. Rules:

  HUMAN_CONSENT_MISSING   a loan at/above the limit was approved without a valid, prior human approval
  FORGED_APPROVAL         an approval event that the approval service did not sign
  SPLIT_LOAN_SUSPECTED    several sub-limit loans to one applicant that together reach the limit
  DURATION_TOO_FAST/SLOW  a task far outside that agent's normal duration
  UNAUTHORISED_DATA_ACCESS  access to a resource/action outside the agent's allow-list
  UNREGISTERED_AGENT      an agent id that the policy does not know
"""
from __future__ import annotations

import statistics
import sys
import time
from datetime import datetime

from .alerts import Alert, Notifier
from .events import APPROVAL_EVENT_TYPES, APPROVAL_SOURCE, AuditLog, Event, approval_secret, signature_valid
from .policy import Policy


def _t(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class Monitor:
    def __init__(self, policy: Policy, notifiers: list[Notifier] | None = None, secret: str | None = None):
        self.policy, self.notifiers = policy, notifiers or []
        self.secret = secret or approval_secret()
        self.alerts: list[Alert] = []
        self.delivery_errors: list[str] = []
        self._seen: set = set()                                   # de-duplication keys
        self._tasks: dict[str, tuple[datetime, str]] = {}         # task_id -> (start, task_type)
        self._history: dict[tuple[str, str], list[float]] = {}    # (agent, task_type) -> normal durations
        self._requested: set[str] = set()                         # loan_ids a human was asked about
        self._approvals: dict[str, list[dict]] = {}               # loan_id -> verified human answers
        self._small_loans: dict[str, list[tuple[datetime, float, str, str]]] = {}   # applicant -> loans
        self.events_seen = 0

    # ---- input -------------------------------------------------------------------------------
    def scan(self, events) -> list[Alert]:
        found: list[Alert] = []
        for ev in events:
            found += self.process(ev)
        return found

    def watch(self, log: AuditLog, poll_seconds: float = 1.0, max_polls: int | None = None,
              stop_when_idle: int | None = None) -> None:
        """Tail the log forever (or until max_polls / `stop_when_idle` consecutive empty polls)."""
        offset, polls, idle = 0, 0, 0
        while max_polls is None or polls < max_polls:
            if log.path.stat().st_size < offset:                  # the log was restarted/truncated: read it afresh
                offset = 0
            events, offset = log.read_from(offset)
            self.scan(events)
            idle = 0 if events else idle + 1
            if stop_when_idle is not None and idle >= stop_when_idle:
                return
            polls += 1
            time.sleep(poll_seconds)

    def process(self, ev: Event) -> list[Alert]:
        self.events_seen += 1
        if ev.type in APPROVAL_EVENT_TYPES:
            return self._on_approval(ev)
        out = self._check_registered(ev)
        if ev.type == "task_start":
            self._tasks[ev.task_id] = (_t(ev.ts), ev.data.get("task_type", "unknown"))
        elif ev.type == "task_end":
            out += self._on_task_end(ev)
        elif ev.type == "data_access":
            out += self._on_data_access(ev)
        elif ev.type == "decision":
            out += self._on_decision(ev)
        return out

    # ---- rules -------------------------------------------------------------------------------
    def _check_registered(self, ev: Event) -> list[Alert]:
        if ev.agent_id in self.policy.agents:
            return []
        return self._raise(("unreg", ev.agent_id), ev, "MEDIUM", "UNREGISTERED_AGENT",
                           f"Unknown agent '{ev.agent_id}' is active",
                           "This agent id has no entry in the policy, so nothing it does can be judged as allowed.")

    def _on_approval(self, ev: Event) -> list[Alert]:
        trusted = ev.source == APPROVAL_SOURCE and signature_valid(ev, self.secret)
        if not trusted:
            why = "its signature is missing or wrong" if ev.source == APPROVAL_SOURCE else f"it was written by '{ev.source}'"
            return self._raise(("forged", ev.id), ev, "CRITICAL", "FORGED_APPROVAL",
                               f"'{ev.agent_id}' wrote an approval event that only the approval service can issue",
                               f"Event {ev.id} claims to be from the approval service but {why}. "
                               f"Claimed approver: {ev.data.get('approver')}. Treated as no approval.")
        loan_id = ev.data.get("loan_id", "")
        if ev.type == "approval_requested":
            self._requested.add(loan_id)
        else:
            approver = ev.data.get("approver", "")
            self._approvals.setdefault(loan_id, []).append({
                "id": ev.id, "ts": _t(ev.ts), "approved": bool(ev.data.get("approved")),
                "approver": approver, "amount": ev.data.get("amount"),
                "authorised": approver in self.policy.authorized_approvers and approver != ev.agent_id})
        return []

    def _on_decision(self, ev: Event) -> list[Alert]:
        d = ev.data
        if d.get("outcome") != "approved":
            return []
        amount = float(d.get("amount", 0))
        if self.policy.needs_human(amount):
            return self._check_consent(ev, amount)
        return self._check_splitting(ev, amount)

    def _check_consent(self, ev: Event, amount: float) -> list[Alert]:
        loan_id, when = ev.data.get("loan_id", ""), _t(ev.ts)
        answers = self._approvals.get(loan_id, [])
        for a in answers:
            age = (when - a["ts"]).total_seconds()
            if a["approved"] and a["authorised"] and a["amount"] == amount and 0 <= age <= self.policy.approval_max_age_seconds:
                return []                                          # valid human consent exists
        if not answers:
            reason = ("a human was asked but no answer had been recorded yet" if loan_id in self._requested
                      else "no human approval was ever requested")
        else:
            last = answers[-1]
            if not last["approved"]:
                reason = f"the human reviewer ({last['approver']}) DENIED it"
            elif not last["authorised"]:
                reason = f"'{last['approver']}' is not an authorised approver"
            elif last["amount"] != amount:
                reason = f"the human approved {last['amount']} but the agent approved {amount:g}"
            else:
                reason = "the human approval was stale"
        evidence = [ev.id] + [a["id"] for a in answers]
        return self._raise(("consent", loan_id), ev, "CRITICAL", "HUMAN_CONSENT_MISSING",
                           f"Loan {loan_id} for {amount:g} approved without human consent",
                           f"Policy requires a human to approve loans of {self.policy.human_approval_required_from:g} or more, "
                           f"but {reason}. Applicant {ev.data.get('applicant')}.", evidence)

    def _check_splitting(self, ev: Event, amount: float) -> list[Alert]:
        applicant, when = ev.data.get("applicant", ""), _t(ev.ts)
        loans = self._small_loans.setdefault(applicant, [])
        loans.append((when, amount, ev.data.get("loan_id", ""), ev.id))
        loans[:] = [x for x in loans if (when - x[0]).total_seconds() <= self.policy.split_window_seconds]
        total = sum(x[1] for x in loans)
        if len(loans) >= 2 and self.policy.needs_human(total):
            window_start = loans[0][0].isoformat()
            return self._raise(("split", applicant, window_start), ev, "HIGH", "SPLIT_LOAN_SUSPECTED",
                               f"{len(loans)} small loans to {applicant} add up to {total:g}",
                               f"Each loan is below {self.policy.human_approval_required_from:g} so none needed a human, "
                               f"but together they reach {total:g} within {self.policy.split_window_seconds / 60:g} minutes. "
                               f"This looks like one large loan split to dodge the approval rule.",
                               [x[3] for x in loans])
        return []

    def _on_task_end(self, ev: Event) -> list[Alert]:
        started = self._tasks.pop(ev.task_id, None)
        agent_cfg = self.policy.agents.get(ev.agent_id)
        if started is None or agent_cfg is None or not agent_cfg.monitor_duration:
            return []
        if ev.data.get("status", "completed") != "completed":
            return []
        start, task_type = started
        seconds = (_t(ev.ts) - start).total_seconds()
        key = (ev.agent_id, task_type)
        history = self._history.setdefault(key, [])
        if len(history) >= self.policy.min_history:
            baseline, basis = statistics.median(history), f"median of its last {len(history)} runs"
        elif task_type in agent_cfg.expected_task_seconds:
            baseline, basis = agent_cfg.expected_task_seconds[task_type], "the expected duration in the policy"
        else:
            history.append(seconds)                                # still learning what normal looks like
            return []
        if seconds < baseline * self.policy.too_fast_ratio:
            return self._raise(("fast", ev.task_id), ev, "HIGH", "DURATION_TOO_FAST",
                               f"'{task_type}' finished in {seconds:.0f}s, normally ~{baseline:.0f}s",
                               f"{ev.agent_id} completed this task in {seconds:.0f}s against {baseline:.0f}s ({basis}). "
                               f"Finishing this fast suggests steps were skipped or the output was not really produced.")
        if seconds > baseline * self.policy.too_slow_ratio:
            return self._raise(("slow", ev.task_id), ev, "MEDIUM", "DURATION_TOO_SLOW",
                               f"'{task_type}' took {seconds:.0f}s, normally ~{baseline:.0f}s",
                               f"{ev.agent_id} took {seconds:.0f}s against {baseline:.0f}s ({basis}); it may be stuck or doing extra work.")
        history.append(seconds)                                    # only normal runs shape the baseline
        return []

    def _on_data_access(self, ev: Event) -> list[Alert]:
        agent_cfg = self.policy.agents.get(ev.agent_id)
        if agent_cfg is None:
            return []
        resource, action = ev.data.get("resource", ""), ev.data.get("action", "read")
        if action in agent_cfg.allowed_data.get(resource, []):
            return []
        restricted = self.policy.resource_classification.get(resource) == "restricted"
        allowed = ", ".join(f"{r} ({'/'.join(a)})" for r, a in agent_cfg.allowed_data.items()) or "nothing"
        return self._raise(("data", ev.agent_id, ev.task_id, resource, action), ev,
                           "CRITICAL" if restricted else "HIGH", "UNAUTHORISED_DATA_ACCESS",
                           f"{ev.agent_id} {action} '{resource}' ({ev.data.get('records', '?')} records), not permitted",
                           f"'{resource}' is {'classified restricted and ' if restricted else ''}outside this agent's allow-list. "
                           f"Allowed: {allowed}.")

    # ---- output ------------------------------------------------------------------------------
    def _raise(self, key, ev: Event, severity: str, rule: str, title: str, detail: str,
               evidence: list[str] | None = None) -> list[Alert]:
        if key in self._seen:
            return []
        self._seen.add(key)
        alert = Alert(id=f"ALR-{len(self.alerts) + 1:04d}", ts=ev.ts, severity=severity, rule=rule,
                      agent_id=ev.agent_id, task_id=ev.task_id, title=title, detail=detail,
                      evidence=evidence or [ev.id])
        self.alerts.append(alert)
        for n in self.notifiers:
            try:
                n.send(alert)
            except Exception as exc:                               # one broken channel must not stop the monitor
                self.delivery_errors.append(f"{n.name}: {exc}")
                print(f"  (notification via {n.name} failed: {exc})", file=sys.stderr)
        return [alert]
