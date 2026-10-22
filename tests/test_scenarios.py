"""End to end: simulate the fleet, let the monitor scan the finished log, check it caught exactly the right thing."""
import json

import pytest

from agent_guard.policy import Policy
from agent_guard.scenarios import SCENARIOS, run_scenario, write_results


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    out = tmp_path_factory.mktemp("scn")
    res = [run_scenario(s, out) for s in SCENARIOS]
    return out, res


def test_every_scenario_passes(results):
    _, res = results
    assert [(r.scenario.name, r.problems) for r in res if not r.passed] == []


def test_normal_day_raises_no_false_alarms(results):
    _, res = results
    base = next(r for r in res if r.scenario.name == "baseline_normal")
    assert base.alerts == [] and base.events > 100


def test_alerts_are_written_to_the_notification_file(results):
    out, res = results
    for r in res:
        path = out / r.scenario.name / "alerts.jsonl"
        rows = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        assert [x["rule"] for x in rows] == [a.rule for a in r.alerts]


def test_incident_alerts_point_at_the_incident_day(results):
    _, res = results
    for r in res:
        for a in r.alerts:
            assert a.ts.startswith("2026-10-11")           # day 11 of the simulation


def test_results_report_is_written(results):
    out, res = results
    text = write_results(res, out).read_text(encoding="utf-8")
    assert "7 of 7 scenarios passed" in text and "report-agent" in text


def test_a_monitor_with_the_wrong_policy_fails_the_scenario(tmp_path):
    """Guard against tests that pass for the wrong reason: loosen the policy and the catch must disappear."""
    policy = Policy.load()
    sc = next(s for s in SCENARIOS if s.name == "loan_no_consent")
    policy.human_approval_required_from = 1_000_000
    res = run_scenario(sc, tmp_path, policy)
    assert not res.passed and res.alerts == []
