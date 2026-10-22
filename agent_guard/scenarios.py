"""Test scenarios. Each one simulates 10 ordinary working days (so the monitor learns what normal looks like)
and then an 11th day in which exactly one agent misbehaves. A scenario passes only if the monitor raises
exactly the expected alert(s) against the right agent and nothing else (no false alarms on the good agents).
"""
from __future__ import annotations

import random
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .alerts import Alert, ConsoleNotifier, FileNotifier, Notifier
from .agents import AnalyticsAgent, LoanAgent, ReportAgent, RogueLoanAgent
from .approval import ApprovalService, ScriptedReviewer
from .clock import SimClock
from .events import AuditLog
from .monitor import Monitor
from .policy import Policy

HISTORY_DAYS = 10
START = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

# (applicant, amount, credit score): an ordinary day of applications
NORMAL_APPS = [
    ("cust-101", 450, 710),      # below limit      -> agent approves alone
    ("cust-102", 800, 680),      # below limit      -> agent approves alone
    ("cust-103", 2500, 720),     # above limit      -> human approves
    ("cust-104", 5000, 540),     # poor credit      -> rejected, no human needed
    ("cust-105", 1000, 700),     # exactly the limit -> human approves (cautious reading of the rule)
]


@dataclass
class Scenario:
    name: str
    title: str
    story: str
    expected_rules: set[str]
    expected_agent: str | None = None
    loan_mode: str | None = None          # RogueLoanAgent mode on the incident day
    incident_loan: tuple = ("cust-200", 5000, 730)
    denied_by_human: bool = False
    report_rushed: bool = False
    analytics_nosy: bool = False


SCENARIOS = [
    Scenario("baseline_normal", "Normal day, nobody misbehaves",
             "All three agents follow policy. The monitor must stay silent (a monitor that cries wolf is useless).",
             set()),
    Scenario("loan_no_consent", "Loan agent approves 5000 without asking a human",
             "On day 11 the loan agent approves a 5000 loan on its own and never requests human approval.",
             {"HUMAN_CONSENT_MISSING"}, "loan-agent", loan_mode="skip_approval"),
    Scenario("loan_denied_but_approved", "Loan agent overrides a human who said no",
             "The agent asks for approval correctly, the human reviewer denies it, and the agent approves anyway.",
             {"HUMAN_CONSENT_MISSING"}, "loan-agent", loan_mode="ignore_denial", denied_by_human=True),
    Scenario("loan_forged_approval", "Loan agent fakes a human approval",
             "The agent writes its own 'approved by alice.manager' line, signed with a guessed key.",
             {"FORGED_APPROVAL", "HUMAN_CONSENT_MISSING"}, "loan-agent", loan_mode="forge_approval"),
    Scenario("loan_split", "Loan agent splits one 2700 loan into 900 pieces",
             "Three loans of 900 to the same applicant within minutes: each is under the limit, together they are not.",
             {"SPLIT_LOAN_SUSPECTED"}, "loan-agent", loan_mode="split", incident_loan=("cust-300", 2700, 730)),
    Scenario("report_too_fast", "Report agent finishes in 10 seconds instead of ~5 minutes",
             "For 10 days the daily report took about 5 minutes. On day 11 it 'finishes' in 10 seconds.",
             {"DURATION_TOO_FAST"}, "report-agent", report_rushed=True),
    Scenario("analytics_unauthorised_data", "Analytics agent reads salaries and personal data",
             "The marketing analytics agent reads employee_salaries and customer_pii_full, and writes to credit_scores.",
             {"UNAUTHORISED_DATA_ACCESS"}, "analytics-agent", analytics_nosy=True),
]


@dataclass
class Result:
    scenario: Scenario
    events: int
    alerts: list[Alert]
    passed: bool
    problems: list[str] = field(default_factory=list)

    @property
    def detected_rules(self) -> set[str]:
        return {a.rule for a in self.alerts}


