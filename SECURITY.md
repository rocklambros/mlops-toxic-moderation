# Security Policy

## What this project is

`mlops-toxic-moderation` is a university course project (COMP 4450, MLOps). It builds a multi-label toxic comment moderation system: a FastAPI service, a Streamlit interface, a monitoring dashboard, and a Postgres database, deployed to a dedicated AWS account.

**It is not a production service and is not offered to anyone as a service.** The AWS deployment is created and destroyed between work sessions, its network ingress is restricted to a single operator address, and it holds no third-party user data. Read that as context for the scope and the response times below, both of which are deliberately honest rather than aspirational.

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

Stated so you know what has already been considered, and where to look for gaps:

- No static AWS credentials exist. Human access uses IAM Identity Center, CI uses GitHub OIDC with separate read and deploy roles, and compute uses instance profiles.
- A service control policy denies `iam:CreateUser` and `iam:CreateAccessKey` in the deployment account, so the previous point is enforced rather than merely intended.
- Deployment runs over AWS Systems Manager. No security group opens port 22 and no SSH key exists.
- Models load through `skops`, `safetensors`, and ONNX with SHA-256 digest verification. Never `pickle` or `joblib`.
- Secrets live in AWS Secrets Manager. The database password is generated and held by RDS and never enters Terraform state.
- CI runs `ruff`, `pytest`, `gitleaks`, and `semgrep` on every pull request, and GitHub secret scanning with push protection is enabled on this repository.
- Dependencies are pinned.

If you find a place where one of these claims is not true, that is a valid report and a useful one.
