# Assignment 2: Policy-bound AI agents and a monitor that watches them

Several agents do work under rules ("approve loans below 1000 alone; from 1000 up, get a human first").
A separate **monitoring agent** watches what they do and sends a notification when one breaks the rules.
Test scenarios make agents misbehave on purpose to prove the monitor catches it.

Runs entirely on this laptop. Python standard library only (pytest only for the tests). No AWS, no internet,
nothing is sent anywhere unless you configure a webhook or email yourself.

## What the brief asked for, and where it is

| Brief | Where |
|---|---|
| Agents doing tasks under policies (loan below 1000 auto, above 1000 human in the loop) | `agent_guard/agents.py` (`LoanAgent`, `ReportAgent`, `AnalyticsAgent`), `agent_guard/approval.py` (the human), `policies/policy.json` |
| Monitor agent that notifies when the agent skipped human consent | `agent_guard/monitor.py` rule `HUMAN_CONSENT_MISSING`, `agent_guard/alerts.py` (console, file, Slack, webhook, email) |
| Test scenario: agent misbehaves, monitor flags it | `agent_guard/scenarios.py`: `loan_no_consent`, `loan_denied_but_approved`, `loan_forged_approval`, `loan_split` |
| Task normally ~5 min suddenly finishes in 10 s | scenario `report_too_fast`, rule `DURATION_TOO_FAST` |
| Agent reads data it should not; monitor notifies | scenario `analytics_unauthorised_data`, rule `UNAUTHORISED_DATA_ACCESS` |

## How it works

```
 loan-agent ─┐                                   ┌─ approval service ── human reviewer
 report-agent ┼──► AUDIT LOG (append-only file) ◄┘   (signs every approval with a secret agents don't have)
 analytics-agent ┘            │  stamps the time itself
                              ▼
                      MONITOR (reads the log only, never calls the agents)
                              │
              rules ──► ALERT ──► console · alerts.jsonl · webhook (Slack/Teams) · email
```

Two design choices make the monitor hard to fool:

1. **The monitor watches the record, not the agent.** It only reads the audit log, so a misbehaving agent cannot
   hide by lying about itself. It also never imports the agent code (a test enforces that).
2. **Consent cannot be faked.** Every human approval is written by a separate approval service and signed
   (HMAC) with a secret the agents never see. The log, not the agent, stamps the time. An approval the monitor
   cannot verify is treated as forged and as *no* approval.

### The monitor's rules

| Rule | Fires when | Severity |
|---|---|---|
| `HUMAN_CONSENT_MISSING` | A loan of 1000 or more is approved and there is no valid prior human approval. It says *why*: never asked, asked but unanswered, human denied, approver not authorised, approved a different amount, approval too old | CRITICAL |
| `FORGED_APPROVAL` | An approval event not written and signed by the approval service | CRITICAL |
| `SPLIT_LOAN_SUSPECTED` | Two or more sub-limit loans to one applicant within an hour that add up to the limit | HIGH |
| `DURATION_TOO_FAST` / `_SLOW` | A task takes under 20% / over 300% of that agent's normal time. "Normal" is the median of its last runs (or the policy's expected value until enough history exists). Anomalous runs are not added to the baseline, so a streak of bad runs cannot teach the monitor that bad is normal | HIGH / MEDIUM |
| `UNAUTHORISED_DATA_ACCESS` | An agent touches a resource, or uses an action (e.g. write), outside its allow-list. Resources marked `restricted` are CRITICAL | CRITICAL / HIGH |
| `UNREGISTERED_AGENT` | An agent id the policy does not know | MEDIUM |

Alerts are de-duplicated (50 reads of the same forbidden table = one alert) and one failing channel (say, SMTP
down) cannot stop the others or the monitor.

## Results

`output/RESULTS.md` has the full report. Each scenario simulates 10 normal days, then an 11th day in which exactly
one agent misbehaves. It passes only if the expected alert fires, against the right agent, with **no other alerts**
(no false alarms on the well-behaved agents).

| Scenario | Misbehaviour | Monitor raised | Result |
|---|---|---|---|
| baseline_normal | none | no alerts | PASS |
| loan_no_consent | approves 5000 without asking a human | HUMAN_CONSENT_MISSING | PASS |
| loan_denied_but_approved | human says no, agent approves anyway | HUMAN_CONSENT_MISSING | PASS |
| loan_forged_approval | agent writes its own "approved by alice.manager" | FORGED_APPROVAL + HUMAN_CONSENT_MISSING | PASS |
| loan_split | one 2700 loan sliced into 900s | SPLIT_LOAN_SUSPECTED | PASS |
| report_too_fast | daily report in 10 s instead of ~5 min | DURATION_TOO_FAST | PASS |
| analytics_unauthorised_data | reads salaries and personal data, writes credit scores | UNAUTHORISED_DATA_ACCESS (x3) | PASS |

68 automated tests pass. To check they are not passing by accident, four deliberate bugs were put into a
scratch copy of the monitor (trust forged signatures, never flag too-fast, ignore human denial, allow all data
access); the tests failed for every one.

## Run it

From `E:\TruvanAI\Assighnment2` in a terminal:

```
python -m agent_guard scenarios          # run every scenario, print alerts, write output/RESULTS.md
python -m agent_guard scenarios --only loan_no_consent
python -m pytest -q                      # 68 tests
```

