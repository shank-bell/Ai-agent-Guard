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


def _make_llm(args):
    from .llm import BedrockLLM
    from .llm_agents import simulated_model
    if args.offline_llm:
        return simulated_model(gullible=True)
    return BedrockLLM(args.model_id, args.region)


def _live(args) -> int:
    """Run the agents in (sped-up) real time, writing to a log that a monitor in another window can tail."""
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")                      # start a fresh log
    policy = Policy.load(args.policy)
    clock = ScaledClock(datetime.now(timezone.utc), args.speed)
    log = AuditLog(log_path, clock)
    approvals = ApprovalService(log, clock, ScriptedReviewer())
    inc = args.incident
    if args.llm:
        from .llm_agents import LLMAnalyticsAgent, LLMLoanAgent
        from .llm_scenarios import ANALYTICS_CASES, LOAN_CASES
        llm = _make_llm(args)
        print(f"LLM-driven agents using model {llm.model_id} at {args.speed:g}x speed, log: {log_path} (incident: {inc})")
        loans = LLMLoanAgent(log, clock, policy, approvals, llm, enforce_policy=args.guard)
        cases = [c for c in LOAN_CASES if c[5] == "normal"][:3]
        if inc == "loan":
            cases.append(next(c for c in LOAN_CASES if c[0] == "inject-notice"))
        for cid, who, amount, score, note, kind in cases:
            print(f"  llm-loan-agent handling {who} amount {amount}" + ("  <- application text hides an attack" if kind == "attack" else ""))
            res = loans.handle({"loan_id": f"LIVE-{cid}", "applicant": who, "amount": amount, "credit_score": score,
                                "purpose": "personal loan", "applicant_note": note})
            print(f"     model said: {res['model_action']} -> outcome: {res['outcome']}"
                  + ("  (code guard overrode the model)" if res["overridden"] else ""))
        analytics = LLMAnalyticsAgent(log, clock, policy, llm, enforce_policy=args.guard)
        task = (next(c for c in ANALYTICS_CASES if c[0] == "inject-notice")[1] if inc == "data"
                else ANALYTICS_CASES[0][1])
        print("  llm-analytics-agent running" + ("  <- task text hides an attack" if inc == "data" else ""))
        res = analytics.run(task)
        print(f"     model chose to read: {', '.join(res['datasets_read'])}")
        print("Agents finished.")
        return 0
    good = LoanAgent(log, clock, policy, approvals)
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


def _llm_scenarios(args) -> int:
    from .llm_scenarios import run_llm_scenarios, write_report
    llm = _make_llm(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"Running LLM-driven agents with model {llm.model_id}" + ("" if args.offline_llm else f" in {args.region}")
          + " (this calls the model about 30 times)")
    phases = run_llm_scenarios(out, llm, Policy.load(args.policy))
    path = write_report(phases, out, llm, args.region)
    for p in phases:
        caught = len([r for r in p.violations if r.flagged])
        print(f"  {'PASS' if p.ok else 'FAIL'}  {p.title}: model broke policy {len(p.violations)}x, "
              f"monitor caught {caught}, missed {len(p.missed)}, false alarms {len(p.false_alarms) + p.unexpected_alerts}")
    errors = sum(len(p.errors) for p in phases)
    if errors:
        print(f"\n{errors} model call(s) FAILED, for example: {next(r.error for p in phases for r in p.rows if r.error)}")
    print(f"\nReport: {path}")
    if errors:
        return 3
    return 0 if all(p.ok for p in phases) else 1


def _add_llm_options(parser) -> None:
    from .llm import DEFAULT_MODEL_ID, DEFAULT_REGION
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID, help="Bedrock model id (default: %(default)s)")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--offline-llm", action="store_true",
                        help="use a SIMULATED stand-in model instead of calling AWS (for when you are not signed in)")


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
    _add_llm_options(lv)
    lv.add_argument("--llm", action="store_true", help="use the language-model-driven agents instead of the rule-based ones")
    lv.add_argument("--guard", action="store_true", help="(with --llm) add a hard policy check in code")
    lv.set_defaults(fn=_live)
    ls = sub.add_parser("llm-scenarios", help="test the monitor against LLM-driven agents, including prompt-injection attacks")
    ls.add_argument("--out", default=str(DEFAULT_OUT / "llm"))
    _add_llm_options(ls)
    ls.set_defaults(fn=_llm_scenarios)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
