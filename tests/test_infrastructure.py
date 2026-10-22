"""Audit log, notification channels, live watching, CLI and the monitor's independence from the agents."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent_guard import alerts as alerts_mod
from agent_guard import cli
from agent_guard.alerts import Alert, EmailNotifier, FileNotifier, WebhookNotifier, notifiers_from_env
from agent_guard.events import AuditLog, Event, sign, signature_valid

ALERT = Alert("ALR-1", "2026-10-01T09:00:00+00:00", "CRITICAL", "HUMAN_CONSENT_MISSING", "loan-agent", "t1",
              "Loan L1 approved without consent", "details here", ["e1"])


def test_log_stamps_time_itself(rig):
    ev = rig.log.emit("task_start", "report-agent", data={"task_type": "x"})
    assert ev.ts == rig.clock.now().isoformat()


def test_roundtrip_and_signature(rig):
    ev = rig.log.emit("approval_decided", "loan-agent", source="approval-service", data={"a": 1}, secret="s3")
    back = rig.log.read_all()[0]
    assert back == ev and signature_valid(back, "s3") and not signature_valid(back, "other")
    back.data["a"] = 2
    assert not signature_valid(back, "s3")


def test_incremental_read_and_partial_line(rig):
    rig.access("report-agent", "reports", "write")
    events, offset = rig.log.read_from(0)
    assert len(events) == 1
    with rig.log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"task_st')                      # a writer is mid-line
    assert rig.log.read_from(offset) == ([], offset)
    with rig.log.path.open("a", encoding="utf-8") as fh:
        fh.write('art","agent_id":"report-agent","source":"report-agent"}\n')
    events, _ = rig.log.read_from(offset)
    assert [e.type for e in events] == ["task_start"]


def test_corrupt_line_is_skipped(rig):
    with rig.log.path.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
    rig.access("report-agent", "reports", "write")
    assert len(rig.log.read_all()) == 1


def test_watch_picks_up_events_written_while_it_runs(rig):
    m = rig.monitor()

    def write_late():
        rig.access("analytics-agent", "employee_salaries")

    t = threading.Timer(0.15, write_late)
    t.start()
    m.watch(rig.log, poll_seconds=0.05, stop_when_idle=12)
    t.join()
    assert [a.rule for a in m.alerts] == ["UNAUTHORISED_DATA_ACCESS"]


def test_watch_survives_the_log_being_restarted(rig):
    for _ in range(5):
        rig.access("report-agent", "reports", "write")          # a long, clean log
    m = rig.monitor()

    def restart_and_misbehave():
        rig.log.path.write_text("", encoding="utf-8")           # new, shorter log than the saved offset
        rig.access("analytics-agent", "employee_salaries")

    t = threading.Timer(0.2, restart_and_misbehave)
    t.start()
    m.watch(rig.log, poll_seconds=0.05, stop_when_idle=12)
    t.join()
    assert [a.rule for a in m.alerts] == ["UNAUTHORISED_DATA_ACCESS"]


def test_file_notifier_writes_json_lines(tmp_path):
    n = FileNotifier(tmp_path / "out" / "alerts.jsonl")
    n.send(ALERT)
    n.send(ALERT)
    rows = [json.loads(x) for x in (tmp_path / "out" / "alerts.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["rule"] == "HUMAN_CONSENT_MISSING"


def test_webhook_delivers_a_slack_style_payload():
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        WebhookNotifier(f"http://127.0.0.1:{srv.server_port}/hook").send(ALERT)
    finally:
        srv.shutdown()
    assert "HUMAN_CONSENT_MISSING" in got[0]["text"] and got[0]["alert"]["id"] == "ALR-1"


def test_email_notifier_builds_and_sends_a_message(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["conn"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = (u, p)

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(alerts_mod.smtplib, "SMTP", FakeSMTP)
    EmailNotifier("smtp.example.com", 587, "guard@example.com", ["boss@example.com"], "u", "p").send(ALERT)
    assert sent["conn"] == ("smtp.example.com", 587) and sent["tls"] and sent["login"] == ("u", "p")
    assert sent["msg"]["To"] == "boss@example.com" and "HUMAN_CONSENT_MISSING" in sent["msg"]["Subject"]


def test_no_real_channel_is_enabled_without_env(monkeypatch):
    for k in ("AGENT_GUARD_WEBHOOK_URL", "AGENT_GUARD_SMTP_HOST", "AGENT_GUARD_EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    assert notifiers_from_env() == []
    monkeypatch.setenv("AGENT_GUARD_WEBHOOK_URL", "http://example.invalid/x")
    monkeypatch.setenv("AGENT_GUARD_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("AGENT_GUARD_EMAIL_TO", "a@x.com, b@x.com")
    assert [n.name for n in notifiers_from_env()] == ["webhook", "email"]


def test_monitor_never_touches_the_agents():
    """The monitor must work from the log alone, so it may not import the agent code."""
    src = Path(cli.__file__).with_name("monitor.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+(\S+)", src, re.M)
    assert not [i for i in imports if i in (".agents", ".approval", ".scenarios", ".cli", ".llm", ".llm_agents")]


def test_cli_monitor_exit_code_reflects_alerts(rig, capsys):
    assert cli.main(["monitor", "--log", str(rig.log.path)]) == 0
    rig.access("analytics-agent", "employee_salaries")
    assert cli.main(["monitor", "--log", str(rig.log.path)]) == 1
    assert "UNAUTHORISED_DATA_ACCESS" in capsys.readouterr().out


@pytest.mark.parametrize("incident,rule", [("loan", "HUMAN_CONSENT_MISSING"), ("report", "DURATION_TOO_FAST"),
                                           ("data", "UNAUTHORISED_DATA_ACCESS"), ("none", None)])
def test_live_mode_writes_a_log_the_monitor_can_judge(tmp_path, capsys, incident, rule):
    log = tmp_path / "live.jsonl"
    assert cli.main(["live", "--log", str(log), "--incident", incident, "--speed", "2000"]) == 0   # fast, yet real overhead stays tiny in sim time
    capsys.readouterr()
    code = cli.main(["monitor", "--log", str(log)])
    out = capsys.readouterr().out
    assert (rule in out) if rule else (code == 0 and "0 alert(s)" in out)


def test_partial_run_does_not_overwrite_the_full_report(tmp_path):
    (tmp_path / "RESULTS.md").write_text("FULL REPORT", encoding="utf-8")
    assert cli.main(["scenarios", "--only", "baseline_normal", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "RESULTS.md").read_text(encoding="utf-8") == "FULL REPORT"
    assert (tmp_path / "RESULTS_partial.md").exists()


def test_cli_unknown_scenario_is_an_error(tmp_path):
    assert cli.main(["scenarios", "--only", "nope", "--out", str(tmp_path)]) == 2
