"""Temporary. Exists to prove the branch-protection gate blocks a red pull request.

A gate that has never refused anything is indistinguishable from a gate that is switched
off, and "we configured branch protection" is a claim about a settings page rather than
about behaviour. This test fails on purpose so the pull request carrying it goes red, the
`ci-gate` job reports failure, and the merge is refused by the API rather than by anyone's
good intentions. The evidence is captured in docs/evidence/ci-gate.md and this file is
deleted in the same task.
"""


def test_this_test_fails_on_purpose_to_prove_the_gate_refuses_a_red_pull_request():
    assert False, "deliberate failure: proving ci-gate blocks the merge"
