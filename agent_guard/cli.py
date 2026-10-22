"""Command line: run the scenarios, or point the monitor at any audit log."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .agents import AnalyticsAgent, LoanAgent, ReportAgent, RogueLoanAgent
from .alerts import Alert, ConsoleNotifier, FileNotifier, notifiers_from_env
from .approval import ApprovalService, ScriptedReviewer
from .clock import ScaledClock
from .events import AuditLog
from .monitor import Monitor
from .policy import Policy
from .scenarios import NORMAL_APPS, SCENARIOS, run_scenario, write_results

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "output"


def _scenarios(args) -> int:
    chosen = [s for s in SCENARIOS if not args.only or s.name in args.only]
    if not chosen:
        print("No such scenario. Available:", ", ".join(s.name for s in SCENARIOS))
        return 2
    policy = Policy.load(args.policy)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for sc in chosen:
        print(f"\n=== {sc.name}: {sc.title}")
        res = run_scenario(sc, out, policy, quiet=False,
                           extra_notifiers=notifiers_from_env(f"SIMULATED test scenario: {sc.name}"))
        if not res.alerts:
            print("  (monitor raised no alerts)")
        print(f"  -> {'PASS' if res.passed else 'FAIL'}" + "".join(f"\n     {p}" for p in res.problems))
        results.append(res)
    # a partial run must not overwrite the full report
    path = write_results(results, out, "RESULTS_partial.md" if args.only else "RESULTS.md")
    ok = sum(r.passed for r in results)
    print(f"\n{ok}/{len(results)} scenarios passed. Report: {path}")
    return 0 if ok == len(results) else 1


def _monitor(args) -> int:
    log = AuditLog(args.log)
    notifiers = [ConsoleNotifier()] + notifiers_from_env(f"monitoring {Path(args.log).name}")
    if args.alerts_out:
        notifiers.append(FileNotifier(args.alerts_out))
    monitor = Monitor(Policy.load(args.policy), notifiers)
    if args.watch:
        print(f"Watching {args.log} (Ctrl+C to stop)")
        try:
            monitor.watch(log, poll_seconds=args.poll)
        except KeyboardInterrupt:
            pass
    else:
        monitor.scan(log.read_all())
    print(f"\n{monitor.events_seen} events scanned, {len(monitor.alerts)} alert(s).")
    return 1 if monitor.alerts else 0


def _notify_test(args) -> int:
    """Send one harmless test message to every configured real channel and say whether it arrived."""
    channels = notifiers_from_env("connection test, not a real incident")
    if not channels:
        print("No real channel is configured, so nothing was sent.\n"
              "Set AGENT_GUARD_SLACK_WEBHOOK_URL (or the webhook/email variables, see README) and try again.")
        return 2
    test = Alert(id="ALR-TEST", ts=datetime.now(timezone.utc).isoformat(), severity="LOW", rule="TEST_NOTIFICATION",
                 agent_id="agent-guard", task_id="", title="Agent Guard can reach this channel",
                 detail="If you can read this, real alerts will arrive here too. This is only a test.",
                 evidence=["none"])
    failed = 0
    for ch in channels:
        try:
            ch.send(test)
            print(f"  {ch.name}: sent")
        except Exception as exc:
            failed += 1
            print(f"  {ch.name}: FAILED - {exc}")
    return 1 if failed else 0


def _live(args) -> int:
    """Run the agents in (sped-up) real time, writing to a log that a monitor in another window can tail."""
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")                      # start a fresh log
    policy = Policy.load(args.policy)
    clock = ScaledClock(datetime.now(timezone.utc), args.speed)
    log = AuditLog(log_path, clock)
    approvals = ApprovalService(log, clock, ScriptedReviewer())
    good = LoanAgent(log, clock, policy, approvals)
    inc = args.incident
    print(f"Agents running at {args.speed:g}x speed, writing to {log_path} (incident: {inc})")
    for n, (who, amount, score) in enumerate(NORMAL_APPS[:3], 1):
        print(f"  loan-agent handling {who} amount {amount}")
        good.handle({"loan_id": f"LIVE-{n}", "applicant": who, "amount": amount, "credit_score": score})
    if inc == "loan":
        print("  loan-agent handling cust-200 amount 5000 (this one will skip the human)")
        RogueLoanAgent(log, clock, policy, approvals, "skip_approval").handle(
            {"loan_id": "LIVE-X", "applicant": "cust-200", "amount": 5000, "credit_score": 730})
    print("  report-agent running" + (" (rushed)" if inc == "report" else ""))
    ReportAgent(log, clock, policy, rushed=inc == "report").run()
    print("  analytics-agent running" + (" (nosy)" if inc == "data" else ""))
    AnalyticsAgent(log, clock, policy, nosy=inc == "data").run()
    print("Agents finished.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agent_guard", description="Policy-bound demo agents and their monitor")
    p.add_argument("--policy", default=None, help="path to a policy JSON (default: policies/policy.json)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scenarios", help="simulate the agents and check the monitor catches each misbehaviour")
    s.add_argument("--only", nargs="*", help="scenario names to run (default: all)")
    s.add_argument("--out", default=str(DEFAULT_OUT))
    s.set_defaults(fn=_scenarios)
    m = sub.add_parser("monitor", help="scan (or watch) an existing audit log")
    m.add_argument("--log", required=True)
    m.add_argument("--watch", action="store_true", help="keep tailing the log and alert as events arrive")
    m.add_argument("--poll", type=float, default=1.0)
    m.add_argument("--alerts-out", default=None, help="also append alerts to this JSON-lines file")
    m.set_defaults(fn=_monitor)
    nt = sub.add_parser("notify-test", help="send one test message to the configured Slack/webhook/email channels")
    nt.set_defaults(fn=_notify_test)
    lv = sub.add_parser("live", help="run the agents in sped-up real time so a second window can watch them")
    lv.add_argument("--log", default=str(DEFAULT_OUT / "live" / "audit.jsonl"))
    lv.add_argument("--incident", choices=["none", "loan", "report", "data"], default="loan")
    lv.add_argument("--speed", type=float, default=60.0, help="time compression (60 = 5 minutes take 5 seconds)")
    lv.set_defaults(fn=_live)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
