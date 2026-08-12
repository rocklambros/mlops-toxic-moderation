# Security Policy

## What this project is

`mlops-toxic-moderation` is a university course project (COMP 4450, MLOps). It builds a multi-label toxic comment moderation system: a FastAPI service, a Streamlit interface, a monitoring dashboard, and a Postgres database, deployed to a dedicated AWS account.

**It is not a production service and is not offered to anyone as a service.** The AWS deployment exists only for the duration of the course project and a stated demo window, and is destroyed afterwards.

During that window the moderation API on 8000, the user interface on 8501, and the monitoring dashboard on 8502 are deliberately reachable from the whole internet, over **cleartext HTTP**, so that the work can be graded. That is a decision with a written record and a stated residual risk: `docs/tls-decision.md`. The reviewer *console* on port 8503 is never opened — `toxic-mod-reviewer` is a security group with no ingress rule on it at all — and is reached only through an authenticated SSM port-forward.

The console is not the whole of the reviewer surface, and an earlier version of this file read as though it were. The API the console calls — `/review/login`, `/review/pending`, `/review/submit`, `/feedback/user` — is mounted on the backend, so it answers on **8000**, which is open. Until 2026-08-11 those four routes took no API key, no rate limit and no body size cap; they now sit behind the same gate as `/predict`, with `/review/login` the one exemption from the key. Since 2026-08-11 the first three also refuse any non-public TCP peer with a 404, so only `/feedback/user` is exposed exactly like `/predict`. The reviewer shared secret is still POSTed to a cleartext listener, which is recorded in `docs/tls-decision.md` as an accepted risk rather than as something the deployment prevents.

`/predict` accepts arbitrary text from anyone holding the demo API key and stores it in the database for 30 days before a scheduled purge nulls the text, so the project **does** hold third-party submissions for a bounded period.

An earlier version of this file said the opposite on both counts — that no third-party data was held, and that the listeners answered only one operator address. Neither was true when it was written, and neither is true now. The practices table below exists so that a reader can tell which sentences here were checked.

Read that as context for the scope and the response times below, both of which are deliberately honest rather than aspirational.

Maintained by one person. Reports get a real reply, not an SLA-backed one.

## Scope

**In scope**

- Source code in this repository: application code, tests, `Dockerfile`s, and the deployment pipeline.
- Infrastructure as code under `infra/`, including the Terraform configuration, the bootstrap script, and the service control policy.
- GitHub Actions workflow definitions under `.github/workflows/`, particularly anything that could allow an untrusted pull request to reach deployment credentials.
- Dependency and supply-chain issues, such as an unpinned or compromised dependency reachable from a build.

**Out of scope**

- **Any running AWS deployment.** It is ephemeral, IP-restricted, and not a service. Do not scan, probe, or attempt to reach it. Testing against live infrastructure is not authorized.
- Third-party services this project uses: Amazon Web Services, GitHub, Weights & Biases, RunPod, and Kaggle. Report issues in those to their own programs.
- The Jigsaw dataset, which is public research data owned by others.
- **Model evasion and misclassification.** Obfuscated text defeating the toxicity classifier, adversarial inputs, false positives, and false negatives are known model limitations documented in `MODEL_CARD.md`. They are model quality issues, not security vulnerabilities. Findings about *bias or fairness* are welcome as regular issues.
- Denial of service, social engineering, and physical attacks.

## Reporting

**Preferred: GitHub private vulnerability reporting.** Open the **Security** tab of this repository and choose **Report a vulnerability**. This is enabled and monitored. It keeps the report private until a fix exists and gives us a place to talk.

**Alternative:** email `rock@rockcyber.com` with `SECURITY` in the subject line.

Please do not open a public issue for a suspected vulnerability.

Useful things to include: what the problem is, which file or workflow it affects, how to reproduce it, and what an attacker gains. A proof of concept helps. A patch is welcome and not expected.

## What to expect

These are honest targets from a solo maintainer carrying a course deadline, not contractual commitments. If something slips, you get a status update rather than silence.

| Stage | Target |
|---|---|
| Acknowledgement | 5 business days |
| Assessment and plan | 15 business days |
| Fix for a serious issue | 30 days |
| Fix for a minor issue | Best effort, or documented as accepted risk |

**There is no bug bounty.** This is an unpaid course project. Credit is offered in the release notes or the commit message if you want it, and withheld if you prefer to stay anonymous. Say which when you report.

## Coordinated disclosure

Please give a reasonable window before publishing, 90 days from the report or 30 days after a fix ships, whichever comes first. If the issue is already public or being exploited, that window does not apply and speed matters more than coordination.

No CVE process is committed here, because this software has no downstream consumers. If that ever changes, this section changes with it.

## Good-faith research

Research conducted in good faith against **the source code in this repository** is welcome and will not be met with a complaint or legal action from the maintainer.

Two limits, and they are firm:

1. **Do not touch the running infrastructure.** Only the repository is in scope. The AWS account is not a target.
2. **Do not access, exfiltrate, or retain anyone's data.** No scenario here requires it.

This statement covers the maintainer only. It cannot and does not bind AWS, GitHub, Weights & Biases, RunPod, Kaggle, or anyone else, and it is not legal advice. Their terms still apply to you.

## Supported versions

`main` only. There are no releases or tags yet, so there is no supported-version matrix to publish. Fixes land on `main`.

## Practices in this repository

Stated as claims with a status and a way to check each one, rather than as assertions. An earlier version of this file listed nine practices in the present tense before any code existed, and two of them were contradicted by the project's own plan.

