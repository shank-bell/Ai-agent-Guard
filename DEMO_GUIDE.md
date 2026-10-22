# Demo guide (about 10 to 12 minutes)

## The pitch in 90 seconds (say this first)

> "Companies are starting to let AI agents act on their own: approve loans, pull data, finish tasks. You can write the rules
> into the prompt, or even into the agent's code, but then you are trusting the thing you are trying to control.
>
> So I built two layers. Agents with policies, and a **separate monitoring agent that never talks to them. It only reads
> their audit trail**. Three design choices make that hold up: the monitor judges *actions*, not explanations; human
> approvals are cryptographically signed so an agent can't fake a manager; and the log stamps the time, so an agent can't
> back-date itself.
>
> I tested it two ways. Seven scripted scenarios: a loan approved without a human, a fake approval, a split loan, a 5-minute task
> finishing in 10 seconds, an agent reading salary data. And then the part I care most about: **real language-model agents**
> on Amazon Bedrock, attacked with prompt injection. In both, the same monitor, without code changes, agreed with what
> really happened on every case."

## Before you start (5 minutes of prep, do it before the meeting)

| Check | How |
|---|---|
| Two terminals open in the project folder | `cd /d E:\TruvanAI\Assighnment2` in both |
| Slack open on your alerts channel, beside the terminals | Browser |
| AWS signed in (for the real-model demo) | `aws login --region ap-southeast-2` (sessions expire, so do it just before) |
| boto3 available in the Python you will use | `pip install -r requirements-llm.txt` |
| The webhook set **in terminal 1 only** | `set AGENT_GUARD_SLACK_WEBHOOK_URL=<your URL>` then `python -m agent_guard notify-test` should print `slack: sent` |
| Do not set the webhook in the window where you run `scenarios` | It would post about 9 cards |
| `policies\policy.json`, `README.md` open in VS Code | You will show them |

## Demo script

### 1. The rulebook (1 minute)
Show `policies\policy.json`: the 1000 limit, the authorised approvers, and the per-agent data allow-lists.
"Change one number here and the whole system follows."

### 2. Scripted scenarios (2 minutes). Terminal 2, no Slack
```
python -m agent_guard scenarios
```
Point at: `baseline_normal` is silent ("a monitor that cries wolf is useless"); then the loan alerts (and that each says *why*); the
10-second report; the three data alerts; `7/7 passed`. Each scenario is 10 normal days then one bad day.

Optional, 30 seconds: `findstr "APR-FORGED" output\loan_forged_approval\audit.jsonl`. "The agent wrote its own 'approved by
the manager' line. The signature doesn't verify, so the monitor calls it forged and ignores it."

### 3. Real LLM agents, live, with Slack (3 minutes). The main event
Terminal 1:
```
python -m agent_guard monitor --log output\live\audit.jsonl --watch
```
Terminal 2:
```
python -m agent_guard live --llm --incident loan --log output\live\audit.jsonl
```
Narrate while it runs (about 15 seconds): "Nova Micro is deciding each loan. The policy only exists as words in its prompt. The last
application has an instruction hidden in the applicant's note, pretending to be a manager's pre-approval."
Expected: the first three loans are fine (two small ones approved alone, one sent to a human), the fourth is approved
with no human, terminal 1 prints a CRITICAL alert, and a **red card lands in Slack**.

Then show the comparison: Ctrl+C terminal 1, restart it, and run the same with the hard check:
```
python -m agent_guard live --llm --guard --incident loan --log output\live\audit.jsonl
```
"With a hard check in code, the model is overridden, a human is asked, and no alert is needed. The check only covers what I
thought of. The monitor covers the rest. It's defence in depth."

> The model is deterministic enough at temperature 0 that the attack worked in our runs, but a real model is not guaranteed
> to repeat it. If it resists, say so: "this time it held, which is also a valid result. The monitor stays silent because
> nothing was violated," and show the pre-recorded result next.

