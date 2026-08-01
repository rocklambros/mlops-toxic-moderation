# The CI gate refuses a red pull request

Captured 2026-08-01 against `rocklambros/mlops-toxic-moderation`, protected branch `main`.

A gate that has never refused anything is indistinguishable from a gate that is switched off.
"Branch protection is configured" is a claim about a settings page; this file records what the
API and the CLI actually did when a merge was attempted with the gate red.

## Protection in force

```
required status checks : ["ci-gate"]
strict (up to date)    : true
enforce_admins         : true
allow_force_pushes     : false
allow_deletions        : false
required_conversation_resolution : true
```

`enforce_admins: true` is the load-bearing setting. Without it the rule binds everyone except
the people most likely to be in a hurry, and the account driving this project is an
administrator.

## The experiment

Pull request **#15** (`proof/ci-gate-blocks`) carried one file,
`tests/unit/test_gate_proof.py`, containing a single deliberately failing assertion. It was
opened against protected `main`, allowed to run CI, and then merged three ways.

CI result on that head: `lint` FAILURE, `test` FAILURE, `secrets-scan` SUCCESS, `sast` SUCCESS,
`deps-audit` SUCCESS, `terraform` SUCCESS, and the aggregate **`ci-gate` FAILURE**.

Note that four of the six jobs passed. The aggregate job is what turns "most things are fine"
into a refusal — a protection rule listing six separate contexts would let a green subset look
like progress.

## What each attempt returned

**Pull-request state (`gh pr view 15`)** — `docs/evidence/blocked-merge-cli.txt`:

```
mergeable: MERGEABLE | state: BLOCKED
```

`MERGEABLE` here means "no merge conflicts". `BLOCKED` is the policy. The two are different
questions and only the second one is the gate.

**REST API merge** — `docs/evidence/blocked-merge-api.txt`:

```
HTTP 405
{"message": "Required status check \"ci-gate\" is failing.", ...}
```

**CLI merge** — `docs/evidence/blocked-merge-cli-attempt.txt`:

```
X Pull request #15 is not mergeable: the base branch policy prohibits the merge.
To use administrator privileges to immediately merge the pull request, add the `--admin` flag.
```

**CLI merge with `--admin`**, the escape hatch the CLI itself advertises:

```
GraphQL: Required status check "ci-gate" is failing. (mergePullRequest)
```

Refused. `main` remained at `1afa73a` throughout.

## Afterwards

Pull request #15 was closed without merging and `proof/ci-gate-blocks` deleted;
`tests/unit/test_gate_proof.py` exists on no branch. The same gate, green, is what admitted
pull request #14 (Phase 4) to `main` minutes earlier — so both directions are evidenced: it
refuses red and admits green.

**Not captured: a screenshot.** The plan asked for `blocked-merge.png`. The API and CLI
transcripts above are strictly stronger evidence — they are reproducible, machine-checkable,
and quote the server's own refusal — whereas an image proves only what a browser rendered. It
is recorded as absent rather than substituted for silently.
