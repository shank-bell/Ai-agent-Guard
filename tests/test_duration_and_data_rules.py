"""Duration-anomaly, data-access and registry rules."""
from agent_guard.alerts import Notifier


def rules(alerts):
    return sorted(a.rule for a in alerts)


def normal_runs(rig, n=10, seconds=300):
    for i in range(n):
        rig.task("report-agent", "daily_report", seconds + (i % 3) * 10)


def test_ten_seconds_against_five_minutes_is_flagged(rig):
    normal_runs(rig)
    rig.task("report-agent", "daily_report", 10)
    (alert,) = rig.scan()
    assert alert.rule == "DURATION_TOO_FAST" and alert.agent_id == "report-agent"


def test_normal_variation_is_not_flagged(rig):
    normal_runs(rig)
    rig.task("report-agent", "daily_report", 240)
    rig.task("report-agent", "daily_report", 390)
    assert rig.scan() == []


def test_unusually_slow_run_is_flagged_as_medium(rig):
    normal_runs(rig)
    rig.task("report-agent", "daily_report", 1500)
    (alert,) = rig.scan()
    assert (alert.rule, alert.severity) == ("DURATION_TOO_SLOW", "MEDIUM")


def test_policy_seed_catches_the_very_first_run(rig):
    rig.task("report-agent", "daily_report", 10)       # no history yet, policy says ~300s
    assert rules(rig.scan()) == ["DURATION_TOO_FAST"]


def test_agent_without_seed_learns_before_judging(rig):
    rig.task("analytics-agent", "campaign_analysis", 10)    # nothing known yet: only learns
    assert rig.scan() == []


def test_anomalies_do_not_poison_the_baseline(rig):
    normal_runs(rig)
    for _ in range(8):                                  # a long streak of rushed runs
        rig.task("report-agent", "daily_report", 10)
    got = rig.scan()
    assert rules(got) == ["DURATION_TOO_FAST"] * 8      # still flagged every time, baseline did not drift to 10s


def test_failed_tasks_are_ignored(rig):
    normal_runs(rig)
    rig.task("report-agent", "daily_report", 5, status="failed")
    assert rig.scan() == []


def test_agents_that_opt_out_of_duration_monitoring_are_not_checked(rig):
    for _ in range(10):
        rig.task("loan-agent", "loan_decision", 600)
    rig.task("loan-agent", "loan_decision", 5)
    assert rig.scan() == []


def test_each_task_type_has_its_own_baseline(rig):
    normal_runs(rig)
    for _ in range(6):
        rig.task("report-agent", "weekly_summary", 20)
    rig.task("report-agent", "weekly_summary", 20)
    assert rig.scan() == []


def test_allowed_reads_are_fine(rig):
    rig.access("analytics-agent", "marketing_stats")
    rig.access("report-agent", "reports", "write")
    assert rig.scan() == []


def test_restricted_data_is_critical_and_other_data_is_high(rig):
    rig.access("analytics-agent", "employee_salaries")
    rig.access("analytics-agent", "credit_scores")
    got = {a.title.split("'")[1]: a.severity for a in rig.scan()}
    assert got == {"employee_salaries": "CRITICAL", "credit_scores": "HIGH"}


def test_write_to_read_only_resource_is_flagged(rig):
    rig.access("analytics-agent", "loan_applications", "write")
    assert rules(rig.scan()) == ["UNAUTHORISED_DATA_ACCESS"]


def test_repeated_access_in_one_task_alerts_once(rig):
    for _ in range(50):
        rig.access("analytics-agent", "employee_salaries")
    assert len(rig.scan()) == 1


def test_unknown_agent_is_reported_once(rig):
    for _ in range(3):
        rig.access("shadow-agent", "anything")
    assert rules(rig.scan()) == ["UNREGISTERED_AGENT"]


def test_a_broken_notification_channel_does_not_stop_the_monitor(rig):
    class Broken(Notifier):
        name = "broken"

        def send(self, alert):
            raise RuntimeError("smtp down")

    sent = []

    class Good(Notifier):
        name = "good"

        def send(self, alert):
            sent.append(alert.rule)

    rig.access("analytics-agent", "employee_salaries")
    m = rig.monitor(notifiers=[Broken(), Good()])
    m.scan(rig.log.read_all())
    assert sent == ["UNAUTHORISED_DATA_ACCESS"] and "broken: smtp down" in m.delivery_errors[0]