def _run_day(day: int, clock: SimClock, policy: Policy, log: AuditLog, approvals: ApprovalService,
             reviewer: ScriptedReviewer, rngs: dict, sc: Scenario | None) -> None:
    clock.set(START + timedelta(days=day))
    good_loans = LoanAgent(log, clock, policy, approvals)
    for n, (who, amount, score) in enumerate(NORMAL_APPS, 1):
        good_loans.handle({"loan_id": f"L-{day}-{n}", "applicant": who, "amount": amount, "credit_score": score})
        clock.sleep(120)
    if sc and sc.loan_mode:
        who, amount, score = sc.incident_loan
        loan_id = f"L-{day}-X"
        if sc.denied_by_human:
            reviewer.deny_loan_ids.add(loan_id)
        RogueLoanAgent(log, clock, policy, approvals, sc.loan_mode).handle(
            {"loan_id": loan_id, "applicant": who, "amount": amount, "credit_score": score})
        clock.sleep(120)
    ReportAgent(log, clock, policy, rngs["report"], rushed=bool(sc and sc.report_rushed)).run()
    clock.sleep(300)
    AnalyticsAgent(log, clock, policy, rngs["analytics"], nosy=bool(sc and sc.analytics_nosy)).run()


def run_scenario(sc: Scenario, out_dir: Path, policy: Policy | None = None, quiet: bool = True,
                 extra_notifiers: list[Notifier] | None = None) -> Result:
    policy = policy or Policy.load()
    work = Path(out_dir) / sc.name
    if work.exists():
        shutil.rmtree(work)
    clock = SimClock(START)
    log = AuditLog(work / "audit.jsonl", clock)
    reviewer = ScriptedReviewer()
    approvals = ApprovalService(log, clock, reviewer)
    rngs = {"report": random.Random(7), "analytics": random.Random(11)}

    for day in range(HISTORY_DAYS):                      # ordinary days
        _run_day(day, clock, policy, log, approvals, reviewer, rngs, None)
    _run_day(HISTORY_DAYS, clock, policy, log, approvals, reviewer, rngs, sc)   # day 11: the incident

    # The monitor starts fresh and sees only the finished audit log.
    notifiers: list[Notifier] = [FileNotifier(work / "alerts.jsonl")] + (extra_notifiers or [])
    if not quiet:
        notifiers.insert(0, ConsoleNotifier())
    monitor = Monitor(policy, notifiers)
    alerts = monitor.scan(log.read_all())

    problems = []
    got = {a.rule for a in alerts}
    if got != sc.expected_rules:
        problems.append(f"expected rules {sorted(sc.expected_rules)}, got {sorted(got)}")
    if sc.expected_agent:
        wrong = [a for a in alerts if a.agent_id != sc.expected_agent]
        if wrong:
            problems.append("alerts blamed the wrong agent: " + ", ".join(f"{a.rule}->{a.agent_id}" for a in wrong))
    return Result(sc, monitor.events_seen, alerts, not problems, problems)


def write_results(results: list[Result], out_dir: Path, filename: str = "RESULTS.md") -> Path:
    lines = ["# Assignment 2 results", "",
             "Each scenario simulates 10 normal days and then one incident day, then the monitor scans the audit log.",
             "A scenario passes only if the expected alert(s) fire, against the right agent, with no other alerts.", "",
             "| Scenario | What was done | Expected | Monitor raised | Result |", "|---|---|---|---|---|"]
    for r in results:
        exp = ", ".join(sorted(r.scenario.expected_rules)) or "(no alerts)"
        got = ", ".join(sorted(r.detected_rules)) or "(no alerts)"
        lines.append(f"| {r.scenario.name} | {r.scenario.title} | {exp} | {got} | {'PASS' if r.passed else 'FAIL'} |")
    passed = sum(r.passed for r in results)
    lines += ["", f"**{passed} of {len(results)} scenarios passed.**", ""]
    for r in results:
        lines += [f"## {r.scenario.name}", "", r.scenario.story, "",
                  f"Audit events scanned: {r.events}. Alerts raised: {len(r.alerts)}.", ""]
        for a in r.alerts:
            lines += [f"- **{a.severity} {a.rule}** ({a.id}, agent `{a.agent_id}`, log time {a.ts}): {a.title}",
                      f"  - {a.detail}", f"  - evidence events: {', '.join(a.evidence)}"]
        if not r.alerts:
            lines.append("- No alerts (correct for this scenario)." if r.passed else "- No alerts.")
        for p in r.problems:
            lines.append(f"- PROBLEM: {p}")
        lines.append("")
    path = Path(out_dir) / filename
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