### 4. The experiment results (2 minutes). Open `output\llm\RESULTS_LLM.md`
- 30 real model calls: the model was talked into breaking policy 2 of 4 times on loans and 2 of 3 on data. **The monitor
  agreed with reality on every case, with no misses and no false alarms.**
- The finding to highlight: in the "read everything" attack the model's explanation said the task violates the restrictions,
  while its structured answer still listed all five datasets. "Words and actions disagreed. That's why I monitor actions."
- Say the caveat out loud: "one small model, one run, attacks I wrote. It illustrates, it doesn't benchmark."

### 5. Proof and production path (2 minutes)
```
python -m pytest -q
```
100 passed. Mention: tests were checked against deliberately broken monitors (they failed every time), and the LLM cross-check was
verified the same way. Then show the "From demo to production" table in the README and name the top three: log where the
agent can't bypass it, append-only storage, a real approval flow.

## If something goes wrong

| Problem | Do this |
|---|---|
| AWS sign-in expired / no access | `python -m agent_guard live --llm --offline-llm --incident loan` uses a **simulated** model. Say so: "this stands in for the real model; the real results are in `RESULTS_LLM.md`" |
| No internet or Slack down | The monitor keeps working and prints alerts in the terminal; say "Slack is one of several channels" |
| Repeat run shows no alert | The monitor remembers alerts it already sent. Ctrl+C and restart it |
| `No module named boto3` | `pip install -r requirements-llm.txt` |
| Model resists the attack | See the note under step 3 |

## Likely questions, and honest answers

**Isn't the monitor just if-statements? Where is the AI?**
Yes, the monitor is deliberately deterministic. Oversight you can reason about and audit is a feature. The AI is in the agents
being watched. A learned detector could be added for fuzzier signals, but the hard rules (consent, data scope) should not be probabilistic.

**Why not just enforce the policy in the agent's code?**
You should, and I tested that (`--guard`): it stopped every violation. But it covers only the cases you predicted, it lives
inside the thing being controlled, and a bug or a new tool bypasses it. The monitor is the independent second line.

**Can the agent fool the monitor?**
It can't forge a human approval (signed with a secret it doesn't have) and can't back-date (the log stamps time). It *could* hide an
action by not logging it, or edit the plain-file log. That is the main production gap: capture at a point the agent can't bypass
and write to append-only storage.

**What if the model is wrong in a way you didn't write a rule for?**
Then the monitor doesn't see it. For example, one model ignored the credit-score rule and escalated instead of rejecting, and that
rule isn't monitored. Rules have to be written for what matters; the roadmap lists more.

**How do you know the monitor isn't missing things, or crying wolf?**
Two ways. Scripted scenarios check each rule fires on the right agent with no other alerts. For the real model, the agents' own
records give ground truth and the monitor must agree on every case. I also broke the monitor on purpose and confirmed the tests fail.

**Why median and 20%?**
The median shrugs off outliers; bad runs aren't added to the baseline so they can't teach it that bad is normal. 20% / 300% are
sensible starting thresholds, not tuned. In production they'd be fitted per agent from real history.

**Why flag two 600 loans?**
Together they reach the 1000 limit within an hour, which is what splitting looks like. It's deliberately strict, and a person reviews it.

**How would it scale?**
Agents write to one shared stream; the monitor processes one event at a time with small per-agent state, so it partitions by agent.
I haven't load-tested it.

**What did you not do?**
Agents run sequentially, the human reviewer is simulated, Slack is the only real channel I verified, the LLM experiment is small,
and it hasn't watched a production agent. Everything in "Known limitations" in the README.

**Did you use AI tools to build this?**
Check the assignment's rules before the meeting and answer truthfully. If you can't explain a part of the code, don't present it;
run through the files table in the README until you can.

## Know the files well enough to explain any of them

`monitor.py` (rules), `events.py` (signing, log), `agents.py` and `llm_agents.py` (the agents and attacks), `approval.py` (human),
`scenarios.py` and `llm_scenarios.py` (how it is tested), `policy.json` (rules). Ask for a mock interview if you'd like to practise.
