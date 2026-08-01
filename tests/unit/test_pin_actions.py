"""The tag->SHA action pinner (premortem H35). Pure function, fake resolver, no network.

Any action in any job can be swapped under its tag by whoever controls that tag, and
`deploy.yml` runs jobs that mint the `gha-deploy` OIDC token. A movable tag in *any* workflow
is therefore a supply-chain hole with production blast radius. The tool is built before there
is a workflow to pin, so the workflow is never committed unpinned even once and no fabricated
SHA ever enters the repository.
"""

import subprocess

import pytest
import yaml

from scripts.pin_actions import main, pin_text, resolve_with_gh

SHA = "0" * 39 + "1"


def fake_resolver(owner: str, repo: str, ref: str) -> str:
    assert ref and not ref.startswith("#")
    return SHA


def test_a_tagged_action_is_rewritten_to_a_sha_with_the_tag_kept_as_a_comment():
    text = "    steps:\n      - uses: actions/checkout@v4.2.2\n"
    assert pin_text(text, fake_resolver) == (
        f"    steps:\n      - uses: actions/checkout@{SHA}  # v4.2.2\n"
    )


def test_an_action_with_a_subpath_keeps_the_subpath():
    text = "      - uses: github/codeql-action/init@v3\n"
    assert f"github/codeql-action/init@{SHA}  # v3" in pin_text(text, fake_resolver)


def test_pinning_is_idempotent():
    once = pin_text("      - uses: actions/checkout@v4.2.2\n", fake_resolver)
    assert pin_text(once, fake_resolver) == once


def test_a_local_action_is_left_alone():
    text = "      - uses: ./.github/actions/setup\n"
    assert pin_text(text, fake_resolver) == text


def test_a_docker_action_reference_is_left_alone():
    text = "      - uses: docker://alpine:3.20\n"
    assert pin_text(text, fake_resolver) == text


def test_a_resolver_that_returns_a_tag_is_rejected():
    def liar(owner: str, repo: str, ref: str) -> str:
        return "v4.2.2"

    with pytest.raises(ValueError, match="non-sha"):
        pin_text("      - uses: actions/checkout@v4.2.2\n", liar)


def test_a_resolver_that_returns_an_abbreviated_or_uppercase_sha_is_rejected():
    """A short SHA is ambiguous and GitHub resolves it at run time, which is the property
    being removed. Uppercase hex compares unequal to everything the hygiene test looks for,
    so it would pass here and fail there."""
    for bad in ("0" * 7, SHA.upper().replace("0", "A"), ""):
        with pytest.raises(ValueError, match="non-sha"):
            pin_text("      - uses: actions/checkout@v4\n", lambda o, r, ref, b=bad: b)


def test_lines_that_are_not_uses_are_untouched():
    text = "    # uses: actions/checkout@v4\n    run: echo uses: actions/checkout@v4\n"
    assert pin_text(text, fake_resolver) == text


def test_a_file_without_a_trailing_newline_does_not_gain_one():
    assert pin_text("      - uses: actions/checkout@v4", fake_resolver).endswith(SHA + "  # v4")


def test_the_pinned_workflow_still_parses_as_yaml_and_says_the_same_thing():
    """The rewrite is textual, so a missing space before the `#` would turn the comment into
    part of the value and the workflow would resolve an action that does not exist."""
    before = (
        "name: ci\n"
        "on:\n"
        "  pull_request:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  lint:\n"
        "    runs-on: ubuntu-24.04-arm\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4.2.2\n"
        "      - uses: actions/setup-python@v5\n"
    )
    after = yaml.safe_load(pin_text(before, fake_resolver))
    steps = after["jobs"]["lint"]["steps"]
    assert [step["uses"] for step in steps] == [
        f"actions/checkout@{SHA}",
        f"actions/setup-python@{SHA}",
    ]


def test_the_resolver_asks_the_github_api_for_the_commit_a_ref_points_at(monkeypatch):
    """`resolve_with_gh` is the only part of this tool that touches the network, so it is the
    only part a fake resolver never exercises. A wrong endpoint here writes a plausible SHA
    that points at the wrong thing."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["check"] = kwargs.get("check")
        return subprocess.CompletedProcess(argv, 0, stdout=f"{SHA}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert resolve_with_gh("actions", "checkout", "v4.2.2") == SHA
    assert seen["argv"] == [
        "gh",
        "api",
        "repos/actions/checkout/commits/v4.2.2",
        "--jq",
        ".sha",
    ]
    assert seen["check"] is True, "a failed lookup must not be read as an empty SHA"


def test_main_rewrites_the_files_it_is_given_and_reports_what_changed(tmp_path, capsys):
    workflow = tmp_path / "ci.yml"
    workflow.write_text("      - uses: actions/checkout@v4.2.2\n", encoding="utf-8")
    already = tmp_path / "deploy.yml"
    already.write_text(f"      - uses: actions/checkout@{SHA}  # v4.2.2\n", encoding="utf-8")

    assert main(["pin_actions", str(workflow), str(already)], resolve=fake_resolver) == 0

    assert workflow.read_text(encoding="utf-8") == (
        f"      - uses: actions/checkout@{SHA}  # v4.2.2\n"
    )
    assert already.read_text(encoding="utf-8") == (
        f"      - uses: actions/checkout@{SHA}  # v4.2.2\n"
    )
    out = capsys.readouterr().out
    assert "ci.yml" in out and "1 workflow(s) rewritten" in out, out
    assert "deploy.yml" not in out, "an unchanged file was reported as rewritten"
