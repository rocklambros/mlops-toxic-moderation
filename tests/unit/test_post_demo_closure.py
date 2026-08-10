"""H15 and H13. Two accepted risks rest on controls nobody owned. This is the owner.

`docs/tls-decision.md` accepts cleartext HTTP, and `MODEL_CARD.md` accepts white-box
evasion. Both acceptances name compensating controls -- close `demo_cidrs`, rotate the
reviewer shared secret, rotate the demo API key, keep the rate limit in force -- and until
now none of the four had an owner, a target or a test. An accepted risk whose compensating
controls are unverified is an unaccepted risk with better prose.

CHANGED FROM THE PLAN, deliberately, and this is the whole design of the file.

The plan asserted every control is `closed` before submission. Applied today that would be a
lie: the demo window is deliberately open, with no scheduled close, so a grader can reach
the stack. A suite that demanded "closed" would have been satisfied by editing a YAML value,
which is precisely the failure mode -- a checklist that certifies itself.

So the tripwire is keyed to the actual state of the world instead. While
`infra/terraform/demo.auto.tfvars` exists, the window is open and the manifest must SAY it
is open, with an owner and a procedure. The moment that file is deleted, the window is
closed and every control must be recorded closed with a date and evidence. Both directions
fail loudly, and neither can be satisfied by wishful editing:

* claiming closure while the listeners are open -> red
* closing the listeners without recording it -> red
* leaving it open past the backstop in test_demo_window.py -> red
"""

import datetime as dt
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "docs/submission-manifest.yml"
CHECKLIST = REPO / "docs/post-demo-closure.md"
SCRIPT = REPO / "scripts/close_demo.sh"
DEMO_TFVARS = REPO / "infra/terraform/demo.auto.tfvars"
OPERATOR_TFVARS = REPO / "infra/terraform/terraform.tfvars"
CARD = REPO / "MODEL_CARD.md"

CONTROLS = (
    "demo_cidrs_closed",
    "reviewer_shared_secret_rotated",
    "demo_api_key_rotated",
    "rate_limit_active",
)
# Controls that can only be satisfied AFTER the window closes. `rate_limit_active` is not
# one of them: it is in force the whole time, so it has no excuse today either.
DEFERRABLE = {"demo_cidrs_closed", "reviewer_shared_secret_rotated", "demo_api_key_rotated"}


def _controls() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["post_demo_controls"]


def _window_is_open() -> bool:
    return DEMO_TFVARS.is_file()


def test_the_checklist_named_by_the_tls_decision_actually_exists():
    assert CHECKLIST.is_file(), "docs/tls-decision.md promises a post-demo checklist"


def test_the_checklist_covers_every_control_the_two_acceptances_rest_on():
    body = CHECKLIST.read_text(encoding="utf-8")
    for control in CONTROLS:
        assert control in body, f"{control} is claimed as a compensating control and unlisted"


def test_every_control_has_an_owner_and_a_verification_procedure():
    """The gap this closes was not that the controls were missing. It was that no one was
    named, so 'someone will close it after grading' was the entire plan."""
    for control, entry in _controls().items():
        assert entry.get("owner"), f"{control}: no owner"
        assert entry.get("how_to_verify"), f"{control}: no way to check it"
        assert entry.get("status"), f"{control}: no status"


def test_the_manifest_accounts_for_every_named_control():
    assert set(CONTROLS) <= set(_controls()), set(CONTROLS) - set(_controls())


def test_a_control_claiming_to_be_satisfied_carries_a_date_and_evidence():
    for control, entry in _controls().items():
        if entry.get("satisfied") is True:
            assert re.fullmatch(r"20\d\d-\d\d-\d\d", str(entry.get("verified_on"))), control
            assert entry.get("evidence"), f"{control}: satisfied with no evidence"


def test_the_always_on_control_is_satisfied_now_not_deferred():
    """`rate_limit_active` is in force during the demo window, not after it. Deferring it
    would mean the cleartext acceptance rests on a control nobody has checked."""
    entry = _controls()["rate_limit_active"]
    assert entry["satisfied"] is True, "the rate limit is a live control, not a post-demo task"