`Enforced` means a test or a policy fails if the claim stops being true. `Partial` means it holds under stated conditions that are not always met. `Not true` means exactly that, and the row is kept rather than deleted — a security policy that lists only the controls that pass is an advertisement.

| Claim | Status | Evidence |
|---|---|---|
| No static AWS credential exists in the deployment account. Humans use IAM Identity Center, CI uses GitHub OIDC, EC2 uses instance profiles | Enforced | `aws iam list-users` returns empty; `infra/aws/scp-sandbox-guardrails.json` denies `iam:CreateUser` and `iam:CreateAccessKey`; `tests/unit/test_scp_policy.py` |
| A service control policy denies credential creation, non-Graviton instance types, public RDS, Aurora, and detective-control tampering | Enforced | `infra/aws/scp-sandbox-guardrails.json`; five denials probed against the live account in `docs/evidence/a1-scp-denials.md` |
| No security group opens port 22, and no SSH key exists. Every remote action is AWS Systems Manager | Enforced | `infra/terraform/network.tf` carries no rule for port 22; recovery path in `docs/runbooks/no-ssh-debug.md` |
| Models load through `skops` with a static trusted-type allowlist and a SHA-256 check against a digest committed to git, and fail closed on mismatch. Never `pickle` or `joblib` | Enforced | `backend/model_loader.py`; `tests/unit/test_model_loader.py`; digest of record in `MODEL_CARD.md` |
| No credential value ever travels as an SSM `SendCommand` parameter or a Docker build argument | Enforced | `tests/infra/test_roll_secrets.py`; `tests/unit/test_buildkit_secrets.py` |
| Secrets live in AWS Secrets Manager and are read on the instance under its own profile. The database password is generated and held by RDS and never enters Terraform state | Enforced | `infra/deploy/instance/roll.sh`; `manage_master_user_password` in `infra/terraform/data.tf` |
| CI runs `ruff`, `pytest`, `gitleaks` and `semgrep` on every pull request, and a failing check blocks the merge | Enforced | `.github/workflows/ci.yml`; blocked-merge evidence in `docs/evidence/ci-gate.md` |
| Every GitHub Action is pinned to a full commit SHA, and no workflow job inherits write permission | Enforced | `tests/unit/test_pin_actions.py`; `tests/unit/test_deploy_workflow.py` |
| Python dependencies install from a hashed lock with `--require-hashes`, and container base images are pinned by digest | Enforced | `requirements/dev.lock`; `tests/unit/test_dependency_locks.py`; `tests/unit/test_dockerfile_hygiene.py` |
| Every route on the backend enforces a demo API key, a rate limit, and an input-size cap. The key has three named exemptions, each with a reason in `backend/app.py`: `/health`, the OpenAPI schema routes, and `/review/login`. Nothing is exempt from the rate limit. Since 2026-08-11 the four reviewer routes additionally require a non-public TCP peer, and answer 404 rather than 401 to anyone else | Enforced | `backend/app.py`, `backend/auth.py`, `backend/ratelimit.py`; `tests/unit/test_request_gate.py`; `tests/integration/test_predict_abuse_controls.py` |
| The rate limit meters a caller rather than a credential, so one visitor cannot exhaust the allowance of the next one | Enforced | `backend/fingerprint.py`, `backend/app.py`; `tests/unit/test_request_gate.py`; `tests/integration/test_submitter_fingerprint.py` |
| The reviewer **console** on 8503 is never reachable from the internet. Until 2026-08-11 this row said "the reviewer queue", which reads as a claim about the queue itself — the API the console calls is mounted on 8000, which is open, and it was then ungated | **Partial** — true of the port and of the console, false as it was worded about the capability | `infra/exposure.py`; `tests/unit/test_exposure_contract.py`; `tests/integration/test_deployed_traversal.py`; `tests/unit/test_demo_window.py`; the gate that now covers the API is in `backend/app.py` |
| The reviewer shared secret does not cross a cleartext listener | **Not true** — `/review/login` is on 8000 and the console posts the secret to it in the clear. Accepted with the rest of the cleartext decision | `backend/review_api.py`; residual risk in `docs/tls-decision.md` |
| Ingress to the three graded listeners is restricted to the operator address | **Partial** — open to `0.0.0.0/0` since 2026-08-10 for the demo window, by deliberate committed change | `infra/terraform/demo.auto.tfvars`; `tests/unit/test_demo_window.py`; rationale in `docs/tls-decision.md` |
| Submitted comment text is never written to Weights & Biases, to application logs, or to any screenshot | Enforced | `scripts/redact.py` and `tests/unit/test_redact.py` for the publication path; no logging call takes the text |
| Submitted comment text is retained for 30 days and then purged | **Partial** — the purge is correct and tested, and nothing schedules it. `make purge` is the only caller, so the window holds only when an operator runs it | `backend/retention.py`; `tests/integration/test_retention.py` call `purge()` directly, which proves the function nulls old rows and not that any row has been nulled |
| Traffic between the browser and the service is encrypted | **Not true** — the three demo endpoints are plain HTTP. Accepted, with the residual risk written down | `docs/tls-decision.md` |
| A CycloneDX SBOM and AIBOM are published with each release | **Planned** — specified, not yet generated | Task 23 of `docs/superpowers/plans/2026-07-31-phase-5-deploy-docs.md` |

If you find a place where one of these rows is not true, that is a valid report and a useful one.
