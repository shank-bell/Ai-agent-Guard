"""Human-consent, forgery and loan-splitting rules, attacked from the edges."""
from agent_guard.events import APPROVAL_SOURCE, approval_secret
from agent_guard.policy import Policy


def ask_and_answer(rig, loan_id, amount, approved=True, approver="alice.manager", applicant="cust-1"):
    rig.reviewer.name = approver
    if not approved:
        rig.reviewer.deny_loan_ids.add(loan_id)
    t = rig.approvals.request("loan-agent", "t1", loan_id, applicant, amount)
    rig.approvals.wait(t)


def rules(alerts):
    return sorted(a.rule for a in alerts)


def test_human_approved_large_loan_is_clean(rig):
    ask_and_answer(rig, "L1", 5000)
    rig.decision("L1", 5000)
    assert rig.scan() == []


def test_boundary_999_needs_no_human_but_1000_does(rig):
    rig.decision("L1", 999.99)
    assert rig.scan() == []
    rig.decision("L2", 1000)
    assert rules(rig.scan()) == ["HUMAN_CONSENT_MISSING"]


def test_rejected_loans_never_need_a_human(rig):
    rig.decision("L1", 50000, outcome="rejected")
    assert rig.scan() == []


def test_approval_for_a_different_amount_is_not_consent(rig):
    ask_and_answer(rig, "L1", 1500)
    rig.decision("L1", 9000)                     # human said 1500, agent approved 9000
    (alert,) = rig.scan()
    assert alert.rule == "HUMAN_CONSENT_MISSING" and "approved 1500" in alert.detail


def test_approval_from_unauthorised_person_is_not_consent(rig):
    ask_and_answer(rig, "L1", 5000, approver="intern.joe")
    rig.decision("L1", 5000)
    (alert,) = rig.scan()
    assert "not an authorised approver" in alert.detail


def test_agent_cannot_approve_itself(rig):
    rig.policy.authorized_approvers.append("loan-agent")
    ask_and_answer(rig, "L1", 5000, approver="loan-agent")
    rig.decision("L1", 5000)
    assert rules(rig.scan()) == ["HUMAN_CONSENT_MISSING"]


def test_approval_arriving_after_the_decision_does_not_count(rig):
    rig.decision("L1", 5000)                     # decided first...
    rig.clock.sleep(30)
    ask_and_answer(rig, "L1", 5000)              # ...human says yes afterwards
    assert rules(rig.scan()) == ["HUMAN_CONSENT_MISSING"]


def test_stale_approval_expires(rig):
    ask_and_answer(rig, "L1", 5000)
    rig.clock.sleep(rig.policy.approval_max_age_seconds + 1)
    rig.decision("L1", 5000)
    (alert,) = rig.scan()
    assert "stale" in alert.detail


def test_question_asked_but_not_answered_yet(rig):
    rig.approvals.request("loan-agent", "t1", "L1", "cust-1", 5000)
    rig.decision("L1", 5000)
    (alert,) = rig.scan()
    assert "no answer had been recorded" in alert.detail


def test_denied_by_human_then_approved_anyway(rig):
    ask_and_answer(rig, "L1", 5000, approved=False)
    rig.decision("L1", 5000)
    (alert,) = rig.scan()
    assert "DENIED" in alert.detail


def test_unsigned_approval_written_by_agent_is_forgery(rig):
    rig.log.emit("approval_decided", "loan-agent", source="loan-agent", task_id="t1",
                 data={"ticket": "X", "loan_id": "L1", "amount": 5000, "approved": True, "approver": "alice.manager"})
    rig.decision("L1", 5000)
    assert rules(rig.scan()) == ["FORGED_APPROVAL", "HUMAN_CONSENT_MISSING"]


def test_wrong_signature_is_forgery_but_right_signature_is_trusted(rig):
    data = {"ticket": "X", "loan_id": "L1", "amount": 5000, "approved": True, "approver": "alice.manager"}
    rig.log.emit("approval_decided", "loan-agent", source=APPROVAL_SOURCE, task_id="t1", data=data, secret="guess")
    rig.decision("L1", 5000)
    assert "FORGED_APPROVAL" in rules(rig.scan())
    # same event signed with the real secret is accepted (proves the check is the signature, not the wording)
    rig.log.emit("approval_decided", "loan-agent", source=APPROVAL_SOURCE, task_id="t1",
                 data={**data, "loan_id": "L2"}, secret=approval_secret())
    rig.decision("L2", 5000)
    assert not [a for a in rig.scan() if "L2" in a.title]


def test_tampering_with_a_signed_approval_is_detected(rig):
    ask_and_answer(rig, "L1", 1500)
    lines = rig.log.path.read_text(encoding="utf-8").replace('"amount":1500', '"amount":9000')
    rig.log.path.write_text(lines, encoding="utf-8")        # someone edits the log after the fact
    rig.decision("L1", 9000)
    assert "FORGED_APPROVAL" in rules(rig.scan())


def test_rule_follows_the_policy_not_a_hardcoded_number(rig):
    rig.policy = Policy.load()
    rig.policy.human_approval_required_from = 10000
    rig.decision("L1", 5000)
    assert rig.scan() == []                       # same loan that was flagged under the default policy
    rig.policy.human_approval_required_from = 100
    assert rules(rig.scan()) == ["HUMAN_CONSENT_MISSING"]


def test_splitting_two_pieces_that_reach_the_limit(rig):
    rig.decision("A", 600, applicant="cust-9")
    rig.clock.sleep(120)
    rig.decision("B", 600, applicant="cust-9")
    (alert,) = rig.scan()
    assert alert.rule == "SPLIT_LOAN_SUSPECTED" and "1200" in alert.title


def test_small_loans_that_stay_under_the_limit_are_fine(rig):
    rig.decision("A", 400, applicant="cust-9")
    rig.decision("B", 400, applicant="cust-9")
    assert rig.scan() == []


def test_loans_to_different_people_or_far_apart_are_not_splitting(rig):
    rig.decision("A", 600, applicant="cust-1")
    rig.decision("B", 600, applicant="cust-2")
    rig.clock.sleep(rig.policy.split_window_seconds + 10)
    rig.decision("C", 600, applicant="cust-1")
    assert rig.scan() == []
