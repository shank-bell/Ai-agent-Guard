"""The alert object the monitor produces and the channels that deliver it."""
from __future__ import annotations

import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from pathlib import Path

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


@dataclass
class Alert:
    id: str
    ts: str                  # time of the event that triggered it (log time, not wall clock)
    severity: str
    rule: str
    agent_id: str
    task_id: str
    title: str
    detail: str
    evidence: list[str] = field(default_factory=list)     # ids of the audit events that prove it

    def summary(self) -> str:
        return f"[{self.severity}] {self.rule} | {self.agent_id} | {self.title}"


class Notifier:
    name = "notifier"

    def send(self, alert: Alert) -> None:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout

    def send(self, alert: Alert) -> None:
        print(f"  ALERT {alert.id} {alert.summary()}\n        {alert.detail}", file=self.stream)


class FileNotifier(Notifier):
    """Appends one JSON line per alert (an inbox other tools can tail)."""
    name = "file"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, alert: Alert) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(alert)) + "\n")


class WebhookNotifier(Notifier):
    """POSTs JSON. The `text` key makes it work with Slack/Teams/Discord-style incoming webhooks."""
    name = "webhook"

    def __init__(self, url: str, timeout: float = 5.0):
        self.url, self.timeout = url, timeout

    def send(self, alert: Alert) -> None:
        body = json.dumps({"text": f"{alert.summary()}\n{alert.detail}", "alert": asdict(alert)}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=self.timeout).close()


SLACK_COLOURS = {"CRITICAL": "#d00000", "HIGH": "#f77f00", "MEDIUM": "#fcbf49", "LOW": "#6c757d"}
SLACK_ICONS = {"CRITICAL": ":rotating_light:", "HIGH": ":warning:", "MEDIUM": ":large_yellow_circle:", "LOW": ":information_source:"}


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackNotifier(Notifier):
    """Posts to a Slack Incoming Webhook as a colour-coded card.

    The webhook URL is a secret (anyone holding it can post to your channel): it is never printed or put in errors.
    `context` is shown in the card footer, e.g. "simulated scenario: loan_no_consent", so test alerts are not
    mistaken for real incidents.
    """
    name = "slack"

    def __init__(self, url: str, timeout: float = 10.0, context: str = "", min_interval: float = 1.1):
        self.url, self.timeout, self.context, self.min_interval = url, timeout, context, min_interval
        self._last_sent = 0.0                           # Slack allows about one message per second per webhook

    def payload(self, alert: Alert) -> dict:
        icon = SLACK_ICONS.get(alert.severity, ":bell:")
        fields = [("Agent", alert.agent_id), ("Rule", alert.rule), ("Task", alert.task_id or "-"),
                  ("Log time", alert.ts), ("Alert id", alert.id)]
        footer = f"Evidence events: {', '.join(alert.evidence)}"
        if self.context:
            footer += f"  |  {self.context}"
        return {
            "text": f"{icon} [{alert.severity}] {alert.rule}: {alert.title}",      # notification/preview text
            "attachments": [{
                "color": SLACK_COLOURS.get(alert.severity, "#6c757d"),
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*{icon} {alert.severity} | {_slack_escape(alert.title)}*"[:2900]}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": _slack_escape(alert.detail)[:2900]}},
                    {"type": "section", "fields": [{"type": "mrkdwn", "text": f"*{k}*\n{_slack_escape(v)}"}
                                                   for k, v in fields]},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": _slack_escape(footer)[:2900]}]},
                ]}],
        }

    def send(self, alert: Alert) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_sent)
        if wait > 0 and self._last_sent:
            time.sleep(wait)
        body = json.dumps(self.payload(alert)).encode()
        for attempt in (1, 2):
            req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    reply = resp.read().decode("utf-8", "replace").strip()
                self._last_sent = time.monotonic()
                if reply != "ok":
                    raise RuntimeError(f"Slack answered '{reply[:80]}' instead of 'ok'")
                return
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt == 1:                      # rate limited: wait as told, once
                    time.sleep(min(float(exc.headers.get("Retry-After", "1") or 1), 5.0))
                    continue
                reply = exc.read().decode("utf-8", "replace").strip()[:80]
                raise RuntimeError(f"Slack rejected the message (HTTP {exc.code}: {reply}). "
                                   f"Check the webhook URL is complete, current and for the right workspace.") from None
            except urllib.error.URLError as exc:
                raise RuntimeError(f"could not reach Slack ({exc.reason})") from None


class EmailNotifier(Notifier):
    name = "email"

    def __init__(self, host: str, port: int, sender: str, recipients: list[str],
                 user: str | None = None, password: str | None = None, starttls: bool = True):
        self.host, self.port, self.sender, self.recipients = host, port, sender, recipients
        self.user, self.password, self.starttls = user, password, starttls

    def send(self, alert: Alert) -> None:
        msg = EmailMessage()
        msg["Subject"] = alert.summary()
        msg["From"], msg["To"] = self.sender, ", ".join(self.recipients)
        msg.set_content(f"{alert.detail}\n\nAlert {alert.id} at {alert.ts}\nTask: {alert.task_id}\n"
                        f"Evidence events: {', '.join(alert.evidence)}\n")
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password or "")
            smtp.send_message(msg)


def notifiers_from_env(context: str = "") -> list[Notifier]:
    """Optional real channels. Nothing is sent anywhere unless these variables are set."""
    out: list[Notifier] = []
    if os.environ.get("AGENT_GUARD_SLACK_WEBHOOK_URL"):
        out.append(SlackNotifier(os.environ["AGENT_GUARD_SLACK_WEBHOOK_URL"], context=context))
    if os.environ.get("AGENT_GUARD_WEBHOOK_URL"):
        out.append(WebhookNotifier(os.environ["AGENT_GUARD_WEBHOOK_URL"]))
    if os.environ.get("AGENT_GUARD_SMTP_HOST") and os.environ.get("AGENT_GUARD_EMAIL_TO"):
        out.append(EmailNotifier(
            host=os.environ["AGENT_GUARD_SMTP_HOST"],
            port=int(os.environ.get("AGENT_GUARD_SMTP_PORT", "587")),
            sender=os.environ.get("AGENT_GUARD_EMAIL_FROM", os.environ.get("AGENT_GUARD_SMTP_USER", "agent-guard@localhost")),
            recipients=[a.strip() for a in os.environ["AGENT_GUARD_EMAIL_TO"].split(",") if a.strip()],
            user=os.environ.get("AGENT_GUARD_SMTP_USER"),
            password=os.environ.get("AGENT_GUARD_SMTP_PASSWORD"),
            starttls=os.environ.get("AGENT_GUARD_SMTP_STARTTLS", "1") != "0"))
    return out