def test_while_the_window_is_open_the_manifest_says_so():
    """Direction one of the tripwire: no claiming closure while the listeners are open."""
    if not _window_is_open():
        return
    entry = _controls()["demo_cidrs_closed"]
    assert entry["satisfied"] is False, (
        "infra/terraform/demo.auto.tfvars still opens the listeners to 0.0.0.0/0, so "
        "demo_cidrs_closed cannot be satisfied. Delete the file and re-apply first."
    )
    assert entry.get("blocked_by"), "say what is holding it open"
    for control in DEFERRABLE:
        assert _controls()[control].get("due"), f"{control}: deferred with no trigger"


def test_when_the_window_closes_every_control_must_be_recorded_closed():
    """Direction two: closing the listeners without recording it is also red.

    This is the assertion the plan wanted, fired at the moment it becomes answerable rather
    than at the moment it is convenient.
    """
    if _window_is_open():
        return
    for control in CONTROLS:
        entry = _controls()[control]
        assert entry["satisfied"] is True, f"{control}: {entry.get('status')}"
        assert re.fullmatch(r"20\d\d-\d\d-\d\d", str(entry["verified_on"])), control
        assert entry["evidence"], f"{control}: no evidence path or command output"


def test_the_deferred_controls_have_not_silently_expired():
    """A deferral with no expiry is a decision to never do it. The backstop is the same date
    test_demo_window.py uses, so both go red together rather than one covering for the other.
    """
    if not _window_is_open():
        return
    today = dt.date.today()
    for control in DEFERRABLE:
        due = _controls()[control]["due"]
        if re.fullmatch(r"20\d\d-\d\d-\d\d", str(due)):
            assert today <= dt.date.fromisoformat(str(due)), (
                f"{control} was due {due} and is still open"
            )


def test_the_committed_tfvars_do_not_leave_the_demo_toggle_open_by_accident():
    """H15/H12. A committed 0.0.0.0/0 is a standing invitation, so opening it must be a
    deliberate, reviewable, single-file act -- which is what demo.auto.tfvars is. What must
    never happen is the operator's own tfvars quietly carrying it too."""
    if not OPERATOR_TFVARS.is_file():
        return
    match = re.search(r"demo_cidrs\s*=\s*(\[[^\]]*\])", OPERATOR_TFVARS.read_text(encoding="utf-8"))
    assert match is None or match.group(1).strip() == "[]", (
        f"demo_cidrs is open in the operator tfvars as {match.group(1)}; it belongs in "
        "demo.auto.tfvars where it is committed and reviewable"
    )


def test_every_compensating_control_the_card_claims_is_verified_in_the_manifest():
    """H13's acceptance is written to rest on named controls. This ties the card's claim to
    the state of those controls at submission time, which is the only moment the claim is
    being made to a reader."""
    card = CARD.read_text(encoding="utf-8")
    assert "Compensating controls" in card
    controls = _controls()
    for control in CONTROLS:
        assert controls[control].get("status"), (
            f"{control}: MODEL_CARD.md claims it, nothing tracks it (H13)"
        )


def test_the_closure_script_is_one_command_and_takes_no_secret_on_the_command_line():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "terraform -chdir=infra/terraform apply" in body
    assert "secretsmanager put-secret-value" in body
    assert "openssl rand" in body
    assert '--secret-string "$' not in body.replace("$(openssl", "OK("), "no secret literal in argv"


def test_closure_is_verified_from_off_the_allowlist_not_only_asserted():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "curl" in body and ("--max-time" in body or "--connect-timeout" in body), (
        "prove the endpoint now refuses a connection; a terraform apply is not a probe"
    )


def test_the_closure_script_deletes_the_file_that_holds_the_window_open():
    """Closing by editing the value leaves a file whose whole meaning is 'the window is
    open'. tests/unit/test_demo_window.py keys off the file's existence, so the script and
    the tripwire have to agree about what closed means."""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "demo.auto.tfvars" in body
    assert re.search(r"\brm\b[^\n]*demo\.auto\.tfvars", body), (
        "close the window by removing the file, not by editing the CIDR to []"
    )
