"""Slack notifier, tested against a local fake Slack (nothing leaves this machine)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_guard import cli
from agent_guard.alerts import Alert, SlackNotifier, notifiers_from_env
from agent_guard.monitor import Monitor

SECRET_PATH = "/services/T0000/B0000/SuperSecretToken123"
ALERT = Alert("ALR-7", "2026-10-11T09:00:00+00:00", "CRITICAL", "HUMAN_CONSENT_MISSING", "loan-agent", "t-1",
              "Loan L-X for 5000 <approved> & more", "Policy <says> no.", ["e1", "e2"])


class FakeSlack:
    """Answers each POST with the next scripted (status, body, headers); records what it received."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.received = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                status, body, headers = outer.script.pop(0) if outer.script else (200, "ok", {})
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}{SECRET_PATH}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def slack():
    s = FakeSlack()
    yield s
    s.close()


def test_card_has_colour_severity_agent_and_evidence(slack):
    SlackNotifier(slack.url, context="SIMULATED test scenario: x", min_interval=0).send(ALERT)
    path, body = slack.received[0]
    assert path == SECRET_PATH
    assert body["text"].startswith(":rotating_light: [CRITICAL] HUMAN_CONSENT_MISSING")
    att = body["attachments"][0]
    assert att["color"] == "#d00000"
    flat = json.dumps(att["blocks"])
    assert "loan-agent" in flat and "e1, e2" in flat and "SIMULATED test scenario: x" in flat


def test_user_text_is_escaped_so_it_cannot_inject_slack_markup(slack):
    SlackNotifier(slack.url, min_interval=0).send(ALERT)
    flat = json.dumps(slack.received[0][1]["attachments"][0]["blocks"])
    assert "&lt;approved&gt; &amp; more" in flat and "<approved>" not in flat


def test_slack_limits_are_respected(slack):
    long = Alert("ALR-8", "t", "HIGH", "R", "a", "", "x" * 5000, "y" * 9000, ["e"])
    SlackNotifier(slack.url, min_interval=0).send(long)
    for block in slack.received[0][1]["attachments"][0]["blocks"]:
        if block["type"] == "section" and "text" in block:
            assert len(block["text"]["text"]) <= 3000


def test_rate_limit_429_is_retried_once_after_the_wait():
    s = FakeSlack([(429, "rate_limited", {"Retry-After": "0"}), (200, "ok", {})])
    try:
        SlackNotifier(s.url, min_interval=0).send(ALERT)
        assert len(s.received) == 2
    finally:
        s.close()


@pytest.mark.parametrize("status,body", [(404, "no_service"), (403, "invalid_token"), (400, "invalid_payload")])
def test_slack_errors_are_reported_without_leaking_the_webhook_url(status, body):
    s = FakeSlack([(status, body, {})])
    try:
        with pytest.raises(RuntimeError) as err:
            SlackNotifier(s.url, min_interval=0).send(ALERT)
        msg = str(err.value)
        assert body in msg and str(status) in msg
        assert "SuperSecretToken123" not in msg and s.url not in msg
    finally:
        s.close()


def test_unexpected_reply_counts_as_failure():
    s = FakeSlack([(200, "<html>captive portal</html>", {})])
    try:
        with pytest.raises(RuntimeError, match="instead of 'ok'"):
            SlackNotifier(s.url, min_interval=0).send(ALERT)
    finally:
        s.close()


def test_unreachable_slack_is_a_clear_error_not_a_crash():
    with pytest.raises(RuntimeError, match="could not reach Slack") as err:
        SlackNotifier("http://127.0.0.1:1/services/T/B/Secret999", timeout=1, min_interval=0).send(ALERT)
    assert "Secret999" not in str(err.value)


def test_slack_down_does_not_stop_the_monitor(rig):
    rig.access("analytics-agent", "employee_salaries")
    rig.access("analytics-agent", "credit_scores", "write")
    m = Monitor(rig.policy, [SlackNotifier("http://127.0.0.1:1/x", timeout=1, min_interval=0)])
    m.scan(rig.log.read_all())
    assert len(m.alerts) == 2 and len(m.delivery_errors) == 2


def test_env_enables_slack_with_context(monkeypatch):
    monkeypatch.setenv("AGENT_GUARD_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    (n,) = notifiers_from_env("monitoring audit.jsonl")
    assert n.name == "slack" and n.context == "monitoring audit.jsonl"


def test_notify_test_command(monkeypatch, capsys):
    assert cli.main(["notify-test"]) == 2 and "nothing was sent" in capsys.readouterr().out
    s = FakeSlack()
    try:
        monkeypatch.setenv("AGENT_GUARD_SLACK_WEBHOOK_URL", s.url)
        assert cli.main(["notify-test"]) == 0
        assert "slack: sent" in capsys.readouterr().out
        assert "connection test" in json.dumps(s.received[0][1])
        s.script.append((404, "no_service", {}))
        assert cli.main(["notify-test"]) == 1
        out = capsys.readouterr().out
        assert "FAILED" in out and "SuperSecretToken123" not in out
    finally:
        s.close()
