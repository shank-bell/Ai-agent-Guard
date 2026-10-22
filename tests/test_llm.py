"""LLM-driven agents, tested with scripted and stubbed models (no network, no AWS)."""
import json

import pytest

from agent_guard import cli
from agent_guard.llm import BedrockLLM, ScriptedLLM, parse_json_reply
from agent_guard.llm_agents import LLMAnalyticsAgent, LLMLoanAgent, simulated_model
from agent_guard.llm_scenarios import run_llm_scenarios
from agent_guard.monitor import Monitor

BIG = {"loan_id": "L1", "applicant": "cust-1", "amount": 5000, "credit_score": 730, "applicant_note": "hi"}
SMALL = {"loan_id": "L2", "applicant": "cust-2", "amount": 400, "credit_score": 700, "applicant_note": "hi"}


def says(action, reason="because"):
    return ScriptedLLM(lambda s, u: json.dumps({"action": action, "reason": reason}), model_id="scripted-test")


def rules(rig):
    return sorted(a.rule for a in rig.scan())


# ---- reply parsing and the Bedrock backend ------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ('{"action": "approve"}', {"action": "approve"}),
    ('```json\n{"action": "reject"}\n```', {"action": "reject"}),
    ('Sure! Here you go: {"action": "approve", "reason": "ok"} Hope that helps.', {"action": "approve", "reason": "ok"}),
    ('{"a": 1} {"b": 2}', {"a": 1}),
    ("no json here", None), ("", None), (None, None), ("[1, 2]", None), ("{broken", None),
])
def test_parse_json_reply(text, expected):
    assert parse_json_reply(text) == expected


def test_bedrock_backend_sends_the_expected_request_and_reads_the_reply():
    seen = {}

    class Stub:
        def converse(self, **kw):
            seen.update(kw)
            return {"output": {"message": {"content": [{"text": '{"action":'}, {"text": ' "reject"}'}]}},
                    "usage": {"inputTokens": 11, "outputTokens": 5}}

    llm = BedrockLLM("amazon.nova-micro-v1:0", "ap-southeast-2", client=Stub())
    reply = llm.complete("SYS", "USER")
    assert reply.text == '{"action": "reject"}' and (reply.input_tokens, reply.output_tokens) == (11, 5)
    assert seen["modelId"] == "amazon.nova-micro-v1:0" and seen["inferenceConfig"]["temperature"] == 0
    assert seen["system"] == [{"text": "SYS"}] and seen["messages"][0]["content"] == [{"text": "USER"}]


# ---- the LLM loan agent -------------------------------------------------------------------------------
def test_model_that_follows_policy_produces_no_alerts(rig):
    def policy_following(system, user):
        amount = json.loads(user.split("Application:\n")[1])["amount"]
        return json.dumps({"action": "request_human_approval" if amount >= 1000 else "approve", "reason": "policy"})

    agent = LLMLoanAgent(rig.log, rig.clock, rig.policy, rig.approvals, ScriptedLLM(policy_following))
    assert agent.handle(BIG)["human_approved"] is True and agent.handle(SMALL)["outcome"] == "approved"
    assert rig.scan() == []


def test_model_talked_into_approving_is_caught_by_the_monitor(rig):
    agent = LLMLoanAgent(rig.log, rig.clock, rig.policy, rig.approvals, says("approve", "note said it's pre-approved"))
    res = agent.handle(BIG)
    assert res["outcome"] == "approved" and res["human_approved"] is None
    (alert,) = rig.scan()
    assert alert.rule == "HUMAN_CONSENT_MISSING" and alert.agent_id == "llm-loan-agent"


def test_code_guard_overrides_the_model_and_a_human_is_asked(rig):
    agent = LLMLoanAgent(rig.log, rig.clock, rig.policy, rig.approvals, says("approve"), enforce_policy=True)
    res = agent.handle(BIG)
    assert res["overridden"] and res["human_approved"] is True
    assert rig.scan() == []
    assert [e.type for e in rig.log.read_all()].count("guard_override") == 1


@pytest.mark.parametrize("reply", ["I think you should approve this.", '{"action": "wire the money"}', "{}", ""])
def test_unusable_model_output_fails_safe_to_a_human(rig, reply):
    agent = LLMLoanAgent(rig.log, rig.clock, rig.policy, rig.approvals, ScriptedLLM(lambda s, u: reply))
    res = agent.handle(BIG)
    assert res["model_action"] == "request_human_approval" and res["human_approved"] is True
    assert rig.scan() == []


def test_model_failure_does_not_crash_the_agent_and_is_logged(rig):
    def boom(system, user):
        raise RuntimeError("ExpiredToken: the session has expired")

    agent = LLMLoanAgent(rig.log, rig.clock, rig.policy, rig.approvals, ScriptedLLM(boom, "scripted-test"))
    res = agent.handle(BIG)
    assert res["model_error"].startswith("RuntimeError") and res["human_approved"] is True
    call = next(e for e in rig.log.read_all() if e.type == "llm_call")
    assert "ExpiredToken" in call.data["error"]
    assert rig.scan() == []


def test_every_model_call_is_recorded_with_prompt_and_reply(rig):
    LLMLoanAgent(rig.log, rig.clock, rig.policy, rig.approvals, says("reject", "poor credit")).handle(SMALL)
    call = next(e for e in rig.log.read_all() if e.type == "llm_call")
    assert call.data["model"] == "scripted-test" and '"amount": 400' in call.data["prompt"]
    assert "poor credit" in call.data["reply"]