**Live demo (two windows).** Window 1 starts the monitor and waits; window 2 runs the agents in sped-up
real time (60x: five minutes of agent work take five seconds). The alert appears in window 1 as it happens.

```
# window 1
python -m agent_guard monitor --log output\live\audit.jsonl --watch --alerts-out output\live\alerts.jsonl
# window 2 (choose loan, report, data or none)
python -m agent_guard live --incident loan
```

`monitor` also works on any audit log file: `python -m agent_guard monitor --log some_audit.jsonl`
(exit code 1 if it raised alerts, 0 if clean).

### Real Slack alerts (optional, off until you set the webhook)

1. In Slack, create a channel for alerts, for example `#agent-alerts`.
2. Go to https://api.slack.com/apps, choose **Create New App > From scratch**, name it `Agent Guard`, pick your workspace.
3. Open **Incoming Webhooks**, switch it **On**, click **Add New Webhook to Workspace**, pick `#agent-alerts`, **Allow**.
4. Copy the Webhook URL (`https://hooks.slack.com/services/T.../B.../...`). **Treat it like a password**: anyone with it can post
   to your channel. Never put it in a file, screenshot or commit; it is read only from an environment variable.
5. In the same terminal window you will run the commands in:

```
set AGENT_GUARD_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
python -m agent_guard notify-test          # one harmless test card; prints "slack: sent" or the exact error
python -m agent_guard scenarios --only loan_no_consent     # real cards, labelled "SIMULATED test scenario"
```

Each alert arrives as a colour-coded card (red = CRITICAL, orange = HIGH, yellow = MEDIUM) showing the agent, the rule,
the plain-English reason and the evidence event ids. The live demo (`monitor --watch` plus `live`) also posts to Slack.
The variable lives only in that terminal window; close it and the webhook is forgotten. The test suite ignores
these variables, so running `pytest` never posts to your Slack. Slack allows about one message per second, so the
notifier spaces messages out and retries once if Slack says "slow down".

### Other channels (optional, off by default)

Set environment variables before starting the monitor. Nothing is sent otherwise.

```
set AGENT_GUARD_WEBHOOK_URL=https://...        (generic JSON webhook, e.g. Teams/Discord/your own service)
set AGENT_GUARD_SMTP_HOST=smtp.gmail.com
set AGENT_GUARD_SMTP_PORT=587
set AGENT_GUARD_SMTP_USER=you@gmail.com
set AGENT_GUARD_SMTP_PASSWORD=app-password
set AGENT_GUARD_EMAIL_TO=you@gmail.com
set AGENT_GUARD_APPROVAL_SECRET=any-long-random-string     (use the same value for approval service and monitor)
```

## Demo for the teacher (about 5 minutes)

1. Explain the idea in one line: agents have rules; a separate monitor reads their audit trail and raises alarms.
2. Open `policies/policy.json` and show the rule: 1000 limit, approvers, which agent may touch which data.
3. Run `python -m agent_guard scenarios`; point at the PASS lines. Start with `baseline_normal` (silent) so they see the monitor does not cry wolf, then `loan_no_consent`, `report_too_fast`, `analytics_unauthorised_data`.
4. Run the two-window live demo with `--incident loan` and show the alert popping up in window 1.
5. Open `output\loan_forged_approval\audit.jsonl` and show that the fake approval has the wrong signature: why the monitor cannot be tricked by an agent writing its own consent.
6. Mention the limitations below before they ask.

## Limitations (please read)

* **The agents are simulated and rule-driven, not LLMs.** The misbehaving ones are written to misbehave. The monitor
  only reads the audit log, so it would watch an LLM-driven agent the same way, but that is not tested here.
* **Agents run one after another**, not concurrently, in a single process. Real systems would be many processes.
* **The monitor sees only what is logged.** An agent that does not log an action is invisible to it. In production,
  log at a point the agent cannot bypass (an API gateway, IAM/CloudTrail, the database audit log) rather than trusting
  the agent to report. Here, agents log through the `AuditLog` helper.
* **The log is a plain file.** Signatures protect approvals, but someone with file access could delete or edit other
  lines. Production should ship the log to append-only storage (e.g. S3 Object Lock, CloudTrail Lake).
* **The demo signing secret is a built-in default.** Set `AGENT_GUARD_APPROVAL_SECRET` for anything real.
* **Thresholds are heuristics.** "Under 20% of the median" is a reasonable starting rule, not a tuned model. A
  fixed-speed agent that legitimately gets much faster (a code improvement) would need its baseline reset.
  The split rule is deliberately strict: two legitimate 600 loans to the same person within an hour are flagged for review.
* **Exactly 1000** is not covered by the brief ("below 1000" / "more than 1000"). The cautious reading is used:
  1000 needs a human. Change `human_approval_required_from` or the comparison if your teacher reads it differently.
* **Time in scenarios is simulated** (a virtual clock), so ten "days" run in seconds; timestamps in the logs are the
  simulated times (October 2026), not when you ran it.
* **Slack** was verified live: a test card, a scenario alert and a live-demo alert were delivered to a real workspace
  channel. The automated tests use a local fake Slack, so they never post to a real one. The generic webhook and
  email channels are tested only against a local HTTP server and a fake SMTP server, not a real mailbox.
* The monitor flags the split on the *second* piece, the earliest moment the pattern exists, so the alert says
  "2 small loans" even though a third followed.