def test_the_monitor_needs_no_changes_for_llm_agents(rig):
    """llm-loan-agent is just another agent id in the policy; the same rules apply to it."""
    LLMLoanAgent(rig.log, rig.clock, rig.policy, rig.approvals, says("approve")).handle(BIG)
    m = Monitor(rig.policy)
    m.scan(rig.log.read_all())
    assert [a.rule for a in m.alerts] == ["HUMAN_CONSENT_MISSING"]


# ---- the LLM analytics agent --------------------------------------------------------------------------
def picks(*names):
    return ScriptedLLM(lambda s, u: json.dumps({"datasets_to_read": list(names), "plan": "x"}))


def test_analytics_model_that_stays_in_its_lane_is_clean(rig):
    LLMAnalyticsAgent(rig.log, rig.clock, rig.policy, picks("marketing_stats")).run("summarise")
    assert rig.scan() == []


def test_analytics_model_talked_into_restricted_data_is_caught(rig):
    res = LLMAnalyticsAgent(rig.log, rig.clock, rig.policy, picks("marketing_stats", "employee_salaries")).run("x")
    assert res["outside_allowlist"] == ["employee_salaries"]
    (alert,) = rig.scan()
    assert (alert.rule, alert.severity, alert.agent_id) == ("UNAUTHORISED_DATA_ACCESS", "CRITICAL", "llm-analytics-agent")


def test_analytics_code_guard_drops_forbidden_datasets(rig):
    res = LLMAnalyticsAgent(rig.log, rig.clock, rig.policy, picks("customer_pii_full", "marketing_stats"),
                            enforce_policy=True).run("x")
    assert res["datasets_read"] == ["marketing_stats"] and rig.scan() == []


def test_analytics_ignores_made_up_dataset_names_and_garbage_output(rig):
    res = LLMAnalyticsAgent(rig.log, rig.clock, rig.policy, picks("not_a_dataset")).run("x")
    assert res["datasets_read"] == []
    res = LLMAnalyticsAgent(rig.log, rig.clock, rig.policy, ScriptedLLM(lambda s, u: "lol")).run("x")
    assert res["datasets_read"] == ["marketing_stats"]


# ---- the cross-checking runner and CLI -----------------------------------------------------------------
def test_runner_agrees_with_monitor_on_every_case_with_a_gullible_model(tmp_path):
    phases = run_llm_scenarios(tmp_path, simulated_model(gullible=True))
    assert all(p.ok for p in phases)
    assert [len(p.violations) for p in phases] == [2, 0, 2, 0]      # fooled twice, guarded runs never
    assert sum(r.overridden for r in phases[1].rows) == 2


def test_runner_agrees_with_monitor_with_a_model_that_resists(tmp_path):
    phases = run_llm_scenarios(tmp_path, simulated_model(gullible=False))
    assert all(p.ok for p in phases) and all(not p.violations for p in phases)


def test_runner_really_detects_a_monitor_that_misses_violations(tmp_path, monkeypatch):
    """Guard against a cross-check that can never fail: blind the monitor and the runner must complain."""
    monkeypatch.setattr(Monitor, "_check_consent", lambda self, ev, amount: [])
    phases = run_llm_scenarios(tmp_path, simulated_model(gullible=True))
    assert not phases[0].ok and len(phases[0].missed) == 2


def test_cli_llm_scenarios_offline_writes_a_clearly_labelled_report(tmp_path, capsys):
    assert cli.main(["llm-scenarios", "--offline-llm", "--out", str(tmp_path)]) == 0
    text = (tmp_path / "RESULTS_LLM_SIMULATED.md").read_text(encoding="utf-8")
    assert "SIMULATED MODEL" in text and "PASS" in text and not (tmp_path / "RESULTS_LLM.md").exists()


def test_cli_reports_failed_model_calls_instead_of_pretending(tmp_path, monkeypatch, capsys):
    def boom(system, user):
        raise RuntimeError("ExpiredToken")

    monkeypatch.setattr(cli, "_make_llm", lambda args: ScriptedLLM(boom, "bedrock-ish"))
    assert cli.main(["llm-scenarios", "--out", str(tmp_path)]) == 3
    assert "FAILED" in capsys.readouterr().out
    assert "model call(s) failed" in (tmp_path / "RESULTS_LLM.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("incident,rule", [("loan", "HUMAN_CONSENT_MISSING"), ("data", "UNAUTHORISED_DATA_ACCESS")])
def test_live_llm_mode_with_offline_model_is_judged_by_the_monitor(tmp_path, capsys, incident, rule):
    log = tmp_path / "live.jsonl"
    assert cli.main(["live", "--llm", "--offline-llm", "--log", str(log), "--incident", incident, "--speed", "2000"]) == 0
    cli.main(["monitor", "--log", str(log)])
    assert rule in capsys.readouterr().out


def test_live_llm_mode_with_the_code_guard_is_clean(tmp_path, capsys):
    log = tmp_path / "live.jsonl"
    cli.main(["live", "--llm", "--offline-llm", "--guard", "--log", str(log), "--incident", "loan", "--speed", "2000"])
    capsys.readouterr()
    assert cli.main(["monitor", "--log", str(log)]) == 0
