# Phase A2: Terraform Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the entire runtime of the toxic-moderation system in the `rockcyber-mlops-toxic` member account from code: one VPC, **three** EC2 instances (backend, frontend, monitoring) each with its own security group and its own instance profile, a private RDS Postgres 16 with a read-only role for the dashboard, four ECR repositories, one GitHub OIDC deploy role, CloudTrail, GuardDuty, CloudWatch logs and one health alarm, a `$100` budget, and a nightly stop schedule that is a hard cost control rather than an alert. Every normative item in this phase is carried by a test that fails when the item is absent.

**Architecture:**

```
                      Internet
                          |
                    +-----+-----+
                    |    IGW    |
                    +-----+-----+
  VPC 10.42.0.0/16        |
  +---------------------------------------------------------------+
  | public-a 10.42.0.0/24            public-b 10.42.1.0/24        |
  |   map_public_ip_on_launch = true (user data needs a route      |
  |   BEFORE the EIP associates)                                   |
  |                                                                |
  |   EC2 #1 backend      EC2 #2 frontend     EC2 #3 monitoring    |
  |   t4g.medium          t4g.small           t4g.medium           |
  |   sg-backend :8000    sg-frontend :8501   sg-monitoring :8502  |
  |   EIP #1              EIP #2  (:8503 = 0 ingress) EIP #3       |
  |   role-backend        role-frontend       role-monitoring      |
  |        \                   |                    /              |
  |         \                  |                   /               |
  |          +--------- 5432 --+------------------+                |
  |                            |                                   |
  | private-a 10.42.10.0/24    |    private-b 10.42.11.0/24        |
  |   RDS db.t4g.micro Postgres 16, sg-db, no egress, no IGW route |
  +---------------------------------------------------------------+

  Egress from every app SG, explicit, because Terraform DELETES the
  default allow-all the moment an aws_security_group is declared:
    443/tcp -> 0.0.0.0/0   ECR, SSM, Secrets Manager, W&B, CloudWatch
     53/udp + 53/tcp -> VPC CIDR      VPC resolver
    123/udp -> 169.254.169.123/32     Amazon Time Sync
   5432/tcp -> private subnet CIDRs   RDS
```

**Tech Stack:** Terraform 1.15.8, `hashicorp/aws` 6.57.1, `hashicorp/time` 0.14.0, AWS CLI 2.36.3, Amazon Linux 2023 arm64, Docker + compose v5.3.1, `python-hcl2` 8.1.2 for offline plan-source assertions, `checkov` 3.3.8 for the IaC security scan, pytest 8.3.3.

## Global Constraints

Inherited from `docs/superpowers/specs/2026-07-30-delivery-plan-design.md` (governs on conflict), `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md`, and the master roadmap. The ones that bind Phase A2:

- **Region `us-west-2`.** Availability zones `us-west-2a` and `us-west-2b`. Everything Graviton (`arm64`).
- **Three EC2 instances, one per tier.** EC2 #1 `t4g.medium` backend, EC2 #2 `t4g.small` frontend, EC2 #3 `t4g.medium` monitoring. Rubric 5.1 names one container for the backend and one for the frontend, 5.2 requires "separate EC2 instances", 3.2 requires the dashboard on "a different EC2 server".
- **Per-tier security groups and per-tier instance profiles.** No shared `sg-app`, no shared `ec2-app-role`. The monitoring dashboard additionally connects to Postgres as a **read-only** role.
- **No SSH, no port 22, no bastion, no NAT gateway.** Operator access is SSM Session Manager and SSM Run Command. Egress must therefore work on the first boot or the instance is unreachable by design.
- **No static AWS credentials.** Operator uses the IAM Identity Center profile `mlops-admin` against the member account. CI uses GitHub OIDC. EC2 uses instance profiles.
- **The SCP is already attached** by Phase A1. Terraform must satisfy it: instance types confined to `t4g.small`, `t4g.medium`, `t4g.large`, `c7g.xlarge`; RDS private; `manage_master_user_password = true`; no Aurora; region locked.
- **Terraform state** lives in the A1-created S3 bucket with S3 native locking (`use_lockfile = true`), which requires Terraform 1.11 or newer.
- **`terraform destroy` must succeed cleanly**, because it is cost control #2. Anything that blocks it (deletion protection, non-empty ECR repositories, a missing final-snapshot identifier) is a defect.
- **Feature-branch and PR.** Human author (`rocklambros <rock@rockcyber.com>`). No AI attribution in commits, code, or docs.

**Branch:** `feat/phase-a2-terraform` off `main`.

**Operator profile:** every AWS CLI command in this plan assumes `export AWS_PROFILE=mlops-admin` and `export AWS_REGION=us-west-2` for the member account. `rc-mgmt` is the *management* account profile and is never used here.

## File Structure

```
infra/terraform/
  backend.tf                     terraform block, S3 backend, providers
  variables.tf                   all inputs, incl. the pinned ami_id
  ami.auto.tfvars                the pinned AMI id, committed (C7)
  backend.hcl.example            partial backend config template
  network.tf                     VPC, subnets, IGW, routes, 4 security groups
  compute.tf                     3 instances, 3 EIPs, 3 associations
  data.tf                        subnet group, RDS, read-only role bootstrap
  iam.tf                         3 instance roles + profiles, scheduler role
  oidc.tf                        OIDC provider + gha-deploy role only
  ecr.tf                         4 repositories + lifecycle policies
  observability.tf               log groups, SNS, CloudTrail, GuardDuty, alarm
  budget.tf                      $100 budget + nightly stop schedules
  outputs.tf                     endpoints, ARNs, log group names
  templates/user_data.sh.tftpl   AL2023 arm64 bootstrap
  sql/monitoring_readonly.sql    the read-only Postgres role
infra/smoke/                     THROWAWAY day-9 single-instance module
  main.tf variables.tf outputs.tf
  user_data.sh.tftpl
  Dockerfile health_server.py
tests/infra/
  __init__.py tfparse.py
  test_backend_and_providers.py test_ami_pin.py test_network.py
  test_security_groups.py test_iam.py test_oidc.py test_ecr.py
  test_compute.py test_data.py test_observability.py test_budget.py
  test_outputs.py test_docs_controls.py test_workflow_guards.py
  test_smoke_module.py test_smoke_health_server.py test_plan_assertions.py
docs/runbooks/no-ssh-debug.md    (C6)
docs/cost-model.md               (H7)
docs/tls-decision.md             (H15)
docs/evidence/a2-smoke-deploy.md (day-9 checkpoint evidence)
docs/evidence/a2-apply-destroy.md
.github/workflows/terraform-ci.yml
.github/workflows/deploy.yml     (skeleton: no apply, paths-ignore)
requirements/infra.txt
```

## Interfaces Produced (consumed by Phase 5 and the monitoring dashboard)

Terraform outputs are the seam. Phase 5's `deploy.yml` and `docker-compose.yml` read exactly these names and nothing else.

```
backend_url             string  "http://<eip1>:8000"
frontend_url            string  "http://<eip2>:8501"
monitoring_url          string  "http://<eip3>:8502"
instance_ids            map(string)  {backend=…, frontend=…, monitoring=…}
ssm_target_tag          string  "Component"          # SendCommand selects on this tag
ecr_repository_urls     map(string)  {backend=…, frontend=…, monitoring=…, rescorer=…}
log_group_names         map(string)  {backend=…, frontend=…, monitoring=…, rescorer=…}
db_endpoint             string  "<host>:5432"
db_name                 string  "toxicmod"
db_master_secret_arn    string  # RDS-managed, read by backend + frontend only
db_readonly_secret_arn  string  # read by monitoring only
gha_deploy_role_arn     string  # goes into the GitHub repo variable AWS_DEPLOY_ROLE_ARN
alerts_topic_arn        string
```

The read-only Postgres role is `monitor_ro`, and it holds `CONNECT`, `USAGE`, and `SELECT` and nothing else. `monitoring/dashboard.py` connects with it. `backend/db.py` and `frontend/ui.py` connect with the master role.

---

### Task 1: Infra test harness, pinned tooling, `backend.tf`, `variables.tf`

The harness comes first because every later task's failing test depends on it. The parser is offline and CI-safe, which matters because H36 removes `terraform plan` from pull-request CI — so the only assertions CI can make about the infrastructure are static ones.

**Files:**
- Create: `requirements/infra.txt`, `tests/infra/__init__.py`, `tests/infra/tfparse.py`, `infra/terraform/backend.tf`, `infra/terraform/variables.tf`, `infra/terraform/backend.hcl.example`
- Modify: `Makefile`, `pyproject.toml`, `requirements/dev.txt`, `.gitignore`
- Test: `tests/infra/test_backend_and_providers.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/tfparse.py`:
```python
"""Offline HCL2 accessors for the Terraform static assertions.

python-hcl2 8.x preserves the raw quoting of string literals and block labels and
injects a `__is_block__` marker into every block body. Both are normalised away here
so the assertions read like the Terraform they assert on. Everything in this module
is pure file parsing: no AWS call, no `terraform init`, no credentials. That is a
requirement, not a convenience, because pull-request CI runs these tests with no AWS
identity at all (premortem H36).
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import hcl2

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "infra" / "terraform"
SMOKE = ROOT / "infra" / "smoke"


def _unquote(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _clean(node: Any) -> Any:
    if isinstance(node, dict):
        return {_unquote(k): _clean(v) for k, v in node.items() if k != "__is_block__"}
    if isinstance(node, list):
        return [_clean(v) for v in node]
    return _unquote(node)


@functools.lru_cache(maxsize=8)
def load(tf_dir: Path = MAIN) -> dict[str, list]:
    """Parse every *.tf file in tf_dir into one merged, normalised dict."""
    paths = sorted(Path(tf_dir).glob("*.tf"))
    if not paths:
        raise AssertionError(f"no .tf files in {tf_dir}")
    merged: dict[str, list] = {}
    for path in paths:
        with path.open() as handle:
            parsed = _clean(hcl2.load(handle))
        for section, entries in parsed.items():
            merged.setdefault(section, []).extend(entries)
    return merged


def _collect(section: str, kind: str, tf_dir: Path) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for entry in load(tf_dir).get(section, []):
        for entry_kind, bodies in entry.items():
            if entry_kind == kind:
                found.update(bodies)
    return found


def resources(kind: str, tf_dir: Path = MAIN) -> dict[str, dict]:
    return _collect("resource", kind, tf_dir)


def resource(kind: str, name: str, tf_dir: Path = MAIN) -> dict:
    found = resources(kind, tf_dir)
    assert name in found, f"missing resource {kind}.{name}; found {sorted(found)}"
    return found[name]


def data_source(kind: str, name: str, tf_dir: Path = MAIN) -> dict:
    found = _collect("data", kind, tf_dir)
    assert name in found, f"missing data {kind}.{name}; found {sorted(found)}"
    return found[name]


def data_kinds(tf_dir: Path = MAIN) -> set[str]:
    kinds: set[str] = set()
    for entry in load(tf_dir).get("data", []):
        kinds.update(entry.keys())
    return kinds


def _flatten(section: str, tf_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for entry in load(tf_dir).get(section, []):
        out.update(entry)
    return out


def variables(tf_dir: Path = MAIN) -> dict[str, dict]:
    return _flatten("variable", tf_dir)


def outputs(tf_dir: Path = MAIN) -> dict[str, dict]:
    return _flatten("output", tf_dir)


def terraform_block(tf_dir: Path = MAIN) -> dict:
    entries = load(tf_dir).get("terraform", [])
    assert entries, f"no terraform block in {tf_dir}"
    merged: dict = {}
    for entry in entries:
        merged.update(entry)
    return merged


def blocks(body: dict, name: str) -> list[dict]:
    """Nested blocks of one name inside a body, always returned as a list."""
    raw = body.get(name, [])
    if isinstance(raw, list):
        return raw
    return [raw]
```

`tests/infra/test_backend_and_providers.py`:
```python
"""Terraform root-module wiring: version floor, S3 native locking, pinned providers."""

import re

from tests.infra import tfparse


def test_required_version_floor_is_at_least_1_11():
    # S3 native state locking went GA in Terraform 1.11. Anything lower silently
    # falls back to the DynamoDB lock table this project deliberately does not have.
    constraint = tfparse.terraform_block()["required_version"]
    assert re.fullmatch(r">=\s*1\.(1[1-9]|[2-9]\d)(\.\d+)?", constraint), constraint


def test_s3_backend_uses_native_locking_and_no_dynamodb_table():
    backend = tfparse.blocks(tfparse.terraform_block(), "backend")[0]["s3"]
    assert backend["use_lockfile"] is True
    assert backend["encrypt"] is True
    assert "dynamodb_table" not in backend


def test_providers_are_pinned_to_an_exact_major_and_minor():
    providers = tfparse.blocks(tfparse.terraform_block(), "required_providers")[0]
    assert providers["aws"]["source"] == "hashicorp/aws"
    assert re.fullmatch(r"~>\s*\d+\.\d+", providers["aws"]["version"])
    assert providers["time"]["source"] == "hashicorp/time"


def test_default_tags_are_applied_by_the_provider():
    provider = tfparse.load()["provider"][0]["aws"]
    tags = tfparse.blocks(provider, "default_tags")[0]["tags"]
    assert tags["Project"] == "${var.project}"
    assert tags["ManagedBy"] == "terraform"


def test_region_is_locked_to_us_west_2():
    region = tfparse.variables()["region"]
    assert region["default"] == "us-west-2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_backend_and_providers.py -v`

Expected: 5 errors, each `AssertionError: no .tf files in /home/rock/github_projects/mlops-toxic-moderation/infra/terraform`

- [ ] **Step 3: Write minimal implementation**

`requirements/infra.txt`:
```
python-hcl2==8.1.2
PyYAML==6.0.3
```

Append to `requirements/dev.txt`:
```
-r infra.txt
```

`infra/terraform/backend.tf`:
```hcl
terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.57"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.14"
    }
  }

  # Partial configuration. Real values come from backend.hcl, which bootstrap.sh
  # writes and .gitignore excludes. `use_lockfile` is S3 native locking, GA in
  # Terraform 1.11; this project has no DynamoDB lock table by design.
  backend "s3" {
    key          = "phase-a2/terraform.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Phase     = "A2"
    }
  }
}

provider "time" {}

data "aws_caller_identity" "current" {}
```

`infra/terraform/backend.hcl.example`:
```hcl
bucket = "REPLACE_WITH_TF_STATE_BUCKET_FROM_infra/aws/bootstrap-outputs.env"
region = "us-west-2"
```

`infra/terraform/variables.tf`:
```hcl
variable "project" {
  description = "Name prefix for every resource in this account."
  type        = string
  default     = "toxic-mod"
}

variable "region" {
  description = "The only region this workload may exist in; the SCP denies the rest."
  type        = string
  default     = "us-west-2"
}

variable "azs" {
  description = "Availability zones for the two public and two private subnets."
  type        = list(string)
  default     = ["us-west-2a", "us-west-2b"]
}

variable "vpc_cidr" {
  description = "VPC address space."
  type        = string
  default     = "10.42.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "Public subnets, one per AZ. EC2 lives here; there is no NAT gateway."
  type        = list(string)
  default     = ["10.42.0.0/24", "10.42.1.0/24"]
}

variable "private_subnet_cidrs" {
  description = "Private subnets, one per AZ. RDS lives here with no internet path."
  type        = list(string)
  default     = ["10.42.10.0/24", "10.42.11.0/24"]
}

variable "operator_cidrs" {
  description = "Always-on ingress allowlist. The operator's address, /32."
  type        = list(string)
}

variable "demo_cidrs" {
  description = "Temporary public demo window. Set to [\"0.0.0.0/0\"] only while a grader is looking, then set it back to []."
  type        = list(string)
  default     = []
}

variable "github_repo" {
  description = "owner/name of the repository allowed to assume the deploy role."
  type        = string
  default     = "rocklambros/mlops-toxic-moderation"
}

variable "alert_email" {
  description = "Address subscribed to the budget and health SNS topic."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Hard ceiling the budget alerts fire against."
  type        = number
  default     = 100
}
```

`Makefile` additions (tabs, not spaces, for recipe lines):
```makefile
.PHONY: tf-fmt tf-validate tf-scan infra-test
TF_DIRS := infra/terraform infra/smoke
CHECKOV_VENV ?= .venv-checkov

tf-fmt:
	terraform fmt -recursive infra/

tf-validate:
	set -e; for d in $(TF_DIRS); do \
	  terraform -chdir=$$d fmt -check -recursive; \
	  terraform -chdir=$$d init -backend=false -input=false; \
	  terraform -chdir=$$d validate; \
	done

tf-scan:
	test -d $(CHECKOV_VENV) || ($(PY) -m venv $(CHECKOV_VENV) && $(CHECKOV_VENV)/bin/python -m pip install checkov==3.3.8)
	$(CHECKOV_VENV)/bin/checkov --directory infra --framework terraform --compact --quiet

infra-test:
	PYTHONHASHSEED=0 $(BIN)/pytest tests/infra -m "not integration" -q
```

Append to `pyproject.toml` under `[tool.pytest.ini_options]`, replacing the existing `markers` line:
```toml
markers = [
  "integration: needs external services (deselect with -m 'not integration')",
  "awsapply: requires a live AWS apply (deselect with -m 'not awsapply')",
]
```

Append to `.gitignore`:
```
infra/terraform/backend.hcl
infra/terraform/*.auto.tfvars.json
infra/terraform/.terraform/
infra/smoke/.terraform/
*.tfstate
*.tfstate.*
*.tfplan
plan.json
.venv-checkov/
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
.venv/bin/python -m pip install -r requirements/infra.txt
make tf-fmt && make tf-validate
.venv/bin/pytest tests/infra/test_backend_and_providers.py -v
```
Expected: `terraform validate` prints `Success! The configuration is valid.` for both directories (the `infra/smoke` loop entry fails until Task 2, so run `terraform -chdir=infra/terraform validate` alone for now), and 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add requirements/infra.txt requirements/dev.txt tests/infra infra/terraform Makefile pyproject.toml .gitignore
git commit -m "Add offline Terraform assertion harness and pinned root module wiring"
```

---

### Task 2: DAY-9 THROWAWAY SINGLE-INSTANCE SMOKE DEPLOY — the checkpoint gate

This is the day-9 gate from the delivery spec schedule and the leading indicator behind the day-11 cut-line. One EC2, one container, `/health` reachable. It is throwaway: its own root module, its own state key, destroyed the moment the evidence is captured. Its entire purpose is to convert every first-time-ever integration — ECR authentication, arm64 boot, SSM registration, **egress including DNS and NTP**, EIP association, the `awslogs` driver, IMDSv2 with hop limit 2 — from a day-13 emergency into a day-9 discovery. It runs **before** the real stack exists and depends on no other phase.

**Files:**
- Create: `infra/smoke/main.tf`, `infra/smoke/variables.tf`, `infra/smoke/outputs.tf`, `infra/smoke/user_data.sh.tftpl`, `infra/smoke/Dockerfile`, `infra/smoke/health_server.py`, `docs/evidence/a2-smoke-deploy.md`
- Test: `tests/infra/test_smoke_health_server.py`, `tests/infra/test_smoke_module.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_smoke_health_server.py`:
```python
"""The throwaway smoke container must answer /health locally before it costs a cent."""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[2] / "infra" / "smoke" / "health_server.py"
PORT = "18000"


@pytest.fixture()
def server():
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)],
        env={"SMOKE_PORT": PORT, "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1).read()
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("smoke health server never came up")
    yield
    proc.terminate()
    proc.wait(timeout=10)


def test_health_returns_200_and_status_ok(server):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5) as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["status"] == "ok"


def test_unknown_path_returns_404(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/nope", timeout=5)
    assert excinfo.value.code == 404
```

`tests/infra/test_smoke_module.py`:
```python
"""The throwaway module must exercise the same failure surfaces the real stack has."""

from tests.infra import tfparse

SMOKE = tfparse.SMOKE


def test_smoke_instance_requires_imdsv2_with_hop_limit_two():
    inst = tfparse.resource("aws_instance", "smoke", SMOKE)
    meta = tfparse.blocks(inst, "metadata_options")[0]
    assert meta["http_tokens"] == "required"
    assert meta["http_put_response_hop_limit"] == 2


def test_smoke_security_group_declares_explicit_egress_for_443_dns_and_ntp():
    # C6: aws_security_group deletes the default allow-all. 443 alone is not enough,
    # because name resolution of ssm.us-west-2.amazonaws.com needs UDP 53 to the VPC
    # resolver and SigV4 signing needs a correct clock from 169.254.169.123 on UDP 123.
    egress = tfparse.blocks(tfparse.resource("aws_security_group", "smoke", SMOKE), "egress")
    assert egress, "no egress block: the instance would never register with SSM"
    ports = {(rule["from_port"], rule["protocol"]) for rule in egress}
    assert (443, "tcp") in ports
    assert (53, "udp") in ports
    assert (123, "udp") in ports


def test_smoke_security_group_never_opens_22():
    ingress = tfparse.blocks(tfparse.resource("aws_security_group", "smoke", SMOKE), "ingress")
    assert all(rule["from_port"] != 22 for rule in ingress)


def test_smoke_subnet_maps_a_public_ip_on_launch():
    # User data runs before the EIP associates. Without an auto-assigned address the
    # instance has no route out during cloud-init and `dnf install docker` hangs.
    assert tfparse.resource("aws_subnet", "smoke", SMOKE)["map_public_ip_on_launch"] is True


def test_smoke_pins_the_ami_rather_than_resolving_it():
    assert "aws_ssm_parameter" not in tfparse.data_kinds(SMOKE)
    assert tfparse.resource("aws_instance", "smoke", SMOKE)["ami"] == "${var.ami_id}"


def test_smoke_has_an_eip_and_publishes_the_health_url():
    assert "smoke" in tfparse.resources("aws_eip", SMOKE)
    assert "smoke_health_url" in tfparse.outputs(SMOKE)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_smoke_health_server.py tests/infra/test_smoke_module.py -v`

Expected: the health-server tests fail with `FileNotFoundError: ... infra/smoke/health_server.py`; the module tests error with `AssertionError: no .tf files in /home/rock/github_projects/mlops-toxic-moderation/infra/smoke`

- [ ] **Step 3: Write minimal implementation**

`infra/smoke/health_server.py`:
```python
"""Minimal /health responder for the throwaway smoke deploy.

Deliberately dependency-free and independent of Phase 2, so the day-9 gate can run
even if the backend image is not ready. Logs to stdout so the awslogs driver has
something to ship.
"""

import http.server
import json
import os
import socketserver

PORT = int(os.environ.get("SMOKE_PORT", "8000"))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        ok = self.path == "/health"
        body = json.dumps({"status": "ok" if ok else "not found", "path": self.path}).encode()
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


class Server(socketserver.TCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    with Server(("", PORT), Handler) as httpd:
        print(f"smoke health server listening on {PORT}", flush=True)
        httpd.serve_forever()
```

`infra/smoke/Dockerfile`:
```dockerfile
FROM public.ecr.aws/docker/library/python:3.11-alpine
COPY health_server.py /app/health_server.py
EXPOSE 8000
CMD ["python", "/app/health_server.py"]
```

`infra/smoke/variables.tf`:
```hcl
variable "project" {
  type    = string
  default = "toxic-mod-smoke"
}

variable "region" {
  type    = string
  default = "us-west-2"
}

variable "az" {
  type    = string
  default = "us-west-2a"
}

variable "ami_id" {
  description = "Pinned AL2023 arm64 AMI. Resolved once by hand; never auto-resolved."
  type        = string

  validation {
    condition     = can(regex("^ami-[0-9a-f]{8,17}$", var.ami_id))
    error_message = "ami_id must be a literal ami-xxxxxxxx identifier."
  }
}

variable "operator_cidrs" {
  description = "Ingress allowlist for port 8000 during the smoke window."
  type        = list(string)
}
```

`infra/smoke/user_data.sh.tftpl`:
```bash
#!/bin/bash
set -euxo pipefail
exec > >(tee /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

dnf -y install docker
systemctl enable --now docker

aws ecr get-login-password --region ${region} \
  | docker login --username AWS --password-stdin ${ecr_registry}

touch /var/lib/cloud/smoke-bootstrapped
```

`infra/smoke/main.tf`:
```hcl
terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.57"
    }
  }

  backend "s3" {
    key          = "phase-a2-smoke/terraform.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Phase     = "A2-smoke"
      Lifetime  = "throwaway"
    }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_vpc" "smoke" {
  cidr_block           = "10.99.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.project }
}

resource "aws_internet_gateway" "smoke" {
  vpc_id = aws_vpc.smoke.id
  tags   = { Name = var.project }
}

resource "aws_subnet" "smoke" {
  vpc_id                  = aws_vpc.smoke.id
  cidr_block              = "10.99.0.0/24"
  availability_zone       = var.az
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.project}-public" }
}

resource "aws_route_table" "smoke" {
  vpc_id = aws_vpc.smoke.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.smoke.id
  }

  tags = { Name = var.project }
}

resource "aws_route_table_association" "smoke" {
  subnet_id      = aws_subnet.smoke.id
  route_table_id = aws_route_table.smoke.id
}

resource "aws_security_group" "smoke" {
  name        = "${var.project}-sg"
  description = "Throwaway smoke instance. No port 22 by design."
  vpc_id      = aws_vpc.smoke.id

  ingress {
    description = "smoke /health"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = var.operator_cidrs
  }

  # Explicit egress. Declaring aws_security_group deletes the default allow-all,
  # and without these three rules the instance never registers with SSM and there
  # is no SSH to fall back to.
  egress {
    description = "ECR, SSM, CloudWatch Logs, Secrets Manager"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "VPC DNS resolver, UDP"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [aws_vpc.smoke.cidr_block]
  }

  egress {
    description = "VPC DNS resolver, TCP fallback for large answers"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.smoke.cidr_block]
  }

  egress {
    description = "Amazon Time Sync; a wrong clock breaks SigV4 and TLS"
    from_port   = 123
    to_port     = 123
    protocol    = "udp"
    cidr_blocks = ["169.254.169.123/32"]
  }

  tags = { Name = var.project }
}

resource "aws_ecr_repository" "smoke" {
  name                 = var.project
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_cloudwatch_log_group" "smoke" {
  name              = "/${var.project}"
  retention_in_days = 14
}

data "aws_iam_policy_document" "smoke_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "smoke" {
  name               = "${var.project}-instance"
  assume_role_policy = data.aws_iam_policy_document.smoke_assume.json
}

resource "aws_iam_role_policy_attachment" "smoke_ssm" {
  role       = aws_iam_role.smoke.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "smoke_inline" {
  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPullSmokeRepoOnly"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.smoke.arn]
  }

  statement {
    sid       = "LogsToSmokeGroupOnly"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.smoke.arn}:*"]
  }
}

resource "aws_iam_role_policy" "smoke_inline" {
  name   = "${var.project}-inline"
  role   = aws_iam_role.smoke.id
  policy = data.aws_iam_policy_document.smoke_inline.json
}

resource "aws_iam_instance_profile" "smoke" {
  name = "${var.project}-instance"
  role = aws_iam_role.smoke.name
}

resource "aws_instance" "smoke" {
  ami                     = var.ami_id
  instance_type           = "t4g.small"
  subnet_id               = aws_subnet.smoke.id
  vpc_security_group_ids  = [aws_security_group.smoke.id]
  iam_instance_profile    = aws_iam_instance_profile.smoke.name
  disable_api_termination = false

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region       = var.region
    ecr_registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
  })

  tags = { Name = var.project, Component = "smoke" }
}

resource "aws_eip" "smoke" {
  domain = "vpc"
  tags   = { Name = var.project }
}

resource "aws_eip_association" "smoke" {
  instance_id   = aws_instance.smoke.id
  allocation_id = aws_eip.smoke.id
}
```

`infra/smoke/outputs.tf`:
```hcl
output "smoke_health_url" {
  value = "http://${aws_eip.smoke.public_ip}:8000/health"
}

output "smoke_instance_id" {
  value = aws_instance.smoke.id
}

output "smoke_ecr_repository_url" {
  value = aws_ecr_repository.smoke.repository_url
}

output "smoke_log_group" {
  value = aws_cloudwatch_log_group.smoke.name
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
make tf-fmt
terraform -chdir=infra/smoke init -backend=false -input=false && terraform -chdir=infra/smoke validate
.venv/bin/pytest tests/infra/test_smoke_health_server.py tests/infra/test_smoke_module.py -v
```
Expected: `Success! The configuration is valid.` and 8 tests PASS.

- [ ] **Step 5: Run the real day-9 smoke deploy and record the evidence**

This is the checkpoint. Every command below is run for real, in order, and its output pasted into `docs/evidence/a2-smoke-deploy.md`.

```bash
export AWS_PROFILE=mlops-admin AWS_REGION=us-west-2
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# 1. Pin the AMI once, by hand. This value is committed; it is never auto-resolved.
AMI=$(aws ssm get-parameter \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query 'Parameter.Value' --output text)
echo "$AMI"

# 2. Stand up the throwaway module.
terraform -chdir=infra/smoke init -backend-config=../terraform/backend.hcl -input=false
terraform -chdir=infra/smoke apply -input=false -auto-approve \
  -var "ami_id=$AMI" -var "operator_cidrs=[\"$(curl -fsS https://checkip.amazonaws.com)/32\"]"

REPO=$(terraform -chdir=infra/smoke output -raw smoke_ecr_repository_url)
IID=$(terraform -chdir=infra/smoke output -raw smoke_instance_id)
URL=$(terraform -chdir=infra/smoke output -raw smoke_health_url)

# 3. Build natively on the aarch64 Jetson and push. Proves ECR auth and arm64.
docker build -t "$REPO:smoke1" infra/smoke
aws ecr get-login-password | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com"
docker push "$REPO:smoke1"

# 4. Proves egress 443 + DNS + NTP. If this list is empty, C6 has recurred.
aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=$IID" \
  --query 'InstanceInformationList[].[InstanceId,PingStatus,PlatformName]' --output table

# 5. Start the container through SSM Run Command. No SSH exists.
CMD=$(aws ssm send-command --instance-ids "$IID" \
  --document-name AWS-RunShellScript \
  --parameters "commands=[\"docker rm -f smoke || true\",\"docker run -d --name smoke --restart=always -p 8000:8000 --log-driver=awslogs --log-opt awslogs-region=us-west-2 --log-opt awslogs-group=/toxic-mod-smoke --log-opt awslogs-stream=smoke ${REPO}:smoke1\"]" \
  --query 'Command.CommandId' --output text)

# Poll to a terminal state. SendCommand is fire-and-forget; exit 0 proves nothing.
until STATUS=$(aws ssm get-command-invocation --command-id "$CMD" --instance-id "$IID" \
      --query Status --output text 2>/dev/null) && \
      [ "$STATUS" != "Pending" ] && [ "$STATUS" != "InProgress" ]; do sleep 5; done
echo "invocation status: $STATUS"
[ "$STATUS" = "Success" ]

# 6. The gate itself.
curl -fsS --max-time 10 "$URL"

# 7. Proves the awslogs driver reaches the log group (H27).
aws logs tail /toxic-mod-smoke --since 10m
```

Gate: step 4 lists the instance with `PingStatus=Online`, step 5's invocation status is `Success`, step 6 prints `{"status": "ok", "path": "/health"}`, and step 7 prints the container's startup line. If any of these fails, the delivery spec's end-of-day-11 checkpoint pre-commits the fallback to console provisioning — but every failure here is cheap to fix on day 9 and expensive on day 13.

- [ ] **Step 6: Destroy the throwaway stack immediately**

```bash
terraform -chdir=infra/smoke destroy -input=false -auto-approve \
  -var "ami_id=$AMI" -var "operator_cidrs=[\"0.0.0.0/32\"]"
aws ec2 describe-addresses --query 'Addresses[].PublicIp' --output text   # must be empty
```
Expected: `Destroy complete!` and no lingering Elastic IP, which is the only resource here that bills while idle.

- [ ] **Step 7: Commit**

```bash
git add infra/smoke tests/infra/test_smoke_module.py tests/infra/test_smoke_health_server.py docs/evidence/a2-smoke-deploy.md
git commit -m "Add throwaway single-instance smoke deploy and record the day-9 checkpoint evidence"
```

---

### Task 3 (C7): Pin the AMI, and prove nothing auto-resolves it

**Finding C7:** `terraform apply` running unattended against an AMI resolved from the SSM public parameter means a routine AL2023 republication forces replacement of all three instances, destroying baked artifacts and pulled images at an arbitrary moment. The premortem's fix list offers three remedies; this task takes two of them and Task 18 takes the third.

**Files:**
- Create: `infra/terraform/ami.auto.tfvars`
- Modify: `infra/terraform/variables.tf`
- Test: `tests/infra/test_ami_pin.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_ami_pin.py`:
```python
"""C7: the AMI is a committed literal, and no data source can move it."""

import re
from pathlib import Path

from tests.infra import tfparse

TFVARS = tfparse.MAIN / "ami.auto.tfvars"


def test_ami_id_variable_exists_and_validates_its_shape():
    ami = tfparse.variables()["ami_id"]
    assert "default" not in ami, "a default would let an unset ami_id apply silently"
    validation = tfparse.blocks(ami, "validation")[0]
    assert "regex" in validation["condition"]
    assert "ami-" in validation["condition"]


def test_ami_is_pinned_in_a_committed_tfvars_file():
    assert TFVARS.exists(), "ami.auto.tfvars must be committed; that is the pin"
    text = TFVARS.read_text()
    assert re.search(r'^ami_id\s*=\s*"ami-[0-9a-f]{8,17}"\s*$', text, re.M), text
    # The pin is only auditable if the resolution date travels with it.
    assert re.search(r"#.*20\d\d-\d\d-\d\d", text), "record the date the AMI was resolved"


def test_no_ssm_parameter_data_source_exists_in_the_root_module():
    # An aws_ssm_parameter data source is exactly how the auto-resolving AMI got in.
    assert "aws_ssm_parameter" not in tfparse.data_kinds()


def test_no_resource_reads_an_ami_from_a_data_source():
    for name, body in tfparse.resources("aws_instance").items():
        assert body["ami"] == "${var.ami_id}", f"{name} does not use the pinned variable"


def test_gitignore_does_not_exclude_the_ami_pin():
    ignore = (Path(tfparse.ROOT) / ".gitignore").read_text()
    assert "infra/terraform/*.auto.tfvars\n" not in ignore
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_ami_pin.py -v`

Expected: 3 failures. `KeyError: 'ami_id'`, `AssertionError: ami.auto.tfvars must be committed; that is the pin`, and `KeyError: 'ami'`-style failures from the empty `aws_instance` set. The two negative tests pass vacuously and will keep passing.

- [ ] **Step 3: Write minimal implementation**

Append to `infra/terraform/variables.tf`:
```hcl
variable "ami_id" {
  description = <<-EOT
    Pinned Amazon Linux 2023 arm64 AMI. Deliberately NOT resolved from
    /aws/service/ami-amazon-latest/... at plan time: an AL2023 republication would
    otherwise force replacement of all three instances at an arbitrary moment,
    destroying baked artifacts and pulled images (premortem C7). Bump it on purpose
    with the command in docs/runbooks/no-ssh-debug.md, then
    `terraform apply -replace=aws_instance.backend` one instance at a time.
  EOT
  type        = string

  validation {
    condition     = can(regex("^ami-[0-9a-f]{8,17}$", var.ami_id))
    error_message = "ami_id must be a literal ami-xxxxxxxx identifier, not an SSM parameter path."
  }
}
```

Resolve the value once and write the file:
```bash
export AWS_PROFILE=mlops-admin AWS_REGION=us-west-2
AMI=$(aws ssm get-parameter \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query 'Parameter.Value' --output text)
printf '# Amazon Linux 2023 arm64, resolved %s. Pinned deliberately: see premortem C7.\n# Re-resolve and bump on purpose only; never wire this to a data source.\nami_id = "%s"\n' \
  "$(date +%F)" "$AMI" > infra/terraform/ami.auto.tfvars
cat infra/terraform/ami.auto.tfvars
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/infra/test_ami_pin.py -v`
Expected: 5 PASS. (`test_no_resource_reads_an_ami_from_a_data_source` passes vacuously until Task 10 adds the instances, at which point it becomes load-bearing.)

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/variables.tf infra/terraform/ami.auto.tfvars tests/infra/test_ami_pin.py
git commit -m "Pin the AL2023 arm64 AMI to a committed variable and forbid auto-resolution"
```

---

### Task 4: `network.tf` — VPC, subnets, internet gateway, routes

Includes `map_public_ip_on_launch` on the public subnets. This is not cosmetic: user data runs during cloud-init, which is before `aws_eip_association` completes, so without an auto-assigned public address the instance has no route to the internet at exactly the moment it is trying to `dnf install docker`. The auto-assigned address is then replaced by the EIP when the association lands.

**Files:**
- Create: `infra/terraform/network.tf`
- Test: `tests/infra/test_network.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_network.py`:
```python
"""VPC shape, and the two subnet properties that decide whether boot works."""

from tests.infra import tfparse

PUBLIC = ["public_a", "public_b"]
PRIVATE = ["private_a", "private_b"]


def test_vpc_has_dns_support_and_hostnames():
    # Without DNS support the VPC resolver does not answer and no AWS endpoint resolves.
    vpc = tfparse.resource("aws_vpc", "main")
    assert vpc["cidr_block"] == "${var.vpc_cidr}"
    assert vpc["enable_dns_support"] is True
    assert vpc["enable_dns_hostnames"] is True


def test_public_subnets_map_a_public_ip_on_launch():
    for name in PUBLIC:
        subnet = tfparse.resource("aws_subnet", name)
        assert subnet["map_public_ip_on_launch"] is True, (
            f"{name}: user data runs before the EIP associates; without an "
            "auto-assigned address cloud-init has no route out"
        )


def test_private_subnets_never_map_a_public_ip():
    for name in PRIVATE:
        subnet = tfparse.resource("aws_subnet", name)
        assert subnet.get("map_public_ip_on_launch", False) is False


def test_private_subnets_have_no_route_to_the_internet_gateway():
    private_rt = tfparse.resource("aws_route_table", "private")
    assert tfparse.blocks(private_rt, "route") == [], "RDS must have no internet path"


def test_public_route_table_has_a_default_route_via_the_igw():
    route = tfparse.blocks(tfparse.resource("aws_route_table", "public"), "route")[0]
    assert route["cidr_block"] == "0.0.0.0/0"
    assert route["gateway_id"] == "${aws_internet_gateway.main.id}"


def test_there_is_no_nat_gateway():
    # A NAT gateway is roughly a third of the monthly ceiling on its own.
    assert tfparse.resources("aws_nat_gateway") == {}


def test_all_four_subnets_are_spread_across_two_azs():
    zones = {tfparse.resource("aws_subnet", n)["availability_zone"] for n in PUBLIC + PRIVATE}
    assert zones == {"${var.azs[0]}", "${var.azs[1]}"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_network.py -v`
Expected: FAIL with `AssertionError: missing resource aws_vpc.main; found []`

- [ ] **Step 3: Write minimal implementation**

`infra/terraform/network.tf`:
```hcl
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.project }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = var.project }
}

# Public subnets. map_public_ip_on_launch is required, not optional: cloud-init runs
# before aws_eip_association completes, so without an auto-assigned address the
# instance has no route out while user data is installing Docker. The EIP replaces
# the auto-assigned address once the association lands.
resource "aws_subnet" "public_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidrs[0]
  availability_zone       = var.azs[0]
  map_public_ip_on_launch = true

  tags = { Name = "${var.project}-public-a", Tier = "public" }
}

resource "aws_subnet" "public_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidrs[1]
  availability_zone       = var.azs[1]
  map_public_ip_on_launch = true

  tags = { Name = "${var.project}-public-b", Tier = "public" }
}

resource "aws_subnet" "private_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.private_subnet_cidrs[0]
  availability_zone       = var.azs[0]
  map_public_ip_on_launch = false

  tags = { Name = "${var.project}-private-a", Tier = "private" }
}

resource "aws_subnet" "private_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.private_subnet_cidrs[1]
  availability_zone       = var.azs[1]
  map_public_ip_on_launch = false

  tags = { Name = "${var.project}-private-b", Tier = "private" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.project}-public" }
}

# Deliberately routeless. There is no NAT gateway (roughly a third of the monthly
# ceiling) and RDS needs no internet path.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${var.project}-private" }
}

resource "aws_route_table_association" "public_a" {
  subnet_id      = aws_subnet.public_a.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "public_b" {
  subnet_id      = aws_subnet.public_b.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private_a" {
  subnet_id      = aws_subnet.private_a.id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "private_b" {
  subnet_id      = aws_subnet.private_b.id
  route_table_id = aws_route_table.private.id
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make tf-fmt && terraform -chdir=infra/terraform validate && .venv/bin/pytest tests/infra/test_network.py -v`
Expected: `Success! The configuration is valid.` and 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/network.tf tests/infra/test_network.py
git commit -m "Add VPC, two public and two private subnets, and routing with no NAT gateway"
```

---

### Task 5 (C6, H16): Per-tier security groups with explicit egress

**Finding C6:** no egress rule is specified anywhere in any spec, and `aws_security_group` **deletes** the default `0.0.0.0/0` egress the moment the resource is declared. The instance then boots, `dnf install docker` hangs, the SSM Agent never reaches `ssm`/`ssmmessages`/`ec2messages` on 443, the instance never appears in SSM inventory, and there is no SSH, no bastion, and no NAT. The sole remaining channel is the one that is broken.

**Finding H16:** one security group across three instances means a Streamlit RCE on the internet-facing box reaches the backend and the database as freely as the backend does. Four groups, one per tier.

Two egress rules beyond 443 are load-bearing and are the part that is easy to miss twice. Name resolution of `ssm.us-west-2.amazonaws.com` goes to the VPC resolver on **UDP 53**, and SigV4 request signing and TLS certificate validation both fail on a skewed clock, which the Amazon Time Sync Service fixes over **UDP 123** to `169.254.169.123`. Egress restricted to 443/tcp alone reproduces C6 exactly one layer down.

The cross-tier references are deliberately asymmetric to avoid a Terraform dependency cycle: an app group's *egress* to a downstream tier uses that tier's subnet CIDRs, while the downstream group's *ingress* uses the upstream security group id. Referencing security group ids in both directions is a cycle Terraform refuses to plan.

**Files:**
- Modify: `infra/terraform/network.tf`
- Test: `tests/infra/test_security_groups.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_security_groups.py`:
```python
"""C6 and H16: explicit egress, per-tier isolation, and no administrative ingress."""

import pytest

from tests.infra import tfparse

APP_GROUPS = ["backend", "frontend", "monitoring"]
ALL_GROUPS = APP_GROUPS + ["db"]
LISTEN_PORT = {"backend": 8000, "frontend": 8501, "monitoring": 8502}


def _rules(group: str, direction: str) -> list[dict]:
    return tfparse.blocks(tfparse.resource("aws_security_group", group), direction)


@pytest.mark.parametrize("group", APP_GROUPS)
def test_every_app_group_declares_an_egress_block_at_all(group):
    # The whole of C6 in one assertion: a declared aws_security_group with no egress
    # block has NO egress, and this design has no SSH to recover through.
    assert _rules(group, "egress"), f"sg-{group} has no egress; the instance is stranded"


@pytest.mark.parametrize("group", APP_GROUPS)
def test_every_app_group_can_reach_443_dns_and_ntp(group):
    ports = {(r["from_port"], r["protocol"]) for r in _rules(group, "egress")}
    assert (443, "tcp") in ports, "no 443: no ECR, no SSM, no Secrets Manager, no W&B"
    assert (53, "udp") in ports, "no UDP 53: ssm.us-west-2.amazonaws.com never resolves"
    assert (53, "tcp") in ports, "no TCP 53: truncated DNS answers fail"
    assert (123, "udp") in ports, "no NTP: clock skew breaks SigV4 signing and TLS"


@pytest.mark.parametrize("group", APP_GROUPS)
def test_dns_egress_is_scoped_to_the_vpc_not_the_internet(group):
    dns = [r for r in _rules(group, "egress") if r["from_port"] == 53]
    for rule in dns:
        assert rule["cidr_blocks"] == ["${aws_vpc.main.cidr_block}"]


@pytest.mark.parametrize("group", ALL_GROUPS)
def test_no_group_opens_port_22_in_either_direction(group):
    for direction in ("ingress", "egress"):
        for rule in _rules(group, direction):
            lo, hi = rule["from_port"], rule["to_port"]
            assert not (lo <= 22 <= hi), f"sg-{group} {direction} spans 22"


@pytest.mark.parametrize("group", APP_GROUPS)
def test_each_tier_listens_on_its_own_port_only(group):
    ports = {r["from_port"] for r in _rules(group, "ingress")}
    assert ports == {LISTEN_PORT[group]}, f"sg-{group} ingress ports {sorted(ports)}"


def test_reviewer_ui_port_8503_has_no_ingress_anywhere():
    # H15/H12: the reviewer shared secret never crosses the internet in cleartext
    # because the reviewer UI is not reachable from the internet at all. The operator
    # gets to it with `aws ssm start-session --document-name
    # AWS-StartPortForwardingSession`, which rides the instance's existing 443 egress.
    for group in ALL_GROUPS:
        for rule in _rules(group, "ingress"):
            assert not (rule["from_port"] <= 8503 <= rule["to_port"])


def test_database_group_accepts_5432_from_the_three_app_groups_only():
    ingress = _rules("db", "ingress")
    assert len(ingress) == 1
    rule = ingress[0]
    assert rule["from_port"] == 5432 and rule["to_port"] == 5432
    assert set(rule["security_groups"]) == {
        "${aws_security_group.backend.id}",
        "${aws_security_group.frontend.id}",
        "${aws_security_group.monitoring.id}",
    }
    assert "cidr_blocks" not in rule, "the database must never be reachable by CIDR"


def test_database_group_has_no_egress_at_all():
    assert _rules("db", "egress") == [], "RDS initiates nothing; give it no way out"


def test_frontend_cannot_reach_the_database_by_a_wide_open_egress():
    wide = [
        r
        for r in _rules("frontend", "egress")
        if r.get("cidr_blocks") == ["0.0.0.0/0"] and r["from_port"] != 443
    ]
    assert wide == []


def test_public_listeners_use_the_operator_allowlist_plus_the_demo_toggle():
    for group in APP_GROUPS:
        public = [r for r in _rules(group, "ingress") if "cidr_blocks" in r]
        assert public, f"sg-{group} has no operator ingress"
        for rule in public:
            assert rule["cidr_blocks"] == "${concat(var.operator_cidrs, var.demo_cidrs)}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_security_groups.py -v`
Expected: every test fails with `AssertionError: missing resource aws_security_group.backend; found []`

- [ ] **Step 3: Write minimal implementation**

Append to `infra/terraform/network.tf`:
```hcl
# ---------------------------------------------------------------------------
# Security groups: one per tier (premortem H16).
#
# EVERY group declares explicit egress. Terraform removes the default
# 0.0.0.0/0 egress the instant an aws_security_group resource exists, and this
# design has no SSH, no bastion, and no NAT, so a group without egress produces
# an instance that never registers with SSM and cannot be reached at all
# (premortem C6). 443 alone is not sufficient: DNS is UDP 53 to the VPC
# resolver and the clock comes from 169.254.169.123 on UDP 123.
#
# Cross-tier references are one-directional on purpose. Egress toward a
# downstream tier uses that tier's subnet CIDRs; the downstream group's ingress
# uses the upstream group's id. Referencing ids in both directions is a
# dependency cycle Terraform refuses to plan.
# ---------------------------------------------------------------------------

locals {
  public_ingress_cidrs = concat(var.operator_cidrs, var.demo_cidrs)
  private_cidrs        = var.private_subnet_cidrs
  public_cidrs         = var.public_subnet_cidrs
}

resource "aws_security_group" "backend" {
  name        = "${var.project}-backend"
  description = "EC2 #1 FastAPI. No port 22: access is SSM Session Manager."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "FastAPI /predict and /health"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = local.public_ingress_cidrs
  }

  ingress {
    description     = "FastAPI from the frontend tier"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.frontend.id]
  }

  egress {
    description = "ECR, SSM, CloudWatch Logs, Secrets Manager, W&B"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "VPC DNS resolver, UDP"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "VPC DNS resolver, TCP fallback"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "Amazon Time Sync Service"
    from_port   = 123
    to_port     = 123
    protocol    = "udp"
    cidr_blocks = ["169.254.169.123/32"]
  }

  egress {
    description = "Postgres in the private subnets"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = local.private_cidrs
  }

  tags = { Name = "${var.project}-backend", Component = "backend" }
}

resource "aws_security_group" "frontend" {
  name        = "${var.project}-frontend"
  description = "EC2 #2 Streamlit user UI on 8501. Reviewer UI on 8503 has NO ingress."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "Streamlit user UI"
    from_port   = 8501
    to_port     = 8501
    protocol    = "tcp"
    cidr_blocks = local.public_ingress_cidrs
  }

  egress {
    description = "ECR, SSM, CloudWatch Logs, Secrets Manager"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "VPC DNS resolver, UDP"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "VPC DNS resolver, TCP fallback"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "Amazon Time Sync Service"
    from_port   = 123
    to_port     = 123
    protocol    = "udp"
    cidr_blocks = ["169.254.169.123/32"]
  }

  egress {
    description = "Backend /predict"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = local.public_cidrs
  }

  egress {
    description = "Postgres for the reviewer workflow"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = local.private_cidrs
  }

  tags = { Name = "${var.project}-frontend", Component = "frontend" }
}

resource "aws_security_group" "monitoring" {
  name        = "${var.project}-monitoring"
  description = "EC2 #3 monitoring dashboard. Reads Postgres as monitor_ro only."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "Monitoring dashboard"
    from_port   = 8502
    to_port     = 8502
    protocol    = "tcp"
    cidr_blocks = local.public_ingress_cidrs
  }

  egress {
    description = "ECR, SSM, CloudWatch Logs, Secrets Manager"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "VPC DNS resolver, UDP"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "VPC DNS resolver, TCP fallback"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "Amazon Time Sync Service"
    from_port   = 123
    to_port     = 123
    protocol    = "udp"
    cidr_blocks = ["169.254.169.123/32"]
  }

  egress {
    description = "Postgres, read-only role"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = local.private_cidrs
  }

  tags = { Name = "${var.project}-monitoring", Component = "monitoring" }
}

resource "aws_security_group" "db" {
  name        = "${var.project}-db"
  description = "RDS Postgres. Reachable only from the three app groups; no egress."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "Postgres from the three application tiers"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    security_groups = [
      aws_security_group.backend.id,
      aws_security_group.frontend.id,
      aws_security_group.monitoring.id,
    ]
  }

  # No egress block on purpose. RDS initiates no outbound connection here, and an
  # empty egress set is the correct posture rather than an omission.

  tags = { Name = "${var.project}-db", Component = "db" }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make tf-fmt && terraform -chdir=infra/terraform validate && .venv/bin/pytest tests/infra/test_security_groups.py -v`
Expected: `Success! The configuration is valid.` and 22 PASS (parametrised).

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/network.tf tests/infra/test_security_groups.py
git commit -m "Add per-tier security groups with explicit 443, DNS, NTP and database egress"
```

---

### Task 5a (C6, H12, H15, H16): One Terraform root module, one declaration of every security group [gap `C6/H12/H15/H16-terraform-duplicate-declaration`]

Phase 3 Task 12 ships `infra/terraform/app_ingress.tf` declaring `aws_security_group.backend | frontend | reviewer | monitoring | db` and `variable "operator_cidrs"`. Task 5 above appends four of the same security-group names to `network.tf` and declares the same variable in `variables.tf`. **They are the same root module.** Terraform aborts with `Error: Duplicate resource "aws_security_group" "backend" configuration` and `Error: Duplicate variable declaration`, so `terraform validate` fails — and `terraform validate` is a required CI job in Phase 4 Task 8, so main goes red and no deploy is possible until someone deletes one file at 2 a.m. on day 10. Phase 3's file header even says "Phase A2 consumes these; it must not redeclare them", and this plan never mentions `app_ingress.tf` anywhere. Neither suite can detect the collision, because each parses only its own file.

The two files also disagree on substance, and three of the disagreements are security findings rather than style:

| | Phase 3 `app_ingress.tf` | Task 5 `network.tf` | Resolution here |
|---|---|---|---|
| demo toggle variable | `demo_ingress_cidrs` | `demo_cidrs` | `demo_cidrs` |
| groups | five (incl. `reviewer`) | four (no `reviewer`) | five |
| port 8503 | ingress from `var.operator_cidrs` — a **public-internet CIDR** | no ingress anywhere | **no ingress anywhere** (H15) |
| db ingress | — | `{backend, frontend, monitoring}` | `{backend, monitoring}` (H16) |
| egress | 443/tcp only | 443 + UDP 53 + TCP 53 + UDP 123 | the four-rule set, for **every** group including `reviewer` |

Two of those need saying plainly.

**H15.** `docs/tls-decision.md` (Task 17) accepts cleartext HTTP *only* because "the reviewer UI has no ingress rule on any security group … The shared secret therefore never crosses the internet at all", enforced by `test_reviewer_ui_port_8503_has_no_ingress_anywhere`. That test iterates a hardcoded `ALL_GROUPS = ["backend","frontend","monitoring","db"]` and reads only **inline `ingress` blocks**. Phase 3's `aws_vpc_security_group_ingress_rule "reviewer_operator_only"` is a different resource type on a group name the list does not contain, so the guard cannot see it — and Phase 3's own `test_reviewer_rule_is_restricted_to_the_operator_cidrs` asserts the rule **must** exist. The reviewer shared secret crosses the internet in cleartext with both suites green.

**H16.** Task 11 grants the internet-facing Streamlit tier `secretsmanager:GetSecretValue` on `aws_db_instance.main.master_user_secret[0].secret_arn`, and Task 5 gives that same tier 5432 to the database. Network path plus master credential is H16's harm sentence verbatim — "a Streamlit RCE on the internet-facing box yields … master-user read/write on all three tables" — and it contradicts Phase 3's binding principle 1, "No UI container holds a database write credential." The frontend reaches the database **through the backend API**, which is the whole point of having a backend tier.

**Files:**
- Delete: `infra/terraform/app_ingress.tf` (Phase 3 Task 12 keeps `infra/exposure.py` only; see the scope correction in that task)
- Modify: `infra/terraform/network.tf` (add `aws_security_group.reviewer` with egress and NO ingress; drop `frontend` from the db ingress; add `locals.ports` mirroring `infra/exposure.py`)
- Test: `tests/infra/test_security_groups.py` (append), `tests/unit/test_exposure_contract.py` (Phase 3)

- [ ] **Step 1: Write the failing test**

Append to `tests/infra/test_security_groups.py`:
```python
import re
import subprocess
from pathlib import Path

TF = Path("infra/terraform")
ALL_TIERS = ["backend", "frontend", "monitoring", "reviewer"]


def test_no_security_group_or_variable_is_declared_twice_in_the_root_module():
    """Phase 3 and Phase A2 both wanted to own the app-tier groups. Terraform allows
    exactly one declaration per address, and `terraform validate` is a required check."""
    seen: dict[str, str] = {}
    for path in sorted(TF.glob("*.tf")):
        body = path.read_text(encoding="utf-8")
        addresses = [
            f"aws_security_group.{name}"
            for name in re.findall(r'^resource\s+"aws_security_group"\s+"([^"]+)"', body, re.M)
        ] + [
            f"variable.{name}"
            for name in re.findall(r'^variable\s+"([^"]+)"', body, re.M)
        ]
        for address in addresses:
            assert address not in seen, (
                f"{address} declared in both {seen[address]} and {path.name}; "
                "terraform validate fails and CI is a required check"
            )
            seen[address] = path.name


def test_the_root_module_actually_validates():
    subprocess.run(
        ["terraform", f"-chdir={TF}", "init", "-backend=false", "-input=false"], check=True
    )
    subprocess.run(["terraform", f"-chdir={TF}", "validate"], check=True)


def test_only_one_demo_toggle_variable_name_exists_repo_wide():
    offenders = [p.name for p in TF.glob("*.tf") if "demo_ingress_cidrs" in p.read_text(encoding="utf-8")]
    assert not offenders, f"two names for one toggle: {offenders}; the canonical name is demo_cidrs"


def test_the_reviewer_group_exists_with_egress_and_no_ingress():
    assert _rules("reviewer", "egress"), "the reviewer tier is stranded without egress (C6)"
    assert _rules("reviewer", "ingress") == [], "8503 ingress falsifies docs/tls-decision.md (H15)"


def test_no_ingress_rule_of_any_kind_anywhere_reaches_8503():
    """H15. The previous guard iterated a fixed group list and parsed only inline `ingress`
    blocks, so it could not see `aws_vpc_security_group_ingress_rule` on a group name it did
    not enumerate. docs/tls-decision.md's entire acceptance of cleartext rests on 8503 having
    no ingress, so the scan must be over every .tf file and every rule form."""
    candidates = list(tfparse.all_resources("aws_vpc_security_group_ingress_rule"))
    for group in tfparse.all_resources("aws_security_group"):
        candidates.extend(tfparse.blocks(group, "ingress"))
    for rule in candidates:
        lo, hi = int(rule["from_port"]), int(rule["to_port"])
        assert not (lo <= 8503 <= hi), (
            f"8503 has ingress: {rule}; docs/tls-decision.md's acceptance of cleartext "
            "depends on it having none (H15)"
        )


def test_only_the_backend_and_monitoring_tiers_may_reach_5432():
    """H16 and Phase 3 principle 1: no UI container holds a database credential, and the
    user UI reaches Postgres only through the backend API."""
    rule = _rules("db", "ingress")[0]
    assert set(rule["security_groups"]) == {
        "${aws_security_group.backend.id}",
        "${aws_security_group.monitoring.id}",
    }, "the user UI tier must reach Postgres only through the backend API (H16)"


@pytest.mark.parametrize("group", ALL_TIERS)
def test_every_group_including_reviewer_reaches_dns_and_ntp(group):
    ports = {(r["from_port"], r["protocol"]) for r in _rules(group, "egress")}
    assert {(443, "tcp"), (53, "udp"), (53, "tcp"), (123, "udp")} <= ports


def test_the_terraform_ports_mirror_the_python_exposure_contract():
    block = re.search(r"locals\s*\{(.*?)\n\}", (TF / "network.tf").read_text(encoding="utf-8"), re.S)
    assert block, "network.tf must declare locals.ports so the Python contract can be compared"
    declared = {n: int(v) for n, v in re.findall(r"(\w+)\s*=\s*(\d+)", block.group(1))}
    assert declared["reviewer_ui"] == 8503
```

**Amend `test_database_group_accepts_5432_from_the_three_app_groups_only`** in Task 5's Step 1: rename it `test_database_group_accepts_5432_from_the_backend_and_monitoring_only` and remove `"${aws_security_group.frontend.id}"` from the expected set.

**Amend `test_reviewer_ui_port_8503_has_no_ingress_anywhere`** in Task 5's Step 1: it stays, and `test_no_ingress_rule_of_any_kind_anywhere_reaches_8503` above supersedes its scope. Keep both; the narrow one gives the better failure message on the common case.

If `tests/infra/tfparse.py` has no `all_resources(kind)` helper, add one: it must walk **every** `.tf` file in `infra/terraform/` and return every block of that resource type, rather than resolving a single named address.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_security_groups.py -v`
Expected: FAIL — `aws_security_group.backend declared in both app_ingress.tf and network.tf`, a non-zero `terraform validate` (`Duplicate resource configuration` / `Duplicate variable declaration`), `8503 has ingress: {…reviewer_operator_only…}`, and `AssertionError: the user UI tier must reach Postgres only through the backend API (H16)`.

- [ ] **Step 3: Write minimal implementation**

1. `git rm infra/terraform/app_ingress.tf`.
2. Remove `"${aws_security_group.frontend.id}"` from `aws_security_group.db`'s ingress in `network.tf`. The frontend calls `BACKEND_URL`; it has no `DATABASE_URL` and now no network path to one.
3. Append the reviewer group to `network.tf`:
```hcl
# The reviewer console tier. Egress only: 8503 has NO ingress rule of any kind, on any
# group, anywhere. That is not an omission — it is the sole reason docs/tls-decision.md
# can accept cleartext HTTP (premortem H15). The operator reaches it with
#   aws ssm start-session --document-name AWS-StartPortForwardingSession
# which rides this group's existing 443 egress and is TLS-encrypted end to end.
resource "aws_security_group" "reviewer" {
  name        = "${var.project}-reviewer"
  description = "Reviewer console on ${local.ports.reviewer_ui}. No ingress, by design."
  vpc_id      = aws_vpc.main.id

  egress {
    description = "HTTPS: SSM, ECR, Secrets Manager"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    description = "DNS over UDP to the VPC resolver"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }
  egress {
    description = "DNS over TCP for truncated answers"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }
  egress {
    description = "NTP: SigV4 and TLS both fail on a skewed clock"
    from_port   = 123
    to_port     = 123
    protocol    = "udp"
    cidr_blocks = ["169.254.169.123/32"]
  }
  egress {
    description = "Backend API"
    from_port   = local.ports.backend
    to_port     = local.ports.backend
    protocol    = "tcp"
    cidr_blocks = local.private_cidrs
  }

  tags = { Name = "${var.project}-reviewer", Component = "reviewer" }
}
```
4. Add the ports local to `network.tf`, mirroring `infra/exposure.py` so Phase 3's `test_the_python_contract_matches_the_terraform_locals` has something to compare:
```hcl
locals {
  ports = {
    backend      = 8000
    frontend     = 8501
    monitoring   = 8502
    reviewer_ui  = 8503
  }
}
```
5. The reviewer container runs on EC2 #2 alongside the user UI. Attach **both** `aws_security_group.frontend` and `aws_security_group.reviewer` to that instance in `compute.tf` (Task 13): the instance needs the frontend group's 8501 ingress and the reviewer group's egress, and neither group opens 8503.

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
make tf-fmt && terraform -chdir=infra/terraform validate \
  && .venv/bin/pytest tests/infra/test_security_groups.py tests/unit/test_exposure_contract.py -v
```
Expected: `Success! The configuration is valid.` and both suites green.

- [ ] **Step 5: Commit**

```bash
git rm infra/terraform/app_ingress.tf
git add infra/terraform/network.tf tests/infra/test_security_groups.py tests/unit/test_exposure_contract.py
git commit -m "Declare every security group once, in the Terraform root module"
```

---

### Task 6 (C6): The no-SSH debug runbook

The second half of C6. Explicit egress stops the instance being stranded; the runbook is what you reach for when it happens anyway. It must name `aws ec2 get-console-output`, `aws ssm describe-instance-information`, and the EC2 Serial Console, because those are the only three channels that survive a broken SSM agent.

**Files:**
- Create: `docs/runbooks/no-ssh-debug.md`
- Test: `tests/infra/test_docs_controls.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_docs_controls.py`:
```python
"""Documented controls that would otherwise be memos: C6 runbook, H7 cost, H15 TLS."""

from pathlib import Path

import pytest

from tests.infra import tfparse

DOCS = Path(tfparse.ROOT) / "docs"
RUNBOOK = DOCS / "runbooks" / "no-ssh-debug.md"


def test_no_ssh_runbook_exists():
    assert RUNBOOK.exists()


@pytest.mark.parametrize(
    "phrase",
    [
        "aws ec2 get-console-output",
        "aws ssm describe-instance-information",
        "EC2 Serial Console",
        "aws ec2 send-serial-console-ssh-public-key",
        "aws ssm start-session",
    ],
)
def test_runbook_names_every_surviving_channel(phrase):
    assert phrase in RUNBOOK.read_text(), f"{phrase} missing from the no-SSH runbook"


def test_runbook_covers_the_dns_and_ntp_egress_failure_mode():
    text = RUNBOOK.read_text()
    assert "udp/53" in text.lower() or "udp 53" in text.lower()
    assert "169.254.169.123" in text


def test_runbook_documents_the_imdsv2_hop_limit_tradeoff():
    text = RUNBOOK.read_text()
    assert "http_put_response_hop_limit" in text
    assert "SSRF" in text


def test_runbook_documents_the_deliberate_ami_bump_procedure():
    assert "terraform apply -replace=aws_instance" in RUNBOOK.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_docs_controls.py -v`
Expected: 9 failures, the first being `AssertionError: assert False` from `RUNBOOK.exists()` and the rest `FileNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`docs/runbooks/no-ssh-debug.md`:
```markdown
# Runbook: debugging an EC2 instance with no SSH

This project has no port 22, no key pair, no bastion, and no NAT gateway. That is a
deliberate trade: it removes an entire class of key-management and exposure problems,
and it means the *only* interactive channel is AWS Systems Manager. When SSM itself is
what is broken, the channels below are what is left, in the order to try them.

All commands assume `export AWS_PROFILE=mlops-admin AWS_REGION=us-west-2`.

## 0. What usually broke

In order of observed frequency for this stack:

1. **Egress.** `aws_security_group` deletes the default `0.0.0.0/0` egress the moment
   the resource is declared. Without egress the SSM Agent never reaches `ssm`,
   `ssmmessages`, or `ec2messages` on 443 and the instance never registers.
2. **DNS.** Egress on 443/tcp alone is not enough. The VPC resolver answers on
   **udp/53** (and tcp/53 for truncated answers) inside the VPC CIDR. With no DNS,
   `ssm.us-west-2.amazonaws.com` never resolves and the symptom looks identical to (1).
3. **Clock.** SigV4 request signing and TLS certificate validation both fail on a
   skewed clock. Amazon Time Sync lives at **169.254.169.123 on udp/123**.
4. **Instance profile.** No `AmazonSSMManagedInstanceCore`, no registration.
5. **No route.** A public subnet without `map_public_ip_on_launch = true` gives
   cloud-init no route out before the Elastic IP associates, so user data hangs at
   `dnf install docker` and never finishes.

## 1. Is it registered at all?

```bash
aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=$IID" \
  --query 'InstanceInformationList[].[InstanceId,PingStatus,AgentVersion,LastPingDateTime]' \
  --output table
```

Empty output means the agent has never checked in: go to §0 items 1 through 4.
`PingStatus=ConnectionLost` means it registered once and then lost egress.

## 2. What did the boot say?

```bash
aws ec2 get-console-output --instance-id "$IID" --latest --output text | tail -n 120
```

This needs no agent, no network from the instance, and no credentials on the instance.
It is the highest-value first command. Look for cloud-init failures, `dnf` timeouts
(egress), and `Failed to resolve host` (DNS).

## 3. Interactive shell, if the agent is alive

```bash
aws ssm start-session --target "$IID"
```

Port-forward a service that has no ingress rule at all, which is how the reviewer UI
on 8503 is reached:

```bash
aws ssm start-session --target "$IID" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8503"],"localPortNumber":["8503"]}'
```

## 4. EC2 Serial Console, when the agent is dead

The EC2 Serial Console works over the EC2 control plane rather than the instance's
network, so it survives a completely broken security group. It is supported on Nitro
instances, which includes every `t4g` and `c7g` used here.

Enable it once per account:

```bash
aws ec2 enable-serial-console-access
aws ec2 get-serial-console-access-status
```

Amazon Linux 2023 has no password set for `ec2-user`, so push an ephemeral key first
(it lives for 60 seconds), then connect:

```bash
ssh-keygen -t ed25519 -f /tmp/serial -N ''
aws ec2 send-serial-console-ssh-public-key \
  --instance-id "$IID" --serial-port 0 \
  --ssh-public-key "file:///tmp/serial.pub"
ssh -i /tmp/serial "${IID}.port0@serial-console.ec2-instance-connect.us-west-2.aws"
```

This is the only channel that does not depend on the instance's own networking. It is
also why the no-SSH posture is safe: the break-glass is control-plane authenticated,
not a standing open port.

## 5. Verify the egress rules that matter

```bash
aws ec2 describe-security-groups --group-ids "$SG" \
  --query 'SecurityGroups[].IpPermissionsEgress[].[IpProtocol,FromPort,ToPort,IpRanges[].CidrIp]' \
  --output table
```

The set must contain 443/tcp to `0.0.0.0/0`, 53/udp and 53/tcp to the VPC CIDR, and
123/udp to `169.254.169.123/32`. Anything less reproduces §0.

## 6. IMDSv2 and the hop-limit tradeoff

Instances set `http_tokens = "required"` (IMDSv2 only, which closes the classic
SSRF-to-credential-theft path) and `http_put_response_hop_limit = 2`.

The hop limit is a deliberate trade. The default of 1 means a request from **inside a
Docker container** on the default bridge network is one hop too far and cannot reach
`169.254.169.254`, so the container cannot obtain instance-profile credentials for ECR
or Secrets Manager. Raising it to 2 restores that — and simultaneously widens the SSRF
blast radius, because an SSRF in a containerised application can now reach IMDS where
it previously could not. Accepted, because every container here needs AWS credentials
and the alternative is static keys on disk. The compensating controls are that IMDSv1
is disabled outright and each instance profile is scoped to its own tier's resources,
so an SSRF yields that tier's permissions and no other tier's.

Symptom of getting this wrong: `docker login` in user data succeeds (the host has hop
1) while an in-container `aws` call fails with `Unable to locate credentials`.

## 7. Bumping the pinned AMI on purpose

The AMI is pinned in `infra/terraform/ami.auto.tfvars` and every instance carries
`lifecycle { ignore_changes = [ami] }`, so a bump is a two-step deliberate act rather
than a surprise replacement of all three instances during grading.

```bash
aws ssm get-parameter \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query 'Parameter.Value' --output text
# edit infra/terraform/ami.auto.tfvars, commit, then one instance at a time:
terraform -chdir=infra/terraform apply -replace=aws_instance.backend
```

Never replace more than one instance in a single apply. The stack has no load
balancer, so a simultaneous replacement is a full outage plus three cold ECR pulls.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/infra/test_docs_controls.py -v`
Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/runbooks/no-ssh-debug.md tests/infra/test_docs_controls.py
git commit -m "Add the no-SSH debug runbook covering console output, SSM and serial console"
```

---

### Task 7: `ecr.tf` — four repositories, immutable tags, scan on push

Four repositories because the system has four containers: backend, frontend, monitoring, rescorer. Immutable tags are what make "the deployed container traces back to an exact commit" true rather than aspirational. `keep last 30` rather than the spec's `keep last 10`, because the dropped-ledger entry in the premortem folded ECR retention into the rollback remediation: ten tags is under two days of commits on this schedule, and the rollback plan needs a target that still exists.

**Files:**
- Create: `infra/terraform/ecr.tf`
- Modify: `tests/infra/tfparse.py`
- Test: `tests/infra/test_ecr.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_ecr.py`:
```python
"""ECR: four repositories, immutable tags, and a teardown that actually completes."""

import json

from tests.infra import tfparse

COMPONENTS = {"backend", "frontend", "monitoring", "rescorer"}


def test_component_list_names_all_four_containers():
    assert set(tfparse.local_values()["components"]) == COMPONENTS


def test_one_repository_per_component():
    repo = tfparse.resource("aws_ecr_repository", "app")
    assert repo["for_each"] == "${toset(local.components)}"
    assert repo["name"] == "${var.project}-${each.value}"


def test_tags_are_immutable_and_images_are_scanned_on_push():
    repo = tfparse.resource("aws_ecr_repository", "app")
    assert repo["image_tag_mutability"] == "IMMUTABLE"
    assert tfparse.blocks(repo, "image_scanning_configuration")[0]["scan_on_push"] is True


def test_force_delete_is_on_so_terraform_destroy_completes():
    # terraform destroy is cost control #2. A repository holding images blocks it,
    # and a half-destroyed stack keeps billing.
    assert tfparse.resource("aws_ecr_repository", "app")["force_delete"] is True


def test_lifecycle_policy_keeps_enough_tags_to_roll_back_to():
    policy = tfparse.resource("aws_ecr_lifecycle_policy", "app")["policy"]
    # The policy is a jsonencode(...) expression; assert on the literal it renders.
    assert '"countNumber": 30' in policy or "countNumber\": 30" in policy or "30" in policy
    assert "sha-" in policy, "tags are keyed by git SHA; the rule must match that prefix"
    assert "untagged" in policy


def test_repository_urls_are_not_hardcoded_anywhere():
    text = json.dumps(tfparse.load())
    assert "dkr.ecr.us-west-2.amazonaws.com" not in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_ecr.py -v`
Expected: FAIL with `AttributeError: module 'tests.infra.tfparse' has no attribute 'local_values'` on the first test and `AssertionError: missing resource aws_ecr_repository.app; found []` on the rest.

- [ ] **Step 3: Write minimal implementation**

Append to `tests/infra/tfparse.py`:
```python
def local_values(tf_dir: Path = MAIN) -> dict:
    """Merged contents of every `locals { }` block in the module."""
    return _flatten("locals", tf_dir)
```

`infra/terraform/ecr.tf`:
```hcl
locals {
  components = ["backend", "frontend", "monitoring", "rescorer"]
}

resource "aws_ecr_repository" "app" {
  for_each = toset(local.components)

  name                 = "${var.project}-${each.value}"
  image_tag_mutability = "IMMUTABLE"

  # Required for `terraform destroy` to complete: a repository holding images
  # cannot be deleted, and a half-destroyed stack keeps billing. Teardown is
  # cost control #2, so nothing may block it.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Component = each.value }
}

resource "aws_ecr_lifecycle_policy" "app" {
  for_each = aws_ecr_repository.app

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the last 30 SHA-tagged images so a rollback target still exists"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 30
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Expire untagged layers after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
    ]
  })
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make tf-fmt && terraform -chdir=infra/terraform validate && .venv/bin/pytest tests/infra/test_ecr.py -v`
Expected: `Success! The configuration is valid.` and 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/ecr.tf tests/infra/tfparse.py tests/infra/test_ecr.py
git commit -m "Add four ECR repositories with immutable tags and a rollback-sized retention rule"
```

---

### Task 8 (H27, part 1): `observability.tf` — log groups, SNS, CloudTrail, GuardDuty

**Finding H27:** no system observability. No container logs leave the box because no log driver is configured anywhere, and the only alarm in the entire design is for root sign-in. Nothing pages when `/predict` is down — which the design *makes* a designed behaviour whenever RDS is unreachable.

This task creates the destinations. Task 12 grants write access to them, Task 14 points the `awslogs` driver at them, and Task 15 adds the health alarm.

**Files:**
- Create: `infra/terraform/observability.tf`
- Test: `tests/infra/test_observability.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_observability.py`:
```python
"""H27 destinations plus the detective controls from the foundation spec §7.5."""

from tests.infra import tfparse


def test_one_log_group_per_component_at_fourteen_day_retention():
    group = tfparse.resource("aws_cloudwatch_log_group", "app")
    assert group["for_each"] == "${toset(local.components)}"
    assert group["name"] == "/${var.project}/${each.value}"
    # Default retention is forever, and log storage is a silent recurring cost.
    assert group["retention_in_days"] == 14


def test_there_is_a_single_alerts_topic_with_an_email_subscription():
    topic = tfparse.resource("aws_sns_topic", "alerts")
    assert topic["name"] == "${var.project}-alerts"
    sub = tfparse.resource("aws_sns_topic_subscription", "alerts_email")
    assert sub["protocol"] == "email"
    assert sub["endpoint"] == "${var.alert_email}"


def test_topic_policy_lets_budgets_and_cloudwatch_publish():
    doc = tfparse.data_source("aws_iam_policy_document", "alerts_topic")
    services = set()
    for statement in tfparse.blocks(doc, "statement"):
        for principal in tfparse.blocks(statement, "principals"):
            services.update(principal["identifiers"])
    assert "budgets.amazonaws.com" in services
    assert "cloudwatch.amazonaws.com" in services
    assert "events.amazonaws.com" in services


def test_cloudtrail_has_log_file_validation_enabled():
    trail = tfparse.resource("aws_cloudtrail", "main")
    assert trail["enable_log_file_validation"] is True
    assert trail["is_multi_region_trail"] is True
    assert trail["include_global_service_events"] is True


def test_trail_bucket_blocks_all_public_access_and_is_versioned():
    block = tfparse.resource("aws_s3_bucket_public_access_block", "trail")
    assert all(
        block[key] is True
        for key in (
            "block_public_acls",
            "block_public_policy",
            "ignore_public_acls",
            "restrict_public_buckets",
        )
    )
    versioning = tfparse.blocks(
        tfparse.resource("aws_s3_bucket_versioning", "trail"), "versioning_configuration"
    )[0]
    assert versioning["status"] == "Enabled"


def test_guardduty_is_enabled():
    assert tfparse.resource("aws_guardduty_detector", "main")["enable"] is True


def test_root_usage_rule_targets_the_alerts_topic():
    rule = tfparse.resource("aws_cloudwatch_event_rule", "root_usage")
    assert "Root" in rule["event_pattern"]
    target = tfparse.resource("aws_cloudwatch_event_target", "root_usage")
    assert target["arn"] == "${aws_sns_topic.alerts.arn}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_observability.py -v`
Expected: 7 failures, the first `AssertionError: missing resource aws_cloudwatch_log_group.app; found []`

- [ ] **Step 3: Write minimal implementation**

`infra/terraform/observability.tf`:
```hcl
# ---------------------------------------------------------------------------
# Log destinations (premortem H27). The awslogs driver on each container writes
# here; Task 12 grants the write, Task 14 points the driver, Task 15 alarms.
# 14-day retention because the CloudWatch default is forever and log storage is
# a silent recurring cost on a $100 ceiling.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "app" {
  for_each = toset(local.components)

  name              = "/${var.project}/${each.value}"
  retention_in_days = 14

  tags = { Component = each.value }
}

# ---------------------------------------------------------------------------
# One SNS topic carries budget alerts, the health alarm, and root-usage events.
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid       = "AllowBudgetsToPublish"
    effect    = "Allow"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
  }

  statement {
    sid       = "AllowCloudWatchAlarmsToPublish"
    effect    = "Allow"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
  }

  statement {
    sid       = "AllowEventBridgeToPublish"
    effect    = "Allow"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}

# ---------------------------------------------------------------------------
# CloudTrail. force_destroy is deliberate: teardown is cost control #2 and a
# non-empty bucket blocks it. Tamper resistance for the trail itself lives in
# the Sandbox OU service control policy from Phase A1, which denies StopLogging,
# DeleteTrail, UpdateTrail and PutEventSelectors from inside this account.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "trail" {
  bucket        = "${var.project}-cloudtrail-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "trail" {
  bucket                  = aws_s3_bucket.trail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "trail" {
  bucket = aws_s3_bucket.trail.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id

  rule {
    id     = "expire-trail-objects"
    status = "Enabled"

    filter {}

    expiration {
      days = 90
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

data "aws_iam_policy_document" "trail_bucket" {
  statement {
    sid       = "AWSCloudTrailAclCheck"
    effect    = "Allow"
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.trail.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
  }

  statement {
    sid       = "AWSCloudTrailWrite"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.trail.arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }
}

resource "aws_s3_bucket_policy" "trail" {
  bucket = aws_s3_bucket.trail.id
  policy = data.aws_iam_policy_document.trail_bucket.json
}

resource "aws_cloudtrail" "main" {
  name                          = "${var.project}-trail"
  s3_bucket_name                = aws_s3_bucket.trail.id
  include_global_service_events = true
  is_multi_region_trail         = true
  enable_log_file_validation    = true

  depends_on = [aws_s3_bucket_policy.trail]
}

resource "aws_guardduty_detector" "main" {
  enable                       = true
  finding_publishing_frequency = "FIFTEEN_MINUTES"
}

# ---------------------------------------------------------------------------
# Root usage is either you deliberately opening the break-glass or an incident.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "root_usage" {
  name        = "${var.project}-root-usage"
  description = "Any activity by the account root user"

  event_pattern = jsonencode({
    "detail-type" = [
      "AWS API Call via CloudTrail",
      "AWS Console Sign In via CloudTrail",
    ]
    detail = {
      userIdentity = {
        type = ["Root"]
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "root_usage" {
  rule      = aws_cloudwatch_event_rule.root_usage.name
  target_id = "alerts"
  arn       = aws_sns_topic.alerts.arn
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make tf-fmt && terraform -chdir=infra/terraform validate && .venv/bin/pytest tests/infra/test_observability.py -v`
Expected: `Success! The configuration is valid.` and 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/observability.tf tests/infra/test_observability.py
git commit -m "Add per-component log groups, alerts topic, CloudTrail with validation, and GuardDuty"
```

---

### Task 9 (H6): `data.tf` — RDS Postgres 16, private, with a real final snapshot

**Finding H6:** `terraform destroy` — cost control #2 — fails on `aws_db_instance` without `skip_final_snapshot` or a `final_snapshot_identifier`, leaving a half-destroyed billing stack. Setting `skip_final_snapshot = true` instead **permanently deletes the graded dashboard dataset** on every teardown. Both branches are bad; the fix is `skip_final_snapshot = false` with a *unique* identifier and `backup_retention_period >= 1`.

The identifier has to be unique per database lifecycle or the second destroy fails with `DBSnapshotAlreadyExists`. `timestamp()` cannot be used, because it re-evaluates on every plan and produces a permanent diff. `time_static` captures the moment the database was created and then holds still in state, which gives a unique identifier per lifecycle with no drift.

This also disarms H29 by accident and on purpose: a stopped RDS instance auto-restarts after seven days, and the documented remedy of "destroy rather than stop" is only survivable because the final snapshot preserves the graded dataset.

**Files:**
- Create: `infra/terraform/data.tf`
- Test: `tests/infra/test_data.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_data.py`:
```python
"""H6: teardown must be both possible and non-destructive to the graded dataset."""

from tests.infra import tfparse


def _db() -> dict:
    return tfparse.resource("aws_db_instance", "main")


def test_final_snapshot_is_taken_not_skipped():
    assert _db()["skip_final_snapshot"] is False, (
        "skip_final_snapshot=true deletes the graded dashboard dataset on every teardown"
    )


def test_final_snapshot_identifier_is_unique_per_database_lifecycle():
    identifier = _db()["final_snapshot_identifier"]
    assert "time_static.db.rfc3339" in identifier, (
        "a constant identifier fails the second destroy with DBSnapshotAlreadyExists"
    )
    assert "timestamp()" not in identifier, "timestamp() re-evaluates and drifts every plan"
    assert "time_static" in tfparse.resources("time_static") or "db" in tfparse.resources(
        "time_static"
    )


def test_backups_are_retained_for_at_least_one_day():
    assert _db()["backup_retention_period"] >= 1


def test_deletion_protection_is_off_so_destroy_can_run():
    assert _db()["deletion_protection"] is False


def test_database_is_private_encrypted_and_single_az():
    db = _db()
    assert db["publicly_accessible"] is False
    assert db["storage_encrypted"] is True
    assert db["multi_az"] is False
    assert db["vpc_security_group_ids"] == ["${aws_security_group.db.id}"]


def test_master_password_is_managed_by_rds_not_by_terraform():
    db = _db()
    assert db["manage_master_user_password"] is True
    assert "password" not in db, "a password attribute writes plaintext into state"
    assert tfparse.resources("random_password") == {}


def test_engine_and_class_match_the_pinned_sizing():
    db = _db()
    assert db["engine"] == "postgres"
    assert str(db["engine_version"]).startswith("16")
    assert db["instance_class"] == "db.t4g.micro"


def test_subnet_group_uses_the_two_private_subnets_only():
    group = tfparse.resource("aws_db_subnet_group", "main")
    assert group["subnet_ids"] == [
        "${aws_subnet.private_a.id}",
        "${aws_subnet.private_b.id}",
    ]


def test_three_secrets_exist_and_terraform_writes_none_of_their_values():
    secrets = tfparse.resources("aws_secretsmanager_secret")
    assert set(secrets) == {"wandb_api_key", "reviewer_shared_secret", "db_readonly"}
    # Spec §7.4: values are seeded once by CLI so no secret passes through state.
    assert tfparse.resources("aws_secretsmanager_secret_version") == {}
    for name, body in secrets.items():
        assert body["recovery_window_in_days"] == 7, f"{name} bills for 30 days by default"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_data.py -v`
Expected: 9 failures, all `AssertionError: missing resource aws_db_instance.main; found []`

- [ ] **Step 3: Write minimal implementation**

`infra/terraform/data.tf`:
```hcl
resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-db"
  subnet_ids = [aws_subnet.private_a.id, aws_subnet.private_b.id]

  tags = { Name = "${var.project}-db" }
}

# Captured once at creation and then held still in state. `timestamp()` would
# re-evaluate on every plan and produce a permanent diff; a constant identifier
# would fail the second `terraform destroy` with DBSnapshotAlreadyExists.
resource "time_static" "db" {}

resource "aws_db_instance" "main" {
  identifier     = "${var.project}-pg"
  engine         = "postgres"
  engine_version = "16"
  instance_class = "db.t4g.micro"

  allocated_storage     = 20
  max_allocated_storage = 0
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "toxicmod"
  username = "toxicadmin"

  # Keeps the master password out of Terraform state entirely, and satisfies the
  # Sandbox OU service control policy, which denies CreateDBInstance without it.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = false

  # premortem H6. Backups are retained so a teardown is recoverable, and the final
  # snapshot means `terraform destroy` neither fails nor destroys the dataset the
  # monitoring dashboard is graded on.
  backup_retention_period   = 7
  backup_window             = "09:00-09:30"
  maintenance_window        = "sun:10:00-sun:10:30"
  copy_tags_to_snapshot     = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.project}-final-${formatdate("YYYYMMDDhhmmss", time_static.db.rfc3339)}"

  # Teardown is cost control #2, so nothing may block it.
  deletion_protection = false

  auto_minor_version_upgrade      = true
  apply_immediately               = true
  performance_insights_enabled    = false
  enabled_cloudwatch_logs_exports = ["postgresql"]

  tags = { Name = "${var.project}-pg", Component = "db" }
}

# ---------------------------------------------------------------------------
# Secret containers only. Values are seeded once by CLI (foundation spec §7.4)
# so that no secret value ever passes through Terraform state or the repository.
# A 7-day recovery window rather than the 30-day default, because a deleted
# secret keeps billing until its window closes.
# ---------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "wandb_api_key" {
  name                    = "${var.project}/wandb-api-key"
  description             = "Weights & Biases API key. Seeded by CLI, read by the backend only."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "reviewer_shared_secret" {
  name                    = "${var.project}/reviewer-shared-secret"
  description             = "Reviewer UI shared secret. Seeded by CLI, read by the frontend only."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "db_readonly" {
  name                    = "${var.project}/db-readonly"
  description             = "monitor_ro Postgres credentials. Seeded by CLI, read by monitoring only."
  recovery_window_in_days = 7
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make tf-fmt && terraform -chdir=infra/terraform validate && .venv/bin/pytest tests/infra/test_data.py -v`
Expected: `Success! The configuration is valid.` and 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/data.tf tests/infra/test_data.py
git commit -m "Add private RDS Postgres 16 with retained backups and a unique final snapshot"
```

---

### Task 10 (H16): The read-only database role for the monitoring dashboard

**Finding H16, third clause:** there is no read-only role for the dashboard, which only ever issues `SELECT`. Today every tier would connect as the RDS master user, so a Streamlit remote-code-execution on the internet-facing monitoring box yields write access to `feedback` — the table the graded live-accuracy number is computed from.

Postgres roles cannot be created by the AWS provider, and the database is private with no bastion, so the role is created by SQL executed *from inside the VPC* through an SSM document. That keeps the no-SSH posture intact and keeps the password out of Terraform state: the value is seeded into the Secrets Manager container from Task 9 by CLI, and the document reads it at run time.

**Files:**
- Create: `infra/terraform/sql/monitoring_readonly.sql`, `infra/terraform/sql/bootstrap_readonly.sh.tftpl`
- Modify: `infra/terraform/data.tf`
- Test: `tests/infra/test_readonly_role.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_readonly_role.py`:
```python
"""H16: the dashboard connects as a role that physically cannot write."""

import re

from tests.infra import tfparse

SQL = tfparse.MAIN / "sql" / "monitoring_readonly.sql"
WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "ALL")


def test_bootstrap_sql_exists():
    assert SQL.exists()


def test_role_is_created_without_any_elevated_attribute():
    text = SQL.read_text().upper()
    for attribute in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOREPLICATION"):
        assert attribute in text, f"{attribute} missing from the monitor_ro role"


def test_only_select_is_granted():
    grants = re.findall(r"^\s*GRANT\s+([A-Z, ]+?)\s+ON", SQL.read_text().upper(), re.M)
    granted = {verb.strip() for line in grants for verb in line.split(",")}
    assert "SELECT" in granted
    assert granted <= {"SELECT", "CONNECT", "USAGE"}, f"over-granted: {sorted(granted)}"
    for verb in WRITE_VERBS:
        assert verb not in granted


def test_default_privileges_cover_tables_created_after_the_role():
    # Phase 2 creates the three tables. Without ALTER DEFAULT PRIVILEGES the role
    # would silently see nothing and the dashboard would render empty charts.
    text = SQL.read_text().upper()
    assert "ALTER DEFAULT PRIVILEGES" in text
    assert "GRANT SELECT ON TABLES" in text


def test_sql_is_idempotent():
    assert "pg_roles" in SQL.read_text(), "re-running must not fail on an existing role"


def test_an_ssm_command_document_carries_the_bootstrap():
    doc = tfparse.resource("aws_ssm_document", "db_bootstrap_readonly")
    assert doc["document_type"] == "Command"
    assert "monitoring_readonly.sql" in doc["content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_readonly_role.py -v`
Expected: 6 failures, the first `AssertionError: assert False` from `SQL.exists()`.

- [ ] **Step 3: Write minimal implementation**

`infra/terraform/sql/monitoring_readonly.sql`:
```sql
-- Read-only Postgres role for the monitoring dashboard (premortem H16).
--
-- The dashboard only ever issues SELECT. It must not be able to write `feedback`,
-- which is where the graded live-accuracy number comes from, and it must never hold
-- the RDS master credentials. Idempotent, so it is safe to re-run after every apply.
--
-- Invoked by the SSM document toxic-mod-db-bootstrap-readonly with psql variables
-- ro_user and ro_pass supplied from Secrets Manager at run time.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro_user') THEN
    EXECUTE format(
      'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION',
      :'ro_user', :'ro_pass');
  ELSE
    EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION',
      :'ro_user', :'ro_pass');
  END IF;
END
$$;

GRANT CONNECT ON DATABASE :"db_name" TO :"ro_user";
GRANT USAGE ON SCHEMA public TO :"ro_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"ro_user";

-- Phase 2 creates predictions, review_queue and feedback AFTER this runs. Without
-- default privileges the role would see an empty schema and the dashboard would
-- render empty charts with no error.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO :"ro_user";
```

`infra/terraform/sql/bootstrap_readonly.sh.tftpl`:
```bash
set -euo pipefail
dnf -qy install postgresql16 jq

MASTER_JSON=$(aws secretsmanager get-secret-value --secret-id '{{MasterSecretArn}}' --query SecretString --output text)
RO_JSON=$(aws secretsmanager get-secret-value --secret-id '{{ReadonlySecretArn}}' --query SecretString --output text)

MASTER_USER=$(printf '%s' "$MASTER_JSON" | jq -r .username)
RO_USER=$(printf '%s' "$RO_JSON" | jq -r .username)
RO_PASS=$(printf '%s' "$RO_JSON" | jq -r .password)
PGPASSWORD=$(printf '%s' "$MASTER_JSON" | jq -r .password)
export PGPASSWORD

umask 077
cat > /tmp/monitoring_readonly.sql <<'TOXICMODSQL'
${sql}
TOXICMODSQL

psql --host '{{DbHost}}' --username "$MASTER_USER" --dbname '{{DbName}}' \
  --set ON_ERROR_STOP=1 \
  --set ro_user="$RO_USER" \
  --set ro_pass="$RO_PASS" \
  --set db_name='{{DbName}}' \
  --file /tmp/monitoring_readonly.sql

rm -f /tmp/monitoring_readonly.sql
unset PGPASSWORD
echo "monitor_ro bootstrap complete"
```

Append to `infra/terraform/data.tf`:
```hcl
# ---------------------------------------------------------------------------
# The read-only role (premortem H16). The database is private with no bastion,
# so the SQL runs from inside the VPC through SSM Run Command against the
# backend instance. Neither password is ever in Terraform state: the master one
# is RDS-managed and the monitor_ro one is seeded into Secrets Manager by CLI.
# ---------------------------------------------------------------------------

resource "aws_ssm_document" "db_bootstrap_readonly" {
  name            = "${var.project}-db-bootstrap-readonly"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Create or update the read-only monitor_ro Postgres role from inside the VPC (monitoring_readonly.sql)."
    parameters = {
      MasterSecretArn = {
        type        = "String"
        description = "ARN of the RDS-managed master credentials secret"
      }
      ReadonlySecretArn = {
        type        = "String"
        description = "ARN of the CLI-seeded monitor_ro credentials secret"
      }
      DbHost = {
        type        = "String"
        description = "RDS endpoint address"
      }
      DbName = {
        type        = "String"
        description = "Database name"
      }
    }
    mainSteps = [
      {
        action = "aws:runShellScript"
        name   = "createReadOnlyRole"
        inputs = {
          timeoutSeconds = "300"
          runCommand = split("\n", templatefile("${path.module}/sql/bootstrap_readonly.sh.tftpl", {
            sql = file("${path.module}/sql/monitoring_readonly.sql")
          }))
        }
      },
    ]
  })
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make tf-fmt && terraform -chdir=infra/terraform validate && .venv/bin/pytest tests/infra/test_readonly_role.py -v`
Expected: `Success! The configuration is valid.` and 6 PASS.

- [ ] **Step 5: Record how the role is actually created**

Append to `docs/runbooks/no-ssh-debug.md`:
```markdown
## 8. Creating or rotating the read-only dashboard role

The database is private and there is no bastion, so this runs through SSM against
the backend instance. Seed the secret first; Terraform only creates the container.

```bash
RO_PASS=$(openssl rand -base64 30 | tr -d '/+=' | head -c 32)
aws secretsmanager put-secret-value \
  --secret-id toxic-mod/db-readonly \
  --secret-string "$(jq -nc --arg u monitor_ro --arg p "$RO_PASS" '{username:$u,password:$p}')"

aws ssm send-command \
  --instance-ids "$(terraform -chdir=infra/terraform output -json instance_ids | jq -r .backend)" \
  --document-name "$(terraform -chdir=infra/terraform output -raw db_bootstrap_document)" \
  --parameters "MasterSecretArn=$(terraform -chdir=infra/terraform output -raw db_master_secret_arn),ReadonlySecretArn=$(terraform -chdir=infra/terraform output -raw db_readonly_secret_arn),DbHost=$(terraform -chdir=infra/terraform output -raw db_host),DbName=toxicmod"
```

Poll `aws ssm get-command-invocation` to a terminal state before believing it worked.
`send-command` returns a CommandId and exits 0 even when it matched nothing.
```

- [ ] **Step 6: Commit**

```bash
git add infra/terraform/sql infra/terraform/data.tf docs/runbooks/no-ssh-debug.md tests/infra/test_readonly_role.py
git commit -m "Add SELECT-only Postgres role for the monitoring dashboard, bootstrapped over SSM"
```

---

### Task 11 (H16, H27): `iam.tf` — one role and one instance profile per tier

**Finding H16, first and second clauses:** one security group, one instance role, one DB user across three instances. A Streamlit remote-code-execution on the internet-facing box yields the W&B key, the reviewer secret, and master-user read/write on all three tables.

Three roles, each scoped to exactly the repositories, log groups and secrets its own tier needs. The monitoring role is the sharp end: it gets the `monitor_ro` secret and *not* the RDS master secret, which is what makes Task 10's read-only role a control rather than a convention.

**Files:**
- Create: `infra/terraform/iam.tf`
- Test: `tests/infra/test_iam.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_iam.py`:
```python
"""H16: per-tier instance roles. A compromise of one tier stays in that tier."""

import pytest

from tests.infra import tfparse

TIERS = ["backend", "frontend", "monitoring"]


def _statements(doc_name: str) -> list[dict]:
    return tfparse.blocks(tfparse.data_source("aws_iam_policy_document", doc_name), "statement")


def _resources_of(doc_name: str) -> list[str]:
    out: list[str] = []
    for statement in _statements(doc_name):
        out.extend(statement.get("resources", []))
    return out


@pytest.mark.parametrize("tier", TIERS)
def test_each_tier_has_its_own_role_and_instance_profile(tier):
    assert tfparse.resource("aws_iam_role", tier)["name"] == f"${{var.project}}-{tier}"
    assert tfparse.resource("aws_iam_instance_profile", tier)["role"] == (
        f"${{aws_iam_role.{tier}.name}}"
    )


@pytest.mark.parametrize("tier", TIERS)
def test_each_tier_gets_ssm_core_so_there_is_a_way_in_without_ssh(tier):
    attachment = tfparse.resource("aws_iam_role_policy_attachment", f"{tier}_ssm")
    assert attachment["policy_arn"] == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"


@pytest.mark.parametrize("tier", TIERS)
def test_wildcard_resources_appear_only_on_ecr_get_authorization_token(tier):
    for statement in _statements(tier):
        if statement.get("resources") == ["*"]:
            assert statement["actions"] == ["ecr:GetAuthorizationToken"], statement.get("sid")


@pytest.mark.parametrize("tier", TIERS)
def test_each_tier_writes_only_to_its_own_log_group(tier):
    log_refs = [r for r in _resources_of(tier) if "aws_cloudwatch_log_group" in r]
    assert log_refs, f"{tier} cannot write logs at all; the awslogs driver would fail"
    for ref in log_refs:
        assert f'app["{tier}"]' in ref or f'app["rescorer"]' in ref, ref


def test_monitoring_cannot_read_the_rds_master_secret():
    # The whole point of the read-only role: the dashboard tier must not be able to
    # fall back to master credentials and write the graded feedback table.
    refs = " ".join(_resources_of("monitoring"))
    assert "master_user_secret" not in refs
    assert "aws_secretsmanager_secret.db_readonly.arn" in refs


def test_monitoring_cannot_read_the_wandb_key_or_the_reviewer_secret():
    refs = " ".join(_resources_of("monitoring"))
    assert "wandb_api_key" not in refs
    assert "reviewer_shared_secret" not in refs


def test_backend_cannot_read_the_reviewer_secret():
    assert "reviewer_shared_secret" not in " ".join(_resources_of("backend"))


def test_frontend_cannot_read_the_wandb_key():
    assert "wandb_api_key" not in " ".join(_resources_of("frontend"))


def test_no_ui_tier_can_read_the_rds_master_secret():
    """H16, stated as the premortem states it: 'a Streamlit RCE on the internet-facing box
    yields ... master-user read/write on all three tables'. The monitoring tier was tested
    for this and the frontend tier was not, so the finding shipped unfixed with every test
    green. Both UI tiers are checked here, by name, so adding a third cannot skip it."""
    for tier in ("frontend", "monitoring"):
        refs = " ".join(_resources_of(tier))
        assert "master_user_secret" not in refs, (
            f"{tier} holds the RDS master credential; premortem H16 and Phase 3 principle 1"
        )


def test_only_the_backend_tier_holds_a_database_write_credential():
    """The complement of the above: exactly one tier may write to Postgres."""
    holders = [
        tier for tier in TIERS if "master_user_secret" in " ".join(_resources_of(tier))
    ]
    assert holders == ["backend"], f"database write credential held by {holders}"


@pytest.mark.parametrize("tier", TIERS)
def test_no_tier_can_pass_a_role_or_touch_iam(tier):
    for statement in _statements(tier):
        for action in statement.get("actions", []):
            assert not action.startswith("iam:"), f"{tier} holds {action}"
            assert not action.startswith("sts:"), f"{tier} holds {action}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_iam.py -v`
Expected: FAIL with `AssertionError: missing data aws_iam_policy_document.backend; found ['alerts_topic', 'trail_bucket']` and `AssertionError: missing resource aws_iam_role.backend; found []`

- [ ] **Step 3: Write minimal implementation**

`infra/terraform/iam.tf`:
```hcl
# ---------------------------------------------------------------------------
# One role and one instance profile per tier (premortem H16). A single shared
# ec2-app-role would mean a Streamlit RCE on the internet-facing box yields the
# W&B key, the reviewer secret, and master read/write on every table.
#
# Each policy names exactly the repository, log group and secret its own tier
# needs. The monitoring tier deliberately cannot read the RDS master secret;
# that omission is what makes the SELECT-only Postgres role a real control.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# ---- backend -------------------------------------------------------------

resource "aws_iam_role" "backend" {
  name               = "${var.project}-backend"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "backend_ssm" {
  role       = aws_iam_role.backend.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "backend" {
  statement {
    sid       = "EcrAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPullBackendOnly"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.app["backend"].arn]
  }

  statement {
    sid       = "LogsBackendGroupOnly"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app["backend"].arn}:*"]
  }

  statement {
    sid       = "SecretsBackendOnly"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.wandb_api_key.arn,
      aws_db_instance.main.master_user_secret[0].secret_arn,
    ]
  }
}

resource "aws_iam_role_policy" "backend" {
  name   = "${var.project}-backend"
  role   = aws_iam_role.backend.id
  policy = data.aws_iam_policy_document.backend.json
}

resource "aws_iam_instance_profile" "backend" {
  name = "${var.project}-backend"
  role = aws_iam_role.backend.name
}

# ---- frontend ------------------------------------------------------------

resource "aws_iam_role" "frontend" {
  name               = "${var.project}-frontend"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "frontend_ssm" {
  role       = aws_iam_role.frontend.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "frontend" {
  statement {
    sid       = "EcrAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPullFrontendOnly"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.app["frontend"].arn]
  }

  statement {
    sid       = "LogsFrontendGroupOnly"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app["frontend"].arn}:*"]
  }

  statement {
    sid       = "SecretsFrontendOnly"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [
      # The reviewer shared secret and NOTHING ELSE. The RDS master credential was here
      # in an earlier draft; combined with this tier's 5432 ingress it was premortem H16's
      # harm sentence verbatim -- "a Streamlit RCE on the internet-facing box yields ...
      # master-user read/write on all three tables" -- inside the file that claims to close
      # it. It also contradicted Phase 3 binding principle 1, "No UI container holds a
      # database write credential." The frontend reaches the database through the backend
      # API; that is what the backend tier is for. See Task 5a.
      aws_secretsmanager_secret.reviewer_shared_secret.arn,
    ]
  }
}

resource "aws_iam_role_policy" "frontend" {
  name   = "${var.project}-frontend"
  role   = aws_iam_role.frontend.id
  policy = data.aws_iam_policy_document.frontend.json
}

resource "aws_iam_instance_profile" "frontend" {
  name = "${var.project}-frontend"
  role = aws_iam_role.frontend.name
}

# ---- monitoring ----------------------------------------------------------

resource "aws_iam_role" "monitoring" {
  name               = "${var.project}-monitoring"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "monitoring_ssm" {
  role       = aws_iam_role.monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "monitoring" {
  statement {
    sid       = "EcrAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPullMonitoringAndRescorer"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [
      aws_ecr_repository.app["monitoring"].arn,
      aws_ecr_repository.app["rescorer"].arn,
    ]
  }

  statement {
    sid     = "LogsMonitoringAndRescorerGroupsOnly"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "${aws_cloudwatch_log_group.app["monitoring"].arn}:*",
      "${aws_cloudwatch_log_group.app["rescorer"].arn}:*",
    ]
  }

  # Read-only database credentials ONLY. The RDS master secret is deliberately
  # absent: the dashboard must not be able to write the graded feedback table.
  statement {
    sid       = "SecretsReadOnlyDatabaseRoleOnly"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db_readonly.arn]
  }
}

resource "aws_iam_role_policy" "monitoring" {
  name   = "${var.project}-monitoring"
  role   = aws_iam_role.monitoring.id
  policy = data.aws_iam_policy_document.monitoring.json
}

resource "aws_iam_instance_profile" "monitoring" {
  name = "${var.project}-monitoring"
  role = aws_iam_role.monitoring.name
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make tf-fmt && terraform -chdir=infra/terraform validate && .venv/bin/pytest tests/infra/test_iam.py -v`
Expected: `Success! The configuration is valid.` and 19 PASS (parametrised).

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/iam.tf tests/infra/test_iam.py
git commit -m "Add per-tier instance roles scoped to their own repository, log group and secret"
```

---

### Task 12 (H4, H36): `oidc.tf` — one deploy role, with a trust policy that ANDs

**Finding H4:** the `gha-deploy` trust policy as described in prose is the canonical OR-bug. IAM evaluates a multi-valued condition as a logical OR, so writing

```json
"StringEquals": {"…:sub": ["repo:owner/repo:ref:refs/heads/main",
                           "repo:owner/repo:environment:production"]}
```

means *either* clause satisfies the policy. Any workflow declaring `environment: production` on **any** branch — including a branch pushed by anyone who can open a branch — assumes the role, bypassing the required-review gate entirely. The fix is a **single-valued** `StringEquals` on `sub` plus a second, independent `StringEquals` on `job_workflow_ref`, because separate condition blocks AND.

**Finding H4, second clause, and H36:** `gha-deploy` also needed `iam:*` to apply `iam.tf`, which is de-facto administrator. This plan removes the need at the root: **`terraform apply` does not run in GitHub Actions at all.** Apply is an operator action from the Jetson under the Identity Center session, which is what the delivery spec's day 10-11 schedule already assumes. `deploy.yml` therefore builds images, pushes to ECR, and rolls containers through SSM — nothing more — and the role carries no IAM, no EC2 launch, no RDS, and no Terraform state access, with an explicit Deny as a second layer.

**Finding H36 also removes `gha-ci` entirely.** With `terraform plan` gone from pull-request CI, the pull-request workflow needs no AWS identity at all, so no role exists that a pull request could assume. That is strictly stronger than scoping one.

**Files:**
- Create: `infra/terraform/oidc.tf`
- Test: `tests/infra/test_oidc.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_oidc.py`:
```python
"""H4: the trust policy must AND, not OR. H36: no role is assumable from a PR."""

from tests.infra import tfparse

TRUST = "gha_deploy_trust"


def _trust_conditions() -> list[dict]:
    doc = tfparse.data_source("aws_iam_policy_document", TRUST)
    statement = tfparse.blocks(doc, "statement")[0]
    return tfparse.blocks(statement, "condition")


def test_exactly_one_oidc_provider_and_no_hardcoded_thumbprint():
    providers = tfparse.resources("aws_iam_openid_connect_provider")
    assert list(providers) == ["github"]
    provider = providers["github"]
    assert provider["url"] == "https://token.actions.githubusercontent.com"
    assert provider["client_id_list"] == ["sts.amazonaws.com"]
    # Optional + Computed: AWS fetches it. A hardcoded pair goes stale on CA rotation.
    assert "thumbprint_list" not in provider


def test_every_trust_condition_is_single_valued():
    # This is the whole of H4. A two-element `values` list is evaluated by IAM as
    # OR, so `environment:production` on ANY branch would satisfy the policy.
    for condition in _trust_conditions():
        assert len(condition["values"]) == 1, (
            f"{condition['variable']} has {len(condition['values'])} values; "
            "IAM evaluates a multi-valued condition as OR"
        )
        assert condition["test"] == "StringEquals", (
            f"{condition['variable']} uses {condition['test']}; StringLike would wildcard"
        )


def test_trust_pins_aud_sub_and_job_workflow_ref():
    variables = {c["variable"] for c in _trust_conditions()}
    assert variables == {
        "token.actions.githubusercontent.com:aud",
        "token.actions.githubusercontent.com:sub",
        "token.actions.githubusercontent.com:job_workflow_ref",
    }


def test_sub_pins_the_production_environment_and_ref_pins_main():
    by_var = {c["variable"]: c["values"][0] for c in _trust_conditions()}
    sub = by_var["token.actions.githubusercontent.com:sub"]
    assert sub == "repo:${var.github_repo}:environment:production"
    assert "*" not in sub
    workflow = by_var["token.actions.githubusercontent.com:job_workflow_ref"]
    assert workflow == "${var.github_repo}/.github/workflows/deploy.yml@refs/heads/main"


def test_there_is_no_gha_ci_role():
    # H36: `terraform plan` is gone from pull-request CI, so no AWS identity is
    # reachable from a pull request at all.
    assert "gha_ci" not in tfparse.resources("aws_iam_role")
    assert set(tfparse.resources("aws_iam_role")) == {
        "backend",
        "frontend",
        "monitoring",
        "gha_deploy",
    }


def test_deploy_role_cannot_run_terraform_apply():
    allowed: set[str] = set()
    for statement in tfparse.blocks(
        tfparse.data_source("aws_iam_policy_document", "gha_deploy"), "statement"
    ):
        if statement.get("effect") == "Allow":
            allowed.update(statement.get("actions", []))
    for forbidden_prefix in ("iam:", "organizations:", "rds:", "s3:", "ec2:Run"):
        assert not any(a.startswith(forbidden_prefix) for a in allowed), (
            f"gha-deploy holds {forbidden_prefix}*; apply belongs to the operator, not CI"
        )


def test_deploy_role_carries_an_explicit_escalation_deny():
    denies = [
        s
        for s in tfparse.blocks(
            tfparse.data_source("aws_iam_policy_document", "gha_deploy"), "statement"
        )
        if s.get("effect") == "Deny"
    ]
    assert denies, "no explicit Deny; the Allow set is the only thing standing between CI and admin"
    denied = {a for s in denies for a in s.get("actions", [])}
    assert {"iam:*", "organizations:*", "sts:AssumeRole"} <= denied


def test_send_command_is_scoped_to_tagged_instances_and_one_document():
    statements = tfparse.blocks(
        tfparse.data_source("aws_iam_policy_document", "gha_deploy"), "statement"
    )
    send = [s for s in statements if "ssm:SendCommand" in s.get("actions", [])]
    assert send, "the deploy role cannot roll containers"
    targets = {r for s in send for r in s["resources"]}
    assert any("instance/*" in t for t in targets)
    assert any("document/AWS-RunShellScript" in t for t in targets)
    tag_conditions = [c for s in send for c in tfparse.blocks(s, "condition")]
    assert any("ssm:resourceTag/Project" in c["variable"] for c in tag_conditions)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_oidc.py -v`
Expected: 8 failures, the first `AssertionError: assert [] == ['github']` and the rest `AssertionError: missing data aws_iam_policy_document.gha_deploy_trust`

- [ ] **Step 3: Write minimal implementation**

`infra/terraform/oidc.tf`:
```hcl
# ---------------------------------------------------------------------------
# GitHub Actions OIDC. ONE role, and `terraform apply` is not among its powers.
#
# premortem H4: a `sub` written as a two-element array is evaluated by IAM as a
# logical OR, so `environment: production` declared on any branch would satisfy
# it and bypass the required-review gate. Each condition below is single-valued,
# and separate condition blocks AND, so the token must simultaneously come from
# the production environment AND from deploy.yml at refs/heads/main.
#
# premortem H4/H36: apply is an operator action from the Identity Center
# session, not a CI action, which is what removes the need for `iam:*` here.
# There is deliberately no `gha-ci` role, because pull-request CI runs
# `terraform validate` offline and needs no AWS identity whatsoever.
# ---------------------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  # thumbprint_list is Optional + Computed: AWS fetches the current thumbprint
  # itself. Hardcoding a pair is the pattern that breaks on CA rotation, and the
  # attribute cannot be cleared once set.
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "gha_deploy_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:environment:production"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:job_workflow_ref"
      values   = ["${var.github_repo}/.github/workflows/deploy.yml@refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "gha_deploy" {
  name                 = "${var.project}-gha-deploy"
  assume_role_policy   = data.aws_iam_policy_document.gha_deploy_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "gha_deploy" {
  statement {
    sid       = "EcrAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPushToTheFourProjectRepositories"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [for repo in aws_ecr_repository.app : repo.arn]
  }

  statement {
    sid       = "SsmSendCommandToProjectInstancesOnly"
    effect    = "Allow"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/Project"
      values   = [var.project]
    }
  }

  statement {
    sid       = "SsmSendCommandRunShellScriptDocumentOnly"
    effect    = "Allow"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ssm:${var.region}::document/AWS-RunShellScript"]
  }

  statement {
    sid    = "SsmObserveTheRollToATerminalState"
    effect = "Allow"
    actions = [
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
      "ssm:ListCommands",
      "ssm:DescribeInstanceInformation",
    ]
    resources = ["*"]
  }

  # Second layer. The Allow set above is already narrow; this makes privilege
  # escalation impossible to reintroduce by editing one statement.
  statement {
    sid    = "DenyPrivilegeEscalationAndInfrastructureChange"
    effect = "Deny"
    actions = [
      "iam:*",
      "organizations:*",
      "sso:*",
      "sso-admin:*",
      "sts:AssumeRole",
      "ec2:RunInstances",
      "ec2:TerminateInstances",
      "rds:*",
      "s3:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "gha_deploy" {
  name   = "${var.project}-gha-deploy"
  role   = aws_iam_role.gha_deploy.id
  policy = data.aws_iam_policy_document.gha_deploy.json
}
```

Note the `SsmObserveTheRollToATerminalState` statement uses `Resource: "*"`: `ssm:GetCommandInvocation` and `ssm:DescribeInstanceInformation` are read-only and are not resource-scopable in a useful way. It is deliberate, and `test_wildcard_resources_appear_only_on_ecr_get_authorization_token` does not apply to this document because that test is parametrised over the three instance tiers.

- [ ] **Step 4: Run test to verify it passes**

Run: `make tf-fmt && terraform -chdir=infra/terraform validate && .venv/bin/pytest tests/infra/test_oidc.py -v`
Expected: `Success! The configuration is valid.` and 8 PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/oidc.tf tests/infra/test_oidc.py
git commit -m "Add GitHub OIDC deploy role with single-valued trust conditions and no apply rights"
```

---

### Task 13 (H2, C7, H27, IMDSv2, EIP): `compute.tf` — three instances

**Finding H2:** the AWS foundation spec §7.2 — the Terraform scope of record — described **two** EC2 instances while the three-instance decision lived in one paragraph of a document the implementer is never told to open. Rubric 5.1 names one container for the backend and one for the frontend, 5.2 requires "separate EC2 instances", 3.2 requires the dashboard on "a different EC2 server". Three instances, and a test that counts them.

**Elastic IPs** because an EC2 public IPv4 address is released on stop and a different one is assigned on start, and the cost model explicitly instructs stopping between sessions. Any URL captured during development would be dead by the next session. Since 2024 AWS charges for *every* public IPv4 address including auto-assigned ones, so an EIP attached to a running instance costs exactly what the auto-assigned address already cost; the only marginal charge is while the instance is stopped.

**Files:**
- Create: `infra/terraform/compute.tf`, `infra/terraform/templates/user_data.sh.tftpl`
- Test: `tests/infra/test_compute.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_compute.py`:
```python
"""H2 three instances; C7 pinned AMI; H27 awslogs; IMDSv2 with hop limit 2; EIPs."""

import pytest

from tests.infra import tfparse

EXPECTED = {
    "backend": ("t4g.medium", "${aws_subnet.public_a.id}"),
    "frontend": ("t4g.small", "${aws_subnet.public_b.id}"),
    "monitoring": ("t4g.medium", "${aws_subnet.public_a.id}"),
}
TIERS = list(EXPECTED)
ALLOWED_BY_SCP = {"t4g.small", "t4g.medium", "t4g.large", "c7g.xlarge"}


def test_there_are_exactly_three_instances_one_per_tier():
    assert set(tfparse.resources("aws_instance")) == set(TIERS)


@pytest.mark.parametrize("tier", TIERS)
def test_each_instance_has_the_sized_class_in_the_scp_allowlist(tier):
    instance_type = tfparse.resource("aws_instance", tier)["instance_type"]
    assert instance_type == EXPECTED[tier][0]
    assert instance_type in ALLOWED_BY_SCP, "the Sandbox OU SCP would deny this launch"


@pytest.mark.parametrize("tier", TIERS)
def test_each_instance_sits_in_a_public_subnet(tier):
    assert tfparse.resource("aws_instance", tier)["subnet_id"] == EXPECTED[tier][1]


@pytest.mark.parametrize("tier", TIERS)
def test_each_instance_uses_its_own_security_group_and_instance_profile(tier):
    instance = tfparse.resource("aws_instance", tier)
    assert instance["vpc_security_group_ids"] == [f"${{aws_security_group.{tier}.id}}"]
    assert instance["iam_instance_profile"] == f"${{aws_iam_instance_profile.{tier}.name}}"


@pytest.mark.parametrize("tier", TIERS)
def test_imdsv2_is_required_with_hop_limit_two(tier):
    meta = tfparse.blocks(tfparse.resource("aws_instance", tier), "metadata_options")[0]
    assert meta["http_endpoint"] == "enabled"
    assert meta["http_tokens"] == "required", "IMDSv1 leaves the SSRF path open"
    # Hop limit 1 stops a container on the default bridge network reaching IMDS at
    # all, so no container could pull from ECR or read a secret. 2 restores that and
    # widens the SSRF blast radius to containerised code; the tradeoff is written
    # down in docs/runbooks/no-ssh-debug.md §6.
    assert meta["http_put_response_hop_limit"] == 2


@pytest.mark.parametrize("tier", TIERS)
def test_each_instance_pins_the_ami_and_ignores_drift_on_it(tier):
    instance = tfparse.resource("aws_instance", tier)
    assert instance["ami"] == "${var.ami_id}"
    lifecycle = tfparse.blocks(instance, "lifecycle")[0]
    assert "ami" in lifecycle["ignore_changes"], (
        "without ignore_changes an AL2023 republication replaces all three instances"
    )


@pytest.mark.parametrize("tier", TIERS)
def test_root_volume_is_encrypted_gp3(tier):
    root = tfparse.blocks(tfparse.resource("aws_instance", tier), "root_block_device")[0]
    assert root["encrypted"] is True
    assert root["volume_type"] == "gp3"


@pytest.mark.parametrize("tier", TIERS)
def test_each_instance_carries_the_tags_ssm_send_command_selects_on(tier):
    tags = tfparse.resource("aws_instance", tier)["tags"]
    assert tags["Component"] == tier
    assert tags["Project"] == "${var.project}"


@pytest.mark.parametrize("tier", TIERS)
def test_each_instance_has_a_stable_elastic_ip(tier):
    assert tfparse.resource("aws_eip", tier)["domain"] == "vpc"
    assoc = tfparse.resource("aws_eip_association", tier)
    assert assoc["instance_id"] == f"${{aws_instance.{tier}.id}}"
    assert assoc["allocation_id"] == f"${{aws_eip.{tier}.id}}"


def test_user_data_configures_the_awslogs_driver_against_the_terraform_log_group():
    template = (tfparse.MAIN / "templates" / "user_data.sh.tftpl").read_text()
    assert "--log-driver=awslogs" in template
    assert "awslogs-group=${log_group}" in template
    assert "awslogs-region=${region}" in template


def test_user_data_verifies_the_compose_binary_checksum():
    template = (tfparse.MAIN / "templates" / "user_data.sh.tftpl").read_text()
    assert "sha256sum -c" in template
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_compute.py -v`
Expected: FAIL with `AssertionError: assert set() == {'backend', 'frontend', 'monitoring'}` and, for the parametrised cases, `AssertionError: missing resource aws_instance.backend; found []`

- [ ] **Step 3: Write minimal implementation**

`infra/terraform/templates/user_data.sh.tftpl`:
```bash
#!/bin/bash
set -euxo pipefail
exec > >(tee /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

dnf -y install docker
systemctl enable --now docker
usermod -aG docker ec2-user

# Compose plugin, checksum-verified. Downloaded under its published name so the
# .sha256 manifest matches.
cd /tmp
curl -fsSL -O "https://github.com/docker/compose/releases/download/${compose_version}/docker-compose-linux-aarch64"
curl -fsSL -O "https://github.com/docker/compose/releases/download/${compose_version}/docker-compose-linux-aarch64.sha256"
sha256sum -c docker-compose-linux-aarch64.sha256
install -D -m 0755 docker-compose-linux-aarch64 /usr/local/lib/docker/cli-plugins/docker-compose
rm -f docker-compose-linux-aarch64 docker-compose-linux-aarch64.sha256

# Deploy-time environment. Phase 5's compose file and the SSM roll read these.
cat > /etc/toxic-mod.env <<'TOXICMODENV'
AWS_DEFAULT_REGION=${region}
TOXIC_MOD_COMPONENT=${component}
TOXIC_MOD_LOG_GROUP=${log_group}
TOXIC_MOD_ECR_REGISTRY=${ecr_registry}
TOXIC_MOD_DB_HOST=${db_host}
TOXIC_MOD_DB_NAME=${db_name}
TOXICMODENV

# Container logs must leave the box (premortem H27). Every container this host
# runs uses the awslogs driver against the Terraform-created group; the group
# already exists, so the driver needs no logs:CreateLogGroup permission.
cat > /etc/docker/daemon.json <<'TOXICMODDOCKER'
{
  "log-driver": "awslogs",
  "log-opts": {
    "awslogs-region": "${region}",
    "awslogs-group": "${log_group}"
  }
}
TOXICMODDOCKER
systemctl restart docker

# Explicit form for anything started by hand or by SSM Run Command:
#   docker run --log-driver=awslogs --log-opt awslogs-region=${region} \
#              --log-opt awslogs-group=${log_group} --log-opt awslogs-stream=${component} ...

aws ecr get-login-password --region ${region} \
  | docker login --username AWS --password-stdin ${ecr_registry}

touch /var/lib/cloud/toxic-mod-bootstrapped
```

`infra/terraform/compute.tf`:
```hcl
# ---------------------------------------------------------------------------
# Three instances, one per graded tier (premortem H2). Rubric 5.1 names one
# container for the backend and one for the frontend, 5.2 requires deployment
# "to separate EC2 instances", and 3.2 requires the monitoring dashboard on
# "a different EC2 server". Two instances satisfied 3.2 only on a permissive
# reading and left 5.1 plus 5.2 arguable.
#
# Every instance:
#   - pins the AMI and ignores drift on it (premortem C7), so an AL2023
#     republication cannot replace all three mid-grading;
#   - requires IMDSv2 with hop limit 2, tradeoff in the no-SSH runbook §6;
#   - ships container logs through the awslogs driver (premortem H27);
#   - holds a stable Elastic IP, so the demo URL survives a stop/start cycle.
# ---------------------------------------------------------------------------

locals {
  ecr_registry    = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
  compose_version = "v5.3.1"
}

resource "aws_instance" "backend" {
  ami                    = var.ami_id
  instance_type          = "t4g.medium"
  subnet_id              = aws_subnet.public_a.id
  vpc_security_group_ids = [aws_security_group.backend.id]
  iam_instance_profile   = aws_iam_instance_profile.backend.name

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    region          = var.region
    component       = "backend"
    log_group       = aws_cloudwatch_log_group.app["backend"].name
    ecr_registry    = local.ecr_registry
    compose_version = local.compose_version
    db_host         = aws_db_instance.main.address
    db_name         = aws_db_instance.main.db_name
  })

  lifecycle {
    ignore_changes = [ami, user_data]
  }

  tags = {
    Name      = "${var.project}-backend"
    Project   = var.project
    Component = "backend"
  }
}

resource "aws_instance" "frontend" {
  ami                    = var.ami_id
  instance_type          = "t4g.small"
  subnet_id              = aws_subnet.public_b.id
  vpc_security_group_ids = [aws_security_group.frontend.id]
  iam_instance_profile   = aws_iam_instance_profile.frontend.name

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    region          = var.region
    component       = "frontend"
    log_group       = aws_cloudwatch_log_group.app["frontend"].name
    ecr_registry    = local.ecr_registry
    compose_version = local.compose_version
    db_host         = aws_db_instance.main.address
    db_name         = aws_db_instance.main.db_name
  })

  lifecycle {
    ignore_changes = [ami, user_data]
  }

  tags = {
    Name      = "${var.project}-frontend"
    Project   = var.project
    Component = "frontend"
  }
}

resource "aws_instance" "monitoring" {
  ami                    = var.ami_id
  instance_type          = "t4g.medium"
  subnet_id              = aws_subnet.public_a.id
  vpc_security_group_ids = [aws_security_group.monitoring.id]
  iam_instance_profile   = aws_iam_instance_profile.monitoring.name

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    region          = var.region
    component       = "monitoring"
    log_group       = aws_cloudwatch_log_group.app["monitoring"].name
    ecr_registry    = local.ecr_registry
    compose_version = local.compose_version
    db_host         = aws_db_instance.main.address
    db_name         = aws_db_instance.main.db_name
  })

  lifecycle {
    ignore_changes = [ami, user_data]
  }

  tags = {
    Name      = "${var.project}-monitoring"
    Project   = var.project
    Component = "monitoring"
  }
}

# ---------------------------------------------------------------------------
# Elastic IPs. An auto-assigned public IPv4 address is released on stop and a
# different one assigned on start, and the cost model instructs stopping
# between sessions, so any captured URL would be dead by the next session.
# Since 2024 every public IPv4 address bills at the same rate whether it is
# auto-assigned or elastic, so the only marginal cost is while stopped.
# ---------------------------------------------------------------------------

resource "aws_eip" "backend" {
  domain = "vpc"
  tags   = { Name = "${var.project}-backend" }
}

resource "aws_eip" "frontend" {
  domain = "vpc"
  tags   = { Name = "${var.project}-frontend" }
}

resource "aws_eip" "monitoring" {
  domain = "vpc"
  tags   = { Name = "${var.project}-monitoring" }
}

resource "aws_eip_association" "backend" {
  instance_id   = aws_instance.backend.id
  allocation_id = aws_eip.backend.id
}

resource "aws_eip_association" "frontend" {
  instance_id   = aws_instance.frontend.id
  allocation_id = aws_eip.frontend.id
}

resource "aws_eip_association" "monitoring" {
  instance_id   = aws_instance.monitoring.id
  allocation_id = aws_eip.monitoring.id
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make tf-fmt && terraform -chdir=infra/terraform validate && .venv/bin/pytest tests/infra/test_compute.py tests/infra/test_ami_pin.py -v`
Expected: `Success! The configuration is valid.`, 26 PASS in `test_compute.py`, and 5 PASS in `test_ami_pin.py` — where `test_no_resource_reads_an_ami_from_a_data_source` is now load-bearing rather than vacuous.

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/compute.tf infra/terraform/templates tests/infra/test_compute.py
git commit -m "Add three tier-separated EC2 instances with pinned AMI, IMDSv2 and Elastic IPs"
```

---

### Task 14 (H27, part 2): Health alarms wired to the existing SNS topic

**Finding H27, second clause:** the only alarm in the entire design is for root sign-in. Nothing pages when `/predict` is down — which the design spec §10 makes a *designed* behaviour whenever RDS is unreachable, since `/predict` returns 503 on persistence failure.

Two alarms, both to the topic Task 8 already created. `StatusCheckFailed` catches a dead instance; a log metric filter on 503 responses catches the failure mode the design actually engineers in. Both use `treat_missing_data = "notBreaching"` so the nightly stop schedule from Task 15 does not page every night at 23:00.

**Files:**
- Modify: `infra/terraform/observability.tf`
- Test: `tests/infra/test_health_alarm.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_health_alarm.py`:
```python
"""H27: something pages when the backend is down."""

import pytest

from tests.infra import tfparse

ALARMS = ["backend_status_check", "backend_predict_unavailable"]


@pytest.mark.parametrize("alarm", ALARMS)
def test_alarm_notifies_the_existing_alerts_topic(alarm):
    body = tfparse.resource("aws_cloudwatch_metric_alarm", alarm)
    assert body["alarm_actions"] == ["${aws_sns_topic.alerts.arn}"]
    assert body["ok_actions"] == ["${aws_sns_topic.alerts.arn}"]


@pytest.mark.parametrize("alarm", ALARMS)
def test_a_stopped_instance_does_not_page_every_night(alarm):
    # The nightly stop schedule takes the instances down on purpose. Without this,
    # the alarm fires at 23:00 daily and the operator learns to ignore it.
    body = tfparse.resource("aws_cloudwatch_metric_alarm", alarm)
    assert body["treat_missing_data"] == "notBreaching"


def test_status_check_alarm_watches_the_backend_instance():
    body = tfparse.resource("aws_cloudwatch_metric_alarm", "backend_status_check")
    assert body["namespace"] == "AWS/EC2"
    assert body["metric_name"] == "StatusCheckFailed"
    assert body["dimensions"]["InstanceId"] == "${aws_instance.backend.id}"


def test_a_metric_filter_counts_503_responses_in_the_backend_log_group():
    # /predict returns 503 when a prediction cannot be persisted. That is the
    # designed behaviour when RDS is unreachable, and it is exactly the outage
    # nothing previously noticed.
    filt = tfparse.resource("aws_cloudwatch_log_metric_filter", "backend_503")
    assert filt["log_group_name"] == '${aws_cloudwatch_log_group.app["backend"].name}'
    assert "503" in filt["pattern"]
    transformation = tfparse.blocks(filt, "metric_transformation")[0]
    # default_value 0 is what makes the metric report during healthy periods; without
    # it the alarm sits in INSUFFICIENT_DATA and never transitions.
    assert transformation["default_value"] == "0"


def test_predict_unavailable_alarm_uses_the_metric_filter_namespace():
    body = tfparse.resource("aws_cloudwatch_metric_alarm", "backend_predict_unavailable")
    transformation = tfparse.blocks(
        tfparse.resource("aws_cloudwatch_log_metric_filter", "backend_503"),
        "metric_transformation",
    )[0]
    assert body["namespace"] == transformation["namespace"]
    assert body["metric_name"] == transformation["name"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_health_alarm.py -v`
Expected: 8 failures, all `AssertionError: missing resource aws_cloudwatch_metric_alarm.backend_status_check; found []`

- [ ] **Step 3: Write minimal implementation**

Append to `infra/terraform/observability.tf`:
```hcl
# ---------------------------------------------------------------------------
# Health alarms (premortem H27). Both notify the topic above.
#
# treat_missing_data = "notBreaching" on both, because the nightly stop schedule
# in budget.tf takes the instances down on purpose every night. An alarm that
# pages nightly is an alarm the operator learns to ignore.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "backend_status_check" {
  alarm_name          = "${var.project}-backend-status-check"
  alarm_description   = "EC2 #1 failed its instance or system status check."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = aws_instance.backend.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# /predict returns 503 when a prediction cannot be persisted, which the design
# makes a deliberate behaviour whenever RDS is unreachable. That is the outage
# nothing previously noticed.
resource "aws_cloudwatch_log_metric_filter" "backend_503" {
  name           = "${var.project}-backend-503"
  log_group_name = aws_cloudwatch_log_group.app["backend"].name
  pattern        = "\"503 Service Unavailable\""

  metric_transformation {
    name          = "PredictUnavailable"
    namespace     = "${var.project}/backend"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "backend_predict_unavailable" {
  alarm_name          = "${var.project}-predict-unavailable"
  alarm_description   = "/predict is returning 503, which means persistence is failing."
  namespace           = "${var.project}/backend"
  metric_name         = "PredictUnavailable"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  depends_on = [aws_cloudwatch_log_metric_filter.backend_503]
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make tf-fmt && terraform -chdir=infra/terraform validate && .venv/bin/pytest tests/infra/test_health_alarm.py -v`
Expected: `Success! The configuration is valid.` and 8 PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/observability.tf tests/infra/test_health_alarm.py
git commit -m "Alarm on backend status checks and on 503 responses from /predict"
```

---

### Task 14a (H27): The alarm must reach a human — a CONFIRMED subscriber and one proven delivery

The observability half of H27 is closed by Tasks 8 and 14: logs ship, one group per component at fourteen-day retention, a metric filter counts 503s, and two alarms notify the topic with `treat_missing_data = "notBreaching"`. **The paging half is not.**

`aws_sns_topic_subscription.alerts_email` uses `protocol = "email"`, and an AWS email subscription is created in **`PendingConfirmation`** state until the recipient clicks the link in the confirmation mail. Terraform reports success either way — `terraform apply` is green, the subscription resource exists, and the topic has zero confirmed subscribers. `grep -rn "SubscriptionArn\|list-subscriptions\|confirm the subscription"` across all eight plans returns nothing, and Task 21's real apply has no step that checks it.

So H27's exact complaint — "Nothing pages when `/predict` is down" — survives remediation in its operative form: the alarm transitions to ALARM, publishes to a topic nobody is subscribed to, and the operator finds out from the grader.

A subscription is not a notification channel until a message has travelled it. This task requires both: a confirmed subscriber, and one alarm actually driven to ALARM with the received notification recorded.

**Files:**
- Create: `docs/evidence/a2-alarm-delivery.md`
- Test: `tests/infra/test_health_alarm.py` (append two live cases, run in Task 21 after `terraform apply`)

- [ ] **Step 1: Write the failing test**

Append to `tests/infra/test_health_alarm.py`:
```python
import json
import re
import subprocess
from pathlib import Path

import pytest

EVIDENCE = Path("docs/evidence/a2-alarm-delivery.md")


@pytest.mark.awsapply
def test_the_alerts_topic_has_at_least_one_confirmed_subscriber():
    """An email subscription sits in PendingConfirmation until someone clicks the link.
    Terraform reports success either way, so a green apply proves nothing about paging."""
    topic_arn = subprocess.check_output(
        ["terraform", "-chdir=infra/terraform", "output", "-raw", "alerts_topic_arn"],
        text=True,
    ).strip()
    subs = json.loads(
        subprocess.check_output(
            ["aws", "sns", "list-subscriptions-by-topic",
             "--topic-arn", topic_arn, "--output", "json"],
            text=True,
        )
    )["Subscriptions"]
    confirmed = [s for s in subs if s["SubscriptionArn"].startswith("arn:")]
    assert confirmed, (
        f"every subscription is PendingConfirmation ({[s['SubscriptionArn'] for s in subs]}); "
        "the alarm pages nobody (H27)"
    )


@pytest.mark.awsapply
def test_alarm_delivery_was_proven_end_to_end():
    """A confirmed subscription is necessary and not sufficient: the alarm action, the topic
    policy, and the delivery all have to work together, and only a real transition tests all
    three at once."""
    assert EVIDENCE.exists(), "no record that any alarm notification was ever received"
    body = EVIDENCE.read_text(encoding="utf-8")
    assert re.search(r"20\d\d-\d\d-\d\d", body), "no dated delivery record"
    assert "toxicmod-predict-unavailable" in body or "predict_unavailable" in body
    assert "received" in body.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_health_alarm.py -m awsapply -v` (after the Task 21 apply)
Expected: FAIL — `every subscription is PendingConfirmation (['PendingConfirmation'])` and `AssertionError: no record that any alarm notification was ever received`.

- [ ] **Step 3: Confirm the subscription and prove one delivery**

```bash
# 1. Confirm. The link arrives at var.alerts_email; click it, then verify from the CLI.
TOPIC=$(terraform -chdir=infra/terraform output -raw alerts_topic_arn)
aws sns list-subscriptions-by-topic --topic-arn "$TOPIC" \
  --query 'Subscriptions[].{Endpoint:Endpoint,Arn:SubscriptionArn}' --output table

# 2. Drive the real alarm to ALARM and let the real action fire.
aws cloudwatch set-alarm-state \
  --alarm-name toxicmod-predict-unavailable \
  --state-value ALARM \
  --state-reason "delivery test $(date -u +%FT%TZ)"

# 3. Put it back.
aws cloudwatch set-alarm-state --alarm-name toxicmod-predict-unavailable \
  --state-value OK --state-reason "delivery test complete"
```

Record the result in `docs/evidence/a2-alarm-delivery.md`, redacted through `scripts/redact.py`:
```markdown
# Alarm delivery, proven end to end

| Date (UTC) | Alarm | Action | Received at | Latency |
|---|---|---|---|---|
| `<YYYY-MM-DD HH:MM>` | `toxicmod-predict-unavailable` | `set-alarm-state --state-value ALARM` | `<YYYY-MM-DD HH:MM>` | `<seconds>` |

Confirmed subscribers on `alerts_topic_arn` at the time of the test:

`<paste the redacted list-subscriptions-by-topic table; SubscriptionArn must start with arn:>`

Notification received (headers redacted):

`<paste the subject line and the first lines of the body>`
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/infra/test_health_alarm.py -m awsapply -v`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/evidence/a2-alarm-delivery.md tests/infra/test_health_alarm.py
git commit -m "Prove the health alarm reaches a confirmed subscriber end to end"
```

**Amendment to Task 21.** Add to the real-apply step list, after the apply and before the destroy: `- [ ] Confirm the SNS email subscription, run the set-alarm-state delivery test, and record it in docs/evidence/a2-alarm-delivery.md. Run `pytest tests/infra/test_health_alarm.py -m awsapply`; both cases must pass before the destroy.` A destroy without this step means the confirmation has to be repeated on the next apply — note that in the evidence file, because SNS subscription confirmation does **not** survive `terraform destroy`.

---

### Task 15 (H7): `budget.tf` — the $100 budget and a nightly stop as a HARD control

**Finding H7, second clause:** the AWS foundation spec declines an automated budget stop action by owner decision, which leaves the SCP instance-type allowlist as the only hard control. That allowlist caps the *rate*, not the *duration* — three allowlisted instances left running for a month reach the ceiling without ever violating the SCP. A nightly EventBridge schedule that stops EC2 and RDS is a hard control on duration, and it is not the budget-action mechanism the owner declined.

The schedule is disableable by variable for the grading window, because an instance stopped at 23:00 while a grader is looking is worse than the spend it saves.

**Files:**
- Create: `infra/terraform/budget.tf`
- Modify: `infra/terraform/variables.tf`
- Test: `tests/infra/test_budget.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_budget.py`:
```python
"""H7: budget alerts on both actual and forecast, plus a hard duration control."""

from tests.infra import tfparse


def test_budget_is_capped_at_the_variable_and_scoped_to_the_month():
    budget = tfparse.resource("aws_budgets_budget", "monthly")
    assert budget["limit_amount"] == "${var.monthly_budget_usd}"
    assert budget["limit_unit"] == "USD"
    assert budget["time_unit"] == "MONTHLY"
    assert budget["budget_type"] == "COST"


def test_six_notifications_cover_actual_and_forecast_at_50_80_and_100():
    pairs = {
        (entry["type"], entry["threshold"])
        for entry in tfparse.local_values()["budget_notifications"]
    }
    assert pairs == {
        ("ACTUAL", 50),
        ("ACTUAL", 80),
        ("ACTUAL", 100),
        ("FORECASTED", 50),
        ("FORECASTED", 80),
        ("FORECASTED", 100),
    }


def test_notifications_reach_both_sns_and_email():
    dynamic = tfparse.blocks(tfparse.resource("aws_budgets_budget", "monthly"), "dynamic")[0]
    content = tfparse.blocks(dynamic["notification"], "content")[0]
    assert content["subscriber_sns_topic_arns"] == ["${aws_sns_topic.alerts.arn}"]
    assert content["subscriber_email_addresses"] == ["${var.alert_email}"]


def test_a_nightly_stop_schedule_exists_for_ec2_and_for_rds():
    ec2 = tfparse.resource("aws_scheduler_schedule", "nightly_stop_ec2")
    rds = tfparse.resource("aws_scheduler_schedule", "nightly_stop_rds")
    assert ec2["target"][0]["arn"] == "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    assert rds["target"][0]["arn"] == "arn:aws:scheduler:::aws-sdk:rds:stopDBInstance"


def test_the_schedule_is_hard_not_flexible():
    for name in ("nightly_stop_ec2", "nightly_stop_rds"):
        window = tfparse.blocks(tfparse.resource("aws_scheduler_schedule", name), "flexible_time_window")[0]
        assert window["mode"] == "OFF"


def test_the_schedule_is_on_by_default_and_disableable_for_the_demo():
    toggle = tfparse.variables()["nightly_stop_enabled"]
    assert toggle["default"] is True, "the control must be on unless deliberately disabled"
    for name in ("nightly_stop_ec2", "nightly_stop_rds"):
        state = tfparse.resource("aws_scheduler_schedule", name)["state"]
        assert "var.nightly_stop_enabled" in state


def test_the_schedule_names_the_three_instances_explicitly():
    target = tfparse.resource("aws_scheduler_schedule", "nightly_stop_ec2")["target"][0]
    for tier in ("backend", "frontend", "monitoring"):
        assert f"aws_instance.{tier}.id" in target["input"]


def test_the_scheduler_role_cannot_stop_anything_else():
    resources = []
    for statement in tfparse.blocks(
        tfparse.data_source("aws_iam_policy_document", "scheduler"), "statement"
    ):
        resources.extend(statement["resources"])
    assert "*" not in resources, "a wildcard here can stop resources outside this project"
    assert any("aws_instance.backend.arn" in r for r in resources)
    assert any("aws_db_instance.main.arn" in r for r in resources)


def test_the_scheduler_trust_policy_pins_the_source_account():
    doc = tfparse.data_source("aws_iam_policy_document", "scheduler_assume")
    conditions = [
        c
        for statement in tfparse.blocks(doc, "statement")
        for c in tfparse.blocks(statement, "condition")
    ]
    assert any(c["variable"] == "aws:SourceAccount" for c in conditions)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_budget.py -v`
Expected: 9 failures, the first `AssertionError: missing resource aws_budgets_budget.monthly; found []`

- [ ] **Step 3: Write minimal implementation**

Append to `infra/terraform/variables.tf`:
```hcl
variable "nightly_stop_enabled" {
  description = <<-EOT
    Whether the nightly EventBridge schedule stops EC2 and RDS. On by default,
    because the SCP instance-type allowlist caps the hourly rate but not the
    duration, and three allowlisted instances left up for a month reach the $100
    ceiling without ever violating the SCP (premortem H7). Set to false for the
    grading window with `terraform apply -var nightly_stop_enabled=false`, and
    set it back afterwards.
  EOT
  type        = bool
  default     = true
}

variable "nightly_stop_cron" {
  description = "EventBridge Scheduler expression, evaluated in nightly_stop_timezone."
  type        = string
  default     = "cron(0 23 * * ? *)"
}

variable "nightly_stop_timezone" {
  description = "IANA timezone the nightly stop cron is evaluated in."
  type        = string
  default     = "America/Denver"
}
```

`infra/terraform/budget.tf`:
```hcl
locals {
  budget_notifications = [
    { type = "ACTUAL", threshold = 50 },
    { type = "ACTUAL", threshold = 80 },
    { type = "ACTUAL", threshold = 100 },
    { type = "FORECASTED", threshold = 50 },
    { type = "FORECASTED", threshold = 80 },
    { type = "FORECASTED", threshold = 100 },
  ]
}

resource "aws_budgets_budget" "monthly" {
  name         = "${var.project}-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = local.budget_notifications

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.type
      subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
      subscriber_email_addresses = [var.alert_email]
    }
  }

  depends_on = [aws_sns_topic_policy.alerts]
}

# ---------------------------------------------------------------------------
# Nightly stop, as a HARD cost control (premortem H7).
#
# The owner declined an automated *budget action*, and this is not one: it is a
# fixed schedule, not a spend-triggered intervention. It closes the gap the SCP
# cannot, because the instance-type allowlist caps the hourly rate and says
# nothing about duration. Left running continuously, three allowlisted
# instances plus RDS reach the $100 ceiling inside a month without a single SCP
# violation. See docs/cost-model.md scenario C.
#
# It also disarms the seven-day RDS auto-restart: a database stopped nightly and
# started each working morning never accumulates seven stopped days.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project}-nightly-stop"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid     = "StopTheThreeProjectInstances"
    effect  = "Allow"
    actions = ["ec2:StopInstances"]
    resources = [
      aws_instance.backend.arn,
      aws_instance.frontend.arn,
      aws_instance.monitoring.arn,
    ]
  }

  statement {
    sid       = "StopTheProjectDatabase"
    effect    = "Allow"
    actions   = ["rds:StopDBInstance"]
    resources = [aws_db_instance.main.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${var.project}-nightly-stop"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule" "nightly_stop_ec2" {
  name       = "${var.project}-nightly-stop-ec2"
  group_name = "default"
  state      = var.nightly_stop_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.nightly_stop_cron
  schedule_expression_timezone = var.nightly_stop_timezone

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    role_arn = aws_iam_role.scheduler.arn

    input = jsonencode({
      InstanceIds = [
        aws_instance.backend.id,
        aws_instance.frontend.id,
        aws_instance.monitoring.id,
      ]
    })

    retry_policy {
      maximum_retry_attempts = 3
    }
  }
}

resource "aws_scheduler_schedule" "nightly_stop_rds" {
  name       = "${var.project}-nightly-stop-rds"
  group_name = "default"
  state      = var.nightly_stop_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.nightly_stop_cron
  schedule_expression_timezone = var.nightly_stop_timezone

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:rds:stopDBInstance"
    role_arn = aws_iam_role.scheduler.arn

    input = jsonencode({
      DbInstanceIdentifier = aws_db_instance.main.identifier
    })

    retry_policy {
      maximum_retry_attempts = 3
    }
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make tf-fmt && terraform -chdir=infra/terraform validate && .venv/bin/pytest tests/infra/test_budget.py -v`
Expected: `Success! The configuration is valid.` and 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/budget.tf infra/terraform/variables.tf tests/infra/test_budget.py
git commit -m "Add the monthly budget with actual and forecast alerts and a nightly stop schedule"
```

---

### Task 16 (H7): Rebuild the cost model with every omitted line item

**Finding H7:** the `$0.101/hr` figure counted four on-demand rates and nothing else — no Elastic IPs, no EBS, no RDS storage or backups, no CloudTrail S3, no GuardDuty, no ECR, no Secrets Manager at roughly `$0.40` per secret per month, no CloudWatch, no SNS, no data transfer. The omissions are not a rounding error: they are roughly `$27` per month of cost that exists whether or not anything is running.

**Files:**
- Create: `docs/cost-model.md`
- Modify: `tests/infra/test_docs_controls.py`
- Test: appended cases in `tests/infra/test_docs_controls.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/infra/test_docs_controls.py`:
```python
COST = DOCS / "cost-model.md"

REQUIRED_LINE_ITEMS = [
    "Elastic IP",
    "EBS",
    "RDS storage",
    "RDS backup",
    "CloudTrail",
    "GuardDuty",
    "ECR",
    "Secrets Manager",
    "CloudWatch",
    "SNS",
    "Data transfer",
    "EventBridge",
    "Terraform state",
]


def test_cost_model_exists():
    assert COST.exists()


@pytest.mark.parametrize("item", REQUIRED_LINE_ITEMS)
def test_cost_model_prices_every_previously_omitted_line_item(item):
    assert item in COST.read_text(), f"{item} is still missing from the cost model"


def test_cost_model_prices_secrets_manager_per_secret():
    text = COST.read_text()
    assert "0.40" in text, "Secrets Manager is ~$0.40 per secret per month"
    assert "4 " in text or "four" in text.lower(), "three Terraform secrets plus the RDS-managed one"


def test_cost_model_separates_fixed_monthly_cost_from_hourly_running_cost():
    text = COST.read_text()
    assert "Fixed monthly" in text
    assert "Variable, per running hour" in text


def test_cost_model_carries_three_duty_cycle_scenarios_against_the_ceiling():
    text = COST.read_text()
    for scenario in ("Scenario A", "Scenario B", "Scenario C"):
        assert scenario in text
    assert "$100" in text


def test_cost_model_supersedes_the_old_figure_explicitly():
    # The premortem's specific complaint was a number that read as authoritative.
    assert "$0.101/hr" in COST.read_text()


def test_cost_model_names_the_nightly_stop_as_the_control_that_makes_it_hold():
    assert "nightly_stop_enabled" in COST.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_docs_controls.py -v`
Expected: the nine Task 6 cases PASS; 19 new cases fail, the first being `AssertionError: assert False` from `COST.exists()`.

- [ ] **Step 3: Write minimal implementation**

`docs/cost-model.md`:
```markdown
# Cost model: `rockcyber-mlops-toxic`

- Rebuilt 2026-07-31 against premortem finding H7.
- Region `us-west-2`. Rates are approximate list prices and are to be confirmed in
  the AWS Pricing Calculator and against the first full bill, not trusted from here.
- Budget ceiling: **$100 per month**, alerting at 50, 80 and 100 percent of both
  actual and forecast spend.

## Why this document replaces the previous figure

The superseded estimate was **`$0.101/hr` with everything running**. It counted four
on-demand compute rates and nothing else. Everything below the compute rows in the
next table was missing, including roughly **$27 per month that accrues whether or not
a single instance is running**. At the planned duty cycle that omission is larger than
the number it was attached to.

## Fixed monthly cost — accrues even with everything stopped

| Line item | Basis | Rate | Quantity | Monthly |
|---|---|---|---|---|
| Elastic IP addresses | Every public IPv4 address, in use or idle, since Feb 2024 | $0.005 / hr | 3 × 730 hr | **$10.95** |
| EBS root volumes | gp3 | $0.08 / GB-month | 30 + 20 + 30 = 80 GB | **$6.40** |
| GuardDuty | Scales with CloudTrail, VPC flow and DNS log volume | estimate | 1 detector | **$4.00** |
| RDS storage | gp3, allocated not used | $0.115 / GB-month | 20 GB | **$2.30** |
| Secrets Manager | Per secret. 3 created by Terraform plus 1 RDS-managed master | $0.40 / secret-month | 4 secrets | **$1.60** |
| ECR storage | Four repositories, 30 retained tags each | $0.10 / GB-month | ~6 GB | **$0.60** |
| CloudWatch Logs | 14-day retention; ingestion $0.50/GB plus storage $0.03/GB-month | mixed | ~1 GB/month | **$0.53** |
| CloudTrail | First copy of management events is free; this is the S3 storage | $0.023 / GB-month | ~10 GB | **$0.25** |
| Terraform state | S3 standard, versioned, tiny | $0.023 / GB-month | < 1 MB | **$0.02** |
| RDS backup storage | Free up to 100% of allocated storage; 7-day retention on 20 GB stays inside it | $0.095 / GB-month above allocation | 0 GB billable | **$0.00** |
| SNS | Email notifications; first 1,000 per month are free | $0 | ~50 | **$0.00** |
| CloudWatch alarms | First 10 standard alarms per account are free | $0 | 2 | **$0.00** |
| EventBridge Scheduler | First 14 million invocations per month are free | $0 | ~60 | **$0.00** |
| Data transfer out | First 100 GB per month is free across the account | $0 | < 1 GB | **$0.00** |
| **Fixed monthly subtotal** | | | | **$26.65** |

## Variable, per running hour — only while EC2 and RDS are up

| Line item | Class | Hourly |
|---|---|---|
| EC2 #1 backend | `t4g.medium` | $0.0336 |
| EC2 #2 frontend | `t4g.small` | $0.0168 |
| EC2 #3 monitoring | `t4g.medium` | $0.0336 |
| RDS | `db.t4g.micro` | $0.0160 |
| **Variable subtotal** | | **$0.100 / hr** |

The Elastic IP charge is deliberately *not* in this table. It bills 24/7 whether the
instance is running, stopped, or the address is unattached, so it belongs in the fixed
block. That placement is the single largest correction to the old figure.

## Scenarios against the $100 ceiling

The project runs from 2026-07-30 to 2026-08-18, which is 19 days, spanning one billing
month. Fixed cost is prorated at 19/30.

| | Running hours | Variable | Fixed (19/30 of $26.65) | Total |
|---|---|---|---|---|
| **Scenario A — planned.** 6 hours per work session, nightly stop enforced | 114 | $11.40 | $16.88 | **$28.28** |
| **Scenario B — nightly stop disabled and forgotten for the whole project** | 456 | $45.60 | $16.88 | **$62.48** |
| **Scenario C — everything left running for a full billing month** | 730 | $73.00 | $26.65 | **$99.65** |

Scenario C is the number that matters. It sits **at** the ceiling, and it is reachable
without a single service control policy violation, because the SCP instance-type
allowlist caps the hourly *rate* and says nothing about *duration*. That is precisely
the gap the nightly stop schedule closes.

## Controls, strongest first

1. **`terraform destroy`.** Full teardown. It works because the ECR repositories set
   `force_delete`, RDS has `deletion_protection = false` and a unique
   `final_snapshot_identifier`, and the CloudTrail bucket sets `force_destroy`. The
   final snapshot preserves the graded dashboard dataset across a teardown.
2. **The nightly stop schedule** (`nightly_stop_enabled`, default `true`). A hard,
   scheduled stop of all three instances and the database at 23:00 America/Denver.
   Disable it deliberately for the grading window and re-enable it afterwards:
   `terraform apply -var nightly_stop_enabled=false`.
3. **The SCP instance-type allowlist** from Phase A1. A hard denial on the rate:
   `t4g.small`, `t4g.medium`, `t4g.large`, `c7g.xlarge` only, GPU and metal denied.
4. **Budget alerts** at 50, 80 and 100 percent of both actual and forecast, to SNS and
   to email. Detective, not preventive.

## Costs that survive a `terraform destroy`

Worth knowing before assuming teardown means zero.

| Item | Why it persists | How long |
|---|---|---|
| The RDS final snapshot | Deliberate: it is the graded dataset | Until deleted by hand; $0.095/GB-month beyond free tier |
| Deleted Secrets Manager secrets | `recovery_window_in_days = 7` | 7 days, at $0.40 per secret-month prorated |
| CloudTrail S3 objects | 90-day lifecycle expiry | Up to 90 days, cents |

## What to check against the real bill

GuardDuty is the only line here that is an estimate rather than a published rate for a
known quantity. Check it against the first full month in Cost Explorer, and if it is
materially above $4, decide whether it earns its place on a class project.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/infra/test_docs_controls.py -v`
Expected: 28 PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/cost-model.md tests/infra/test_docs_controls.py
git commit -m "Rebuild the cost model with the fourteen previously omitted line items"
```

---

### Task 17 (H15): TLS — the explicit decision, plus the control that makes it defensible

**Finding H15:** no TLS anywhere. No 443 listener, no ACM certificate, no load balancer, no reverse proxy in any Terraform file list — yet the delivery spec §4 asserts the frontend "calls the backend over **HTTPS**". The named harm is that the reviewer shared secret crosses the internet in cleartext.

The premortem allows either a terminator or an explicit documented decision. This takes the decision — but *only* because the specific named harm is closed structurally rather than accepted: the reviewer UI has no ingress rule on any security group, and the operator reaches it over `aws ssm start-session --document-name AWS-StartPortForwardingSession`, which the SSM service encrypts end to end. The shared secret therefore never crosses the internet at all. Task 5's `test_reviewer_ui_port_8503_has_no_ingress_anywhere` is the enforcement.

**Files:**
- Create: `docs/tls-decision.md`
- Modify: `tests/infra/test_docs_controls.py`, `docs/superpowers/specs/2026-07-30-delivery-plan-design.md`

- [ ] **Step 1: Write the failing test**

Append to `tests/infra/test_docs_controls.py`:
```python
TLS = DOCS / "tls-decision.md"


def test_tls_decision_exists():
    assert TLS.exists()


@pytest.mark.parametrize(
    "phrase",
    [
        "Application Load Balancer",
        "AWS Certificate Manager",
        "self-signed",
        "Let's Encrypt",
    ],
)
def test_tls_decision_shows_the_alternatives_it_rejected(phrase):
    assert phrase in TLS.read_text()


def test_tls_decision_states_the_decision_in_one_unambiguous_sentence():
    text = TLS.read_text()
    assert "Decision:" in text
    assert "cleartext HTTP" in text


def test_tls_decision_closes_the_named_harm_structurally():
    # The premortem's harm is the reviewer secret in cleartext. The answer is not
    # "accepted", it is "the reviewer UI is not on the internet".
    text = TLS.read_text()
    assert "8503" in text
    assert "AWS-StartPortForwardingSession" in text


def test_tls_decision_commits_to_rotating_the_reviewer_secret():
    assert "rotate" in TLS.read_text().lower()


def test_tls_decision_names_a_reopen_trigger():
    assert "Re-open this decision if" in TLS.read_text()


def test_delivery_spec_no_longer_claims_the_frontend_uses_https():
    spec = (
        DOCS
        / "superpowers"
        / "specs"
        / "2026-07-30-delivery-plan-design.md"
    ).read_text()
    assert "calls the backend over **HTTPS**" not in spec
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_docs_controls.py -v`
Expected: the 28 existing cases PASS; 10 new cases fail, starting with `AssertionError: assert False` from `TLS.exists()` and ending with the delivery-spec drift assertion failing on the live text.

- [ ] **Step 3: Write minimal implementation**

`docs/tls-decision.md`:
```markdown
# Decision record: no TLS terminator on the public listeners

- Date: 2026-07-31
- Status: accepted
- Closes: premortem finding H15
- Owner: Rock Lambros

**Decision: the three public listeners serve cleartext HTTP.** There is no load
balancer, no certificate, and no reverse proxy. The specific harm H15 names — the
reviewer shared secret crossing the internet in cleartext — is removed by taking the
reviewer interface off the internet entirely, not by accepting it.

## What is exposed, and on what

| Listener | Port | Ingress | Carries |
|---|---|---|---|
| FastAPI `/predict`, `/health` | 8000 | operator allowlist, plus the demo window | Public comment text submitted by the operator or a grader |
| Streamlit user UI | 8501 | operator allowlist, plus the demo window | The same text and the returned probabilities |
| Monitoring dashboard | 8502 | operator allowlist, plus the demo window | Aggregated counts, latencies and rates. No raw comment text |
| **Reviewer UI** | **8503** | **none, on any security group** | **The reviewer shared secret and raw comment text** |

The reviewer UI is reached only through Systems Manager, which is TLS-encrypted end to
end by the service and needs no ingress rule at all:

```bash
aws ssm start-session --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8503"],"localPortNumber":["8503"]}'
```

No credential of any kind is sent to a cleartext listener.

## Alternatives considered and why each was rejected

**Application Load Balancer plus AWS Certificate Manager.** The correct production
answer, and the one this would take with more runway. Rejected on three grounds: an ALB
is roughly $16.20 per month in base charges before LCUs, which is 16 percent of the
$100 ceiling for 19 days of use; a public ACM certificate requires a validated domain,
so it adds a Route 53 hosted zone, DNS records, and a validation wait to the critical
path; and it introduces target groups, health checks and listener rules as new
first-time-ever integrations on exactly the days the schedule has no slack.

**A per-instance reverse proxy with a self-signed certificate.** Rejected because it
does not close the harm. A self-signed certificate provides no authentication, so it
does not defend against an active attacker, and it puts a browser interstitial in the
middle of the graded screenshot of the working prototype.

**Caddy with Let's Encrypt on a `rockcyber.com` subdomain.** Genuinely closes it and is
free. Rejected on schedule: it needs DNS records for three hosts, ingress on 80 and 443
opened to the world for the ACME HTTP challenge, and a renewal path — four to six hours
on the critical path, against a residual risk that the structural control already
removes.

## Residual risk, stated plainly

Comment text submitted to `/predict` and rendered by the user UI crosses the internet in
cleartext, and so does the aggregate content of the monitoring dashboard. A network
observer between the grader and the instance can read submitted comments and predicted
probabilities, and can tamper with them in flight.

This is acceptable here for reasons that are specific and would not transfer to a real
moderation service. The data is public-dataset-derived text typed by the operator or a
grader during a demonstration window. No credential, session cookie, or personal
identifier transits these listeners. The endpoints are ingress-restricted to the
operator address by default, with `demo_cidrs` opening them only while someone is
looking. The exposure window is measured in hours, and the entire stack is destroyed
after grading.

## Compensating controls in force

1. Reviewer UI on 8503 with no ingress rule on any security group. Enforced by
   `tests/infra/test_security_groups.py::test_reviewer_ui_port_8503_has_no_ingress_anywhere`.
2. `demo_cidrs` defaults to `[]`. Opening it is a deliberate variable change, and
   closing it again is on the post-demo checklist.
3. **Rotate the reviewer shared secret after the demo window closes**, and again before
   submission:
   ```bash
   aws secretsmanager put-secret-value --secret-id toxic-mod/reviewer-shared-secret \
     --secret-string "$(openssl rand -base64 32)"
   ```
4. `/predict` carries an input-size cap and a rate limit from Phase 2, so a cleartext
   endpoint is not also an unmetered one.
5. IMDSv2 required on every instance, so an SSRF through a cleartext listener cannot
   become credential theft.

## Re-open this decision if

- the endpoints are exposed beyond a supervised demo window, or `demo_cidrs` is left
  open overnight;
- any authenticated action, session cookie, or API key moves onto a public listener;
- real user traffic reaches `/predict` from anyone other than the operator or a grader;
- the project outlives the assignment.

Any one of those makes an ALB with ACM the right answer, and the Terraform to add it is
one file.

## Disclosure

Stated in `MODEL_CARD.md` under limitations, and in `README.md` beside the live URL and
its availability window.
```

Correct the drifted claim in the delivery spec. Section 4's table row for EC2 #2 currently reads "Thin client; calls the backend over **HTTPS** and reads RDS":

```bash
python3 - <<'PY'
from pathlib import Path
p = Path("docs/superpowers/specs/2026-07-30-delivery-plan-design.md")
text = p.read_text()
old = "Thin client; calls the backend over **HTTPS** and reads RDS"
new = ("Thin client; calls the backend over **cleartext HTTP** and reads RDS. "
       "No TLS terminator by decision, see `docs/tls-decision.md` (premortem H15)")
assert old in text, "the HTTPS claim is already gone; check the spec by hand"
p.write_text(text.replace(old, new))
print("delivery spec corrected")
PY
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/infra/test_docs_controls.py tests/infra/test_security_groups.py -v`
Expected: 38 PASS in `test_docs_controls.py` and 22 PASS in `test_security_groups.py`.

- [ ] **Step 5: Commit**

```bash
git add docs/tls-decision.md docs/superpowers/specs/2026-07-30-delivery-plan-design.md tests/infra/test_docs_controls.py
git commit -m "Record the no-TLS decision and take the reviewer interface off the internet"
```

---

### Task 18: `outputs.tf` — the seam Phase 5 consumes

Everything Phase 5's `deploy.yml`, `docker-compose.yml` and the runbooks read comes from here. Nothing downstream may hardcode an address, an ARN, or a log group name.

**Files:**
- Create: `infra/terraform/outputs.tf`
- Test: `tests/infra/test_outputs.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_outputs.py`:
```python
"""The Phase 5 seam. Adding or renaming an output here is a contract change."""

from tests.infra import tfparse

EXPECTED = {
    "backend_url",
    "frontend_url",
    "monitoring_url",
    "instance_ids",
    "ssm_target_tag",
    "ecr_repository_urls",
    "log_group_names",
    "db_endpoint",
    "db_host",
    "db_name",
    "db_master_secret_arn",
    "db_readonly_secret_arn",
    "db_bootstrap_document",
    "gha_deploy_role_arn",
    "alerts_topic_arn",
}


def test_outputs_match_the_published_interface_exactly():
    assert set(tfparse.outputs()) == EXPECTED


def test_public_urls_are_built_from_the_elastic_ips_not_the_ephemeral_ones():
    outputs = tfparse.outputs()
    assert outputs["backend_url"]["value"] == "http://${aws_eip.backend.public_ip}:8000"
    assert outputs["frontend_url"]["value"] == "http://${aws_eip.frontend.public_ip}:8501"
    assert outputs["monitoring_url"]["value"] == "http://${aws_eip.monitoring.public_ip}:8502"


def test_the_reviewer_ui_url_is_not_published():
    # Publishing it would invite someone to try it over the internet, where it is
    # deliberately unreachable. The runbook documents the port-forward instead.
    assert not any("8503" in str(body.get("value")) for body in tfparse.outputs().values())


def test_ssm_target_tag_names_the_tag_the_instances_actually_carry():
    assert tfparse.outputs()["ssm_target_tag"]["value"] == "Component"


def test_no_output_exposes_a_secret_value():
    for name, body in tfparse.outputs().items():
        value = str(body.get("value", ""))
        assert "secret_string" not in value, name
        assert "master_user_secret[0].secret_string" not in value, name


def test_every_output_carries_a_description():
    for name, body in tfparse.outputs().items():
        assert body.get("description"), f"{name} has no description"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_outputs.py -v`
Expected: FAIL with `AssertionError: assert set() == {'alerts_topic_arn', 'backend_url', ...}`

- [ ] **Step 3: Write minimal implementation**

`infra/terraform/outputs.tf`:
```hcl
output "backend_url" {
  description = "FastAPI base URL on the stable Elastic IP. /predict and /health hang off this."
  value       = "http://${aws_eip.backend.public_ip}:8000"
}

output "frontend_url" {
  description = "Streamlit user interface. The reviewer interface on 8503 is NOT published; see docs/tls-decision.md."
  value       = "http://${aws_eip.frontend.public_ip}:8501"
}

output "monitoring_url" {
  description = "Monitoring dashboard on EC2 #3, which rubric 3.2 requires to be a different server."
  value       = "http://${aws_eip.monitoring.public_ip}:8502"
}

output "instance_ids" {
  description = "Instance ids by tier, for SSM send-command and the no-SSH runbook."
  value = {
    backend    = aws_instance.backend.id
    frontend   = aws_instance.frontend.id
    monitoring = aws_instance.monitoring.id
  }
}

output "ssm_target_tag" {
  description = "Tag key that deploy.yml selects instances on. The value is the tier name."
  value       = "Component"
}

output "ecr_repository_urls" {
  description = "Push targets for the four component images, keyed by component."
  value       = { for name, repo in aws_ecr_repository.app : name => repo.repository_url }
}

output "log_group_names" {
  description = "awslogs driver targets, keyed by component. Phase 5 compose reads these."
  value       = { for name, group in aws_cloudwatch_log_group.app : name => group.name }
}

output "db_endpoint" {
  description = "Postgres endpoint including the port."
  value       = aws_db_instance.main.endpoint
}

output "db_host" {
  description = "Postgres hostname without the port, for psql --host and PGHOST."
  value       = aws_db_instance.main.address
}

output "db_name" {
  description = "Initial database name."
  value       = aws_db_instance.main.db_name
}

output "db_master_secret_arn" {
  description = "RDS-managed master credentials. Readable by the backend and frontend roles only."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "db_readonly_secret_arn" {
  description = "monitor_ro credentials container. Seeded by CLI; readable by the monitoring role only."
  value       = aws_secretsmanager_secret.db_readonly.arn
}

output "db_bootstrap_document" {
  description = "SSM document that creates or rotates the SELECT-only monitor_ro role."
  value       = aws_ssm_document.db_bootstrap_readonly.name
}

output "gha_deploy_role_arn" {
  description = "Set this as the GitHub repository variable AWS_DEPLOY_ROLE_ARN. Not a secret, but it carries the account id, so it is a variable rather than a committed literal."
  value       = aws_iam_role.gha_deploy.arn
}

output "alerts_topic_arn" {
  description = "SNS topic carrying budget alerts, health alarms and root-usage events."
  value       = aws_sns_topic.alerts.arn
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make tf-fmt && terraform -chdir=infra/terraform validate && .venv/bin/pytest tests/infra/test_outputs.py -v`
Expected: `Success! The configuration is valid.` and 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/outputs.tf tests/infra/test_outputs.py
git commit -m "Publish the Terraform outputs Phase 5 deployment consumes"
```

---

### Task 19 (H36, C7, H4): Workflow guards — no plan in CI, no apply on a docs commit

**Finding H36:** `terraform plan` on pull requests is code execution on attacker-supplied `.tf` — providers, `data "external"`, module sources. It is not currently reachable by fork pull requests, but `pull_request_target` and `workflow_run` both reintroduce it, and the rubric does not ask for a plan step at all. Dropped entirely, which also removes the need for any AWS identity in pull-request CI.

**Finding C7, second clause:** `deploy.yml` must not apply unattended on a documentation commit. Two guards: `terraform apply` is not in any workflow, and `paths-ignore` excludes `docs/**` and `**.md` so a README fix does not trigger a deployment at all.

**Finding H4, end to end:** the `verify-oidc` step proves the trust policy accepts this exact workflow, at this exact ref, in the production environment — and, by construction, nothing else.

**Files:**
- Create: `.github/workflows/terraform-ci.yml`, `.github/workflows/deploy.yml`
- Test: `tests/infra/test_workflow_guards.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_workflow_guards.py`:
```python
"""H36 no plan in CI; C7 no apply and no docs-triggered deploy; H35 pinned actions."""

import re
from pathlib import Path

import pytest
import yaml

from tests.infra import tfparse

WORKFLOWS = Path(tfparse.ROOT) / ".github" / "workflows"
CI = WORKFLOWS / "terraform-ci.yml"
DEPLOY = WORKFLOWS / "deploy.yml"


def _all_workflows() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def _load(path: Path) -> dict:
    # PyYAML parses the bare `on:` key as the boolean True.
    return yaml.safe_load(path.read_text())


def _triggers(doc: dict) -> dict:
    return doc.get("on", doc.get(True, {}))


def test_the_two_workflows_exist():
    assert CI.exists() and DEPLOY.exists()


@pytest.mark.parametrize("workflow", _all_workflows() or [CI])
def test_no_workflow_anywhere_runs_terraform_plan_or_apply(workflow):
    text = workflow.read_text()
    assert "terraform plan" not in text, f"{workflow.name}: H36"
    assert "terraform apply" not in text, f"{workflow.name}: C7"
    assert "pull_request_target" not in text, f"{workflow.name}: reintroduces H36"


@pytest.mark.parametrize("workflow", _all_workflows() or [CI])
def test_every_action_reference_is_pinned_to_a_commit_sha(workflow):
    for ref in re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", workflow.read_text(), re.M):
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref), f"{workflow.name}: {ref} is not SHA-pinned"


def test_ci_runs_on_pull_requests_to_main():
    assert _triggers(_load(CI))["pull_request"]["branches"] == ["main"]


def test_ci_holds_no_aws_identity_at_all():
    doc = _load(CI)
    assert doc["permissions"] == {"contents": "read"}
    for job in doc["jobs"].values():
        assert "id-token" not in job.get("permissions", {})
    text = CI.read_text()
    assert "configure-aws-credentials" not in text
    assert "assume-role-with-web-identity" not in text


def test_ci_validates_offline_and_scans():
    text = CI.read_text()
    assert "init -backend=false" in text
    assert "terraform" in text and "validate" in text
    assert "checkov" in text
    assert "tests/infra" in text


def test_deploy_ignores_documentation_only_commits():
    ignored = _triggers(_load(DEPLOY))["push"]["paths-ignore"]
    assert "docs/**" in ignored
    assert "**.md" in ignored


def test_deploy_is_gated_by_the_production_environment_on_main_only():
    doc = _load(DEPLOY)
    assert _triggers(doc)["push"]["branches"] == ["main"]
    job = doc["jobs"]["roll"]
    assert job["environment"] == "production"
    assert job["permissions"]["id-token"] == "write"


def test_deploy_reads_the_role_arn_from_a_repository_variable_not_a_literal():
    text = DEPLOY.read_text()
    assert "vars.AWS_DEPLOY_ROLE_ARN" in text
    assert not re.search(r"arn:aws:iam::\d{12}:role", text), "account id must not be committed"


def test_deploy_masks_the_session_credentials_it_exports():
    assert "::add-mask::" in DEPLOY.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_workflow_guards.py -v`
Expected: `AssertionError: assert False` on `test_the_two_workflows_exist`, and `FileNotFoundError` on the rest.

- [ ] **Step 3: Write minimal implementation**

`.github/workflows/terraform-ci.yml`:
```yaml
name: terraform-ci

on:
  pull_request:
    branches: [main]
    paths:
      - 'infra/**'
      - 'tests/infra/**'
      - 'requirements/infra.txt'
      - '.github/workflows/terraform-ci.yml'

# This workflow deliberately holds NO AWS identity. `terraform plan` is code
# execution on attacker-supplied .tf (providers, data "external", module
# sources) and the rubric does not ask for a plan step (premortem H36).
# Everything below runs offline.
permissions:
  contents: read

concurrency:
  group: terraform-ci-${{ github.ref }}
  cancel-in-progress: true

env:
  TERRAFORM_VERSION: 1.15.8

jobs:
  static:
    runs-on: ubuntu-24.04-arm
    steps:
      - uses: actions/checkout@v5

      - name: Install Terraform, checksum-verified
        run: |
          set -euo pipefail
          workdir="$(mktemp -d)"
          cd "$workdir"
          curl -fsSL -O "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_arm64.zip"
          curl -fsSL -O "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_SHA256SUMS"
          grep linux_arm64 "terraform_${TERRAFORM_VERSION}_SHA256SUMS" | sha256sum -c -
          unzip -q "terraform_${TERRAFORM_VERSION}_linux_arm64.zip"
          sudo install -m 0755 terraform /usr/local/bin/terraform
          terraform version

      - name: Format check and offline validate
        run: |
          set -euo pipefail
          for dir in infra/terraform infra/smoke; do
            terraform -chdir="$dir" fmt -check -recursive
            terraform -chdir="$dir" init -backend=false -input=false
            terraform -chdir="$dir" validate
          done

      - name: Static infrastructure assertions
        run: |
          set -euo pipefail
          python3 -m venv .venv-ci
          .venv-ci/bin/python -m pip install --quiet -r requirements/infra.txt pytest==8.3.3
          .venv-ci/bin/pytest tests/infra -m "not integration and not awsapply" -q

      - name: checkov
        run: |
          set -euo pipefail
          python3 -m venv .venv-checkov
          .venv-checkov/bin/python -m pip install --quiet checkov==3.3.8
          .venv-checkov/bin/checkov --directory infra --framework terraform --compact --quiet
```

`.github/workflows/deploy.yml`:
```yaml
name: deploy

# `terraform apply` is deliberately absent (premortem C7 and H4). Infrastructure
# changes are applied by the operator from an IAM Identity Center session, so no
# CI principal needs iam:*, and a republished AMI can never replace three
# instances unattended. paths-ignore means a README fix cannot trigger a
# deployment at all.
on:
  workflow_dispatch:
  push:
    branches: [main]
    paths-ignore:
      - 'docs/**'
      - '**.md'
      - 'LICENSE'
      - '.gitignore'

permissions:
  contents: read

concurrency:
  group: deploy
  cancel-in-progress: false

jobs:
  roll:
    runs-on: ubuntu-24.04-arm
    environment: production
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v5

      - name: Assume gha-deploy through OIDC
        env:
          ROLE_ARN: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
        run: |
          set -euo pipefail
          token=$(curl -fsS \
            -H "Authorization: bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \
            "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=sts.amazonaws.com" | jq -r .value)
          creds=$(aws sts assume-role-with-web-identity \
            --role-arn "$ROLE_ARN" \
            --role-session-name "gha-deploy-${GITHUB_RUN_ID}" \
            --web-identity-token "$token" \
            --duration-seconds 3600)
          key=$(printf '%s' "$creds" | jq -r .Credentials.AccessKeyId)
          secret=$(printf '%s' "$creds" | jq -r .Credentials.SecretAccessKey)
          session=$(printf '%s' "$creds" | jq -r .Credentials.SessionToken)
          echo "::add-mask::$secret"
          echo "::add-mask::$session"
          {
            echo "AWS_ACCESS_KEY_ID=$key"
            echo "AWS_SECRET_ACCESS_KEY=$secret"
            echo "AWS_SESSION_TOKEN=$session"
            echo "AWS_DEFAULT_REGION=us-west-2"
          } >> "$GITHUB_ENV"

      - name: Prove the trust policy accepted this workflow, ref and environment
        run: aws sts get-caller-identity

      # Phase 5 appends here: build four arm64 images on this native runner, tag
      # them sha-${{ github.sha }}, push to the four ECR repositories, then roll
      # containers with `aws ssm send-command` and poll GetCommandInvocation to a
      # terminal state, asserting the invocation count equals the expected
      # instance count before reporting success.
```

Pin both `uses:` references to commit SHAs:
```bash
SHA=$(gh api repos/actions/checkout/commits/v5 --jq .sha)
sed -i "s|uses: actions/checkout@v5|uses: actions/checkout@${SHA} # v5|" \
  .github/workflows/terraform-ci.yml .github/workflows/deploy.yml
grep -n 'uses:' .github/workflows/*.yml
```

Set the repository variable so the deploy job has a role to assume:
```bash
gh variable set AWS_DEPLOY_ROLE_ARN \
  --body "$(terraform -chdir=infra/terraform output -raw gha_deploy_role_arn)"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/infra/test_workflow_guards.py -v`
Expected: 14 PASS.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/terraform-ci.yml .github/workflows/deploy.yml tests/infra/test_workflow_guards.py
git commit -m "Add offline Terraform CI and a deploy workflow that never applies or fires on docs"
```

---

### Task 20: The checkov gate, with every suppression carrying a written rationale

A scanner whose findings are suppressed silently is worse than no scanner, because it produces documented-looking assurance for a property nobody checked — the same failure shape the premortem found in `assert_no_leakage`. The rule here: a check may be skipped, but only if a human wrote down why, and a test enforces that.

The skip list is derived from the **actual** first run, not from memory. Run the scan, read the findings, fix what is genuinely wrong, and suppress the rest with a reason.

**Files:**
- Create: `.checkov.yml`, `docs/checkov-suppressions.md`
- Modify: `infra/terraform/observability.tf`, `Makefile`
- Test: `tests/infra/test_checkov_suppressions.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_checkov_suppressions.py`:
```python
"""Every suppressed scanner finding must carry a written, dated rationale."""

import re
from pathlib import Path

import yaml

from tests.infra import tfparse

CONFIG = Path(tfparse.ROOT) / ".checkov.yml"
RATIONALES = Path(tfparse.ROOT) / "docs" / "checkov-suppressions.md"


def test_a_checkov_config_exists_and_scans_the_whole_infra_tree():
    config = yaml.safe_load(CONFIG.read_text())
    assert config["directory"] == ["infra"]
    assert config["framework"] == ["terraform"]
    # Never soft-fail. The gate must be able to fail.
    assert config.get("soft-fail", False) is False


def test_every_skipped_check_has_a_rationale_line():
    skipped = set(yaml.safe_load(CONFIG.read_text()).get("skip-check", []) or [])
    documented = set(re.findall(r"^\|\s*(CKV2?_[A-Z]+_\d+)\s*\|", RATIONALES.read_text(), re.M))
    assert skipped <= documented, f"undocumented suppressions: {sorted(skipped - documented)}"


def test_no_rationale_is_left_as_a_placeholder():
    for row in RATIONALES.read_text().splitlines():
        if row.startswith("|") and re.search(r"CKV2?_[A-Z]+_\d+", row):
            cells = [cell.strip() for cell in row.strip("|").split("|")]
            assert len(cells) >= 3, row
            assert len(cells[2]) > 30, f"rationale too thin to be a decision: {row}"
            assert "TODO" not in row


def test_suppressions_are_not_used_to_hide_the_findings_this_phase_owns():
    # These are the checks that corroborate premortem findings. Suppressing any of
    # them would silently reopen the finding the phase exists to close.
    skipped = set(yaml.safe_load(CONFIG.read_text()).get("skip-check", []) or [])
    protected = {
        "CKV_AWS_79",   # IMDSv1 must not be enabled
        "CKV_AWS_17",   # RDS must not be publicly accessible
        "CKV_AWS_16",   # RDS storage encrypted
        "CKV_AWS_133",  # RDS backup retention, corroborates H6
        "CKV_AWS_36",   # CloudTrail log file validation
        "CKV_AWS_51",   # ECR immutable tags
        "CKV_AWS_163",  # ECR scan on push
        "CKV2_AWS_6",   # S3 public access block
    }
    assert not (skipped & protected), f"protected checks suppressed: {sorted(skipped & protected)}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/infra/test_checkov_suppressions.py -v`

Expected: 4 failures, all `FileNotFoundError: [Errno 2] No such file or directory: '.../.checkov.yml'`

- [ ] **Step 3: Run the scanner for real, then write the config from what it says**

```bash
make tf-scan 2>&1 | tee /tmp/checkov-first-run.txt
grep -E '^Check: CKV' /tmp/checkov-first-run.txt | sort -u
```

Fix what is genuinely wrong before suppressing anything. One finding is a free fix and should be taken rather than skipped — SNS server-side encryption with the AWS-managed key costs nothing, so amend `aws_sns_topic.alerts` in `infra/terraform/observability.tf`:

```hcl
resource "aws_sns_topic" "alerts" {
  name              = "${var.project}-alerts"
  kms_master_key_id = "alias/aws/sns"
}
```

Then write the config. The list below is the **expected shape**; replace it with the check ids the run actually reported, and give each one a row in the rationale table.

`.checkov.yml`:
```yaml
directory:
  - infra
framework:
  - terraform
compact: true
quiet: true
soft-fail: false
skip-check:
  - CKV_AWS_88    # EC2 with a public IP
  - CKV_AWS_126   # EC2 detailed monitoring
  - CKV_AWS_157   # RDS Multi-AZ
  - CKV_AWS_293   # RDS deletion protection
  - CKV_AWS_353   # RDS Performance Insights
  - CKV_AWS_118   # RDS enhanced monitoring
  - CKV_AWS_161   # RDS IAM database authentication
  - CKV_AWS_145   # S3 encrypted with a customer-managed key
  - CKV_AWS_18    # S3 access logging on the trail bucket
  - CKV_AWS_144   # S3 cross-region replication
  - CKV_AWS_136   # ECR encrypted with a customer-managed key
  - CKV_AWS_158   # CloudWatch log group encrypted with a customer-managed key
  - CKV_AWS_338   # CloudWatch log group retained for a year
  - CKV_AWS_35    # CloudTrail encrypted with a customer-managed key
```

`docs/checkov-suppressions.md`:
```markdown
# checkov suppressions

Every entry in `.checkov.yml` `skip-check` appears here with a reason. A suppression
without a rationale is enforced as a test failure by
`tests/infra/test_checkov_suppressions.py`, because a scanner whose findings are
silently skipped produces documented-looking assurance for a property nobody checked.

Reviewed 2026-07-31 against the first real scan of `infra/`.

| Check | What it wants | Why this project does not do it |
|---|---|---|
| CKV_AWS_88 | No public IP on EC2 | There is no NAT gateway, by design: one would cost roughly a third of the $100 monthly ceiling. Instances sit in public subnets behind a per-tier ingress allowlist with no port 22 and IMDSv2 required. Recorded as an accepted trade in the AWS foundation spec section 11. |
| CKV_AWS_126 | EC2 detailed monitoring | One-minute metrics are a paid feature. The StatusCheckFailed alarm runs on the free basic metrics, which is sufficient for a stack that is deliberately stopped every night. |
| CKV_AWS_157 | RDS Multi-AZ | Multi-AZ doubles the RDS cost for a class project whose database is destroyed after grading. Single-AZ with a seven-day backup retention and a final snapshot is the deliberate trade, and the final snapshot is what protects the graded dataset. |
| CKV_AWS_293 | RDS deletion protection | `terraform destroy` is cost control #2 in the cost model, and deletion protection blocks it. Protection of the data comes from `skip_final_snapshot = false` with a per-lifecycle unique identifier, not from blocking teardown. |
| CKV_AWS_353 | RDS Performance Insights | Paid beyond the free retention tier, and the workload is a few thousand rows with two aggregations. Query performance is not a risk this project carries. |
| CKV_AWS_118 | RDS enhanced monitoring | Per-instance charge for OS-level metrics on a `db.t4g.micro` that is stopped nightly. The CloudWatch defaults answer every question this project asks. |
| CKV_AWS_161 | RDS IAM database authentication | The application connects with a Secrets Manager password. IAM auth would require token refresh logic in the backend, the frontend and the dashboard, which is real work against no reduction in this project's threat model, since no static database credential exists on any box in the first place. |
| CKV_AWS_145 | S3 encrypted with a customer-managed key | A customer-managed KMS key carries a monthly charge per key and buys key-policy control and independent rotation. Correct for regulated data, unwarranted for a CloudTrail bucket in a sandbox account holding no personal data. AES256 with SSE-S3 is in force. |
| CKV_AWS_18 | S3 access logging | Access logging on the trail bucket needs a second bucket, which needs its own logging by the same rule. The account has CloudTrail and GuardDuty; a second bucket is cost and complexity without a matching risk. |
| CKV_AWS_144 | S3 cross-region replication | The service control policy region-locks this account, so replication has almost nowhere to go, and the trail already carries a 90-day lifecycle. Durability of a nineteen-day audit trail in a sandbox account does not warrant a second bucket and its transfer cost. |
| CKV_AWS_136 | ECR encrypted with a customer-managed key | Same reasoning as CKV_AWS_145. Images are built from a public base image and hold no secret; `WANDB_API_KEY` reaches builds through BuildKit secret mounts and never enters a layer. |
| CKV_AWS_158 | CloudWatch log group encrypted with a customer-managed key | Same reasoning. Log groups hold application logs that are explicitly forbidden from containing raw comment text. |
| CKV_AWS_338 | CloudWatch log group retained for a year | Fourteen days by deliberate choice: default retention is forever and log storage is a silent recurring cost. The project's life is nineteen days. |
| CKV_AWS_35 | CloudTrail encrypted with a customer-managed key | Same reasoning as CKV_AWS_145. Log file validation is enabled, which is the control that matters for tamper evidence. |
```

Add a Makefile reporting target:
```makefile
tf-scan-report:
	$(CHECKOV_VENV)/bin/checkov --config-file .checkov.yml --output json > /tmp/checkov.json || true
	python3 -c "import json;d=json.load(open('/tmp/checkov.json'));print('failed:',len(d['results']['failed_checks']))"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make tf-scan && .venv/bin/pytest tests/infra/test_checkov_suppressions.py -v`

Expected: checkov exits 0 reporting no failed checks, and 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add .checkov.yml docs/checkov-suppressions.md infra/terraform/observability.tf Makefile tests/infra/test_checkov_suppressions.py
git commit -m "Gate the infrastructure on checkov with a documented rationale per suppression"
```

---

### Task 21: Real apply, plan-diff assertions, real destroy, and the PR

Static assertions prove what the source says. This task proves what AWS receives. The plan-diff assertions run against `terraform show -json`, which renders every interpolation, every `for_each`, and every IAM policy document into the literal JSON the API will see — the only place the H4 trust policy can be checked as IAM will actually evaluate it.

**Files:**
- Create: `tests/infra/test_plan_assertions.py`, `docs/evidence/a2-apply-destroy.md`
- Modify: `Makefile`, `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`, `docs/2026-07-01-toxic-moderation-mlops-design.md`, `docs/HANDOFF.md`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_plan_assertions.py`:
```python
"""Assertions against the rendered plan: what AWS receives, not what the HCL says.

Marked `awsapply` because generating plan.json needs a real AWS session. Excluded
from pull-request CI by `-m "not integration and not awsapply"`, which is what keeps
premortem H36 closed. Run it on the operator machine:

    make tf-plan-json
    .venv/bin/pytest tests/infra/test_plan_assertions.py -m awsapply -v
"""

import json
from pathlib import Path

import pytest

from tests.infra import tfparse

PLAN = Path(tfparse.ROOT) / "plan.json"

pytestmark = pytest.mark.awsapply


@pytest.fixture(scope="module")
def resources() -> list[dict]:
    if not PLAN.exists():
        pytest.fail("plan.json missing; run `make tf-plan-json` first")
    return json.loads(PLAN.read_text())["planned_values"]["root_module"]["resources"]


def _of_type(resources: list[dict], kind: str) -> dict[str, dict]:
    return {r["name"]: r["values"] for r in resources if r["type"] == kind}


def test_the_plan_creates_exactly_three_instances_with_the_sized_classes(resources):
    instances = _of_type(resources, "aws_instance")
    assert {name: body["instance_type"] for name, body in instances.items()} == {
        "backend": "t4g.medium",
        "frontend": "t4g.small",
        "monitoring": "t4g.medium",
    }


def test_the_plan_creates_three_elastic_ips(resources):
    assert len(_of_type(resources, "aws_eip")) == 3


def test_no_rendered_security_group_rule_reaches_port_22(resources):
    for name, body in _of_type(resources, "aws_security_group").items():
        for direction in ("ingress", "egress"):
            for rule in body.get(direction) or []:
                assert not (rule["from_port"] <= 22 <= rule["to_port"]), f"{name} {direction}"


def test_every_application_group_has_rendered_egress(resources):
    groups = _of_type(resources, "aws_security_group")
    for name in ("backend", "frontend", "monitoring"):
        egress = groups[name].get("egress") or []
        assert egress, f"sg-{name} would be created with no egress at all"
        assert any(r["from_port"] == 443 for r in egress)
        assert any(r["from_port"] == 53 and r["protocol"] == "udp" for r in egress)
        assert any(r["from_port"] == 123 for r in egress)


def test_the_rendered_database_takes_a_final_snapshot(resources):
    db = _of_type(resources, "aws_db_instance")["main"]
    assert db["skip_final_snapshot"] is False
    assert db["final_snapshot_identifier"], "an empty identifier fails the destroy"
    assert db["backup_retention_period"] >= 1
    assert db["publicly_accessible"] is False


def test_the_rendered_oidc_trust_policy_ands_rather_than_ors(resources):
    # The definitive H4 check. The static test asserts on the HCL; this asserts on
    # the JSON document IAM will actually evaluate.
    policy = json.loads(_of_type(resources, "aws_iam_role")["gha_deploy"]["assume_role_policy"])
    conditions = policy["Statement"][0]["Condition"]
    assert set(conditions) == {"StringEquals"}, conditions
    equals = conditions["StringEquals"]
    assert "token.actions.githubusercontent.com:job_workflow_ref" in equals
    for key, value in equals.items():
        assert isinstance(value, str) or len(value) == 1, (
            f"{key} is multi-valued; IAM evaluates that as OR"
        )


def test_the_rendered_deploy_policy_carries_no_iam_or_apply_rights(resources):
    policy = json.loads(_of_type(resources, "aws_iam_role_policy")["gha_deploy"]["policy"])
    allowed = {
        action
        for statement in policy["Statement"]
        if statement["Effect"] == "Allow"
        for action in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    }
    assert not any(a.startswith(("iam:", "organizations:", "rds:")) for a in allowed)


def test_the_rendered_monitoring_policy_omits_the_master_secret(resources):
    policy = json.loads(_of_type(resources, "aws_iam_role_policy")["monitoring"]["policy"])
    text = json.dumps(policy)
    assert "db-readonly" in text
    assert "rds!" not in text, "the RDS-managed master secret name begins with rds!"


def test_the_nightly_stop_schedules_are_enabled(resources):
    schedules = _of_type(resources, "aws_scheduler_schedule")
    assert set(schedules) == {"nightly_stop_ec2", "nightly_stop_rds"}
    for name, body in schedules.items():
        assert body["state"] == "ENABLED", name
```

- [ ] **Step 2: Run test to verify it fails**

Add to the `Makefile`:
```makefile
tf-plan-json:
	terraform -chdir=infra/terraform plan -input=false -out=/tmp/a2.tfplan
	terraform -chdir=infra/terraform show -json /tmp/a2.tfplan > plan.json
	@echo "wrote plan.json"
```

Run: `.venv/bin/pytest tests/infra/test_plan_assertions.py -m awsapply -v`

Expected: 9 failures, each `Failed: plan.json missing; run 'make tf-plan-json' first`

- [ ] **Step 3: Apply for real, then run the assertions against the rendered plan**

```bash
export AWS_PROFILE=mlops-admin AWS_REGION=us-west-2

cp infra/terraform/backend.hcl.example infra/terraform/backend.hcl
# fill in the bucket name from infra/aws/bootstrap-outputs.env
terraform -chdir=infra/terraform init -backend-config=backend.hcl -input=false

cat > infra/terraform/terraform.tfvars <<'EOF'
alert_email = "rock@rockcyber.com"
EOF
printf 'operator_cidrs = ["%s/32"]\n' "$(curl -fsS https://checkip.amazonaws.com)" \
  >> infra/terraform/terraform.tfvars

make tf-plan-json
.venv/bin/pytest tests/infra/test_plan_assertions.py -m awsapply -v

terraform -chdir=infra/terraform apply -input=false /tmp/a2.tfplan
```

Expected: 9 PASS, then `Apply complete!` with the fifteen outputs printed.

Verify the live stack, pasting every output into `docs/evidence/a2-apply-destroy.md`:

```bash
# H2: three instances, three tiers, three addresses. Rubric 5.2 and 3.2 evidence.
aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=toxic-mod" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].[Tags[?Key==`Component`]|[0].Value,InstanceType,PublicIpAddress]' \
  --output table

# C6: all three must register. This is the single most informative check in the phase,
# because registration is only possible if 443, DNS and NTP egress all work.
aws ssm describe-instance-information \
  --query 'InstanceInformationList[].[InstanceId,PingStatus]' --output table

# No group opens 22, in either direction.
aws ec2 describe-security-groups --filters "Name=tag:Project,Values=toxic-mod" \
  --query 'SecurityGroups[?IpPermissions[?FromPort==`22`]||IpPermissionsEgress[?FromPort==`22`]].GroupName' \
  --output text

# H6: private, backed up, destroyable.
aws rds describe-db-instances --db-instance-identifier toxic-mod-pg \
  --query 'DBInstances[0].[PubliclyAccessible,BackupRetentionPeriod,DeletionProtection]' --output text

# H7: the nightly stop schedules exist and are enabled.
aws scheduler list-schedules --query 'Schedules[].[Name,State]' --output table

# H27: the alarms exist.
aws cloudwatch describe-alarms --alarm-name-prefix toxic-mod \
  --query 'MetricAlarms[].[AlarmName,StateValue]' --output table

# Drift: an immediate second plan must be empty. Exit code 0 means no changes;
# exit code 2 means something drifts on every apply and the AMI pin or the
# final-snapshot identifier is not holding still.
terraform -chdir=infra/terraform plan -input=false -detailed-exitcode
```

Expected: the SG query prints nothing, the RDS query prints `False 7 False`, both schedules report `ENABLED`, and `plan -detailed-exitcode` exits `0`.

- [ ] **Step 4: Prove the read-only role cannot write (H16, end to end)**

Create the role using the procedure in `docs/runbooks/no-ssh-debug.md` §8, then:

```bash
aws ssm start-session \
  --target "$(terraform -chdir=infra/terraform output -json instance_ids | jq -r .monitoring)"
```

On the instance, with `$DB_HOST` from `/etc/toxic-mod.env` and the `monitor_ro` password from Secrets Manager:

```bash
psql "postgresql://monitor_ro:$RO_PASS@$DB_HOST:5432/toxicmod" -c "SELECT 1;"
psql "postgresql://monitor_ro:$RO_PASS@$DB_HOST:5432/toxicmod" -c "CREATE TABLE probe(x int);"
```

Expected: the first returns `1`; the second fails with `ERROR: permission denied for schema public`. Paste both into the evidence file. A successful `CREATE TABLE` means H16 is unfixed.

- [ ] **Step 5: Destroy for real, and prove the teardown is clean**

```bash
terraform -chdir=infra/terraform destroy -input=false
aws rds describe-db-snapshots --snapshot-type manual \
  --query 'DBSnapshots[].[DBSnapshotIdentifier,Status]' --output table
aws ec2 describe-addresses --query 'Addresses[].PublicIp' --output text
aws ecr describe-repositories --query 'repositories[].repositoryName' --output text
```

Expected: `Destroy complete!`, exactly one manual snapshot named `toxic-mod-final-<timestamp>` in state `available`, no Elastic IPs, and no repositories. A destroy that errors on `aws_db_instance` is H6 recurring; an Elastic IP left behind is the one resource that keeps billing after teardown.

- [ ] **Step 6: Correct the documents this phase drifted from**

Three statements elsewhere still describe a two-instance topology. The premortem's Tier 0.2 remediation is explicit that these are edited at source rather than covered by a supersession table, because a supersession table does not survive a subagent reading a narrow slice.

```bash
python3 - <<'PY'
from pathlib import Path

edits = [
    (
        "docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md",
        "security groups with no port 22, both EC2 instances with IMDSv2 required",
        "per-tier security groups with explicit 443, DNS and NTP egress and no port 22, "
        "**three** EC2 instances (backend `t4g.medium`, frontend `t4g.small`, monitoring "
        "`t4g.medium`) with IMDSv2 required at hop limit 2, an Elastic IP per instance",
    ),
    (
        "docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md",
        "## Phase 5: Docker, two-EC2 deploy, README, model card, AIBOM",
        "## Phase 5: Docker, three-EC2 deploy, README, model card, AIBOM",
    ),
    (
        "docs/2026-07-01-toxic-moderation-mlops-design.md",
        "EC2 #1 runs the FastAPI backend and the Streamlit UI. EC2 #2 runs the "
        "monitoring dashboard and the DistilBERT re-scorer worker.",
        "EC2 #1 runs the FastAPI backend. EC2 #2 runs the Streamlit user and reviewer "
        "UI. EC2 #3 runs the monitoring dashboard and, if it survives the cut-line, "
        "the DistilBERT re-scorer worker.",
    ),
]

for path, old, new in edits:
    p = Path(path)
    text = p.read_text()
    assert old in text, f"already corrected, or drifted further: {path}"
    p.write_text(text.replace(old, new))
    print(f"corrected {path}")
PY
```

Then append this block to the master plan's **Interface Contracts** section, immediately after the "W&B artifact naming" paragraph. It is a genuine cross-phase contract this phase produces and Phase 5 consumes, and the section is declared authoritative for exactly that kind of seam.

````markdown
**Infrastructure outputs (Phase A2 → Phase 5).** `terraform -chdir=infra/terraform output -json`
is the only source for addresses, ARNs and names. Nothing downstream hardcodes them.

```
backend_url   frontend_url   monitoring_url    instance_ids{backend,frontend,monitoring}
ssm_target_tag = "Component"                   ecr_repository_urls{4}   log_group_names{4}
db_endpoint   db_host   db_name                db_master_secret_arn   db_readonly_secret_arn
db_bootstrap_document                          gha_deploy_role_arn    alerts_topic_arn
```

`ecr_repository_urls` and `log_group_names` are key-identical maps over
`backend, frontend, monitoring, rescorer`. The reviewer UI on port 8503 is deliberately
absent from the outputs: it has no ingress rule on any security group and is reached over
`aws ssm start-session --document-name AWS-StartPortForwardingSession`. See
`docs/tls-decision.md`.
````

Update `docs/HANDOFF.md`: Stage C is complete, the day-9 smoke deploy passed, the resume command is `terraform -chdir=infra/terraform apply`, and the stack is currently destroyed with the final snapshot retained.

- [ ] **Step 7: Full suite, lint, and the PR**

```bash
make lint
make tf-fmt
make tf-validate
make tf-scan
make infra-test
make test
git add -A
git commit -m "Record the Phase A2 apply and destroy evidence and correct the three-instance topology at source"
git push -u origin feat/phase-a2-terraform
```

```bash
gh pr create --base main --title "Phase A2: Terraform infrastructure" --body "Three tier-separated EC2 instances with per-tier security groups, per-tier instance profiles and stable Elastic IPs. Private RDS Postgres 16 with retained backups, a per-lifecycle unique final snapshot, and a SELECT-only role for the monitoring dashboard. Four ECR repositories. One GitHub OIDC deploy role whose trust conditions AND rather than OR, and which cannot run terraform apply. CloudTrail with log file validation, GuardDuty, per-component log groups behind the awslogs driver, and two health alarms. A \$100 budget and a nightly stop schedule as a hard duration control. Rebuilt cost model, no-SSH debug runbook, and a recorded TLS decision. The day-9 throwaway smoke deploy passed and a real apply/destroy cycle completed cleanly. Pull-request CI validates Terraform offline and holds no AWS identity."
```

Expected: `ruff` clean, `terraform validate` clean for both root modules, checkov exits 0, the infra suite green, and the pull request opened with the `terraform-ci` check running.

---

## Self-Review

### Premortem coverage

Every finding assigned to this phase has an owning task whose test fails if the finding is unfixed.

| Finding | Owning task | The test that fails if it regresses |
|---|---|---|
| **C6** — no egress specified anywhere; Terraform deletes the default allow-all; no SSH to recover through | 5, 6, 2 | `test_security_groups.py::test_every_app_group_declares_an_egress_block_at_all`, `::test_every_app_group_can_reach_443_dns_and_ntp`, `test_docs_controls.py::test_runbook_names_every_surviving_channel`, `test_plan_assertions.py::test_every_application_group_has_rendered_egress` |
| **C7** — unattended apply against an auto-resolving AMI | 3, 13, 19 | `test_ami_pin.py::test_no_ssm_parameter_data_source_exists_in_the_root_module`, `test_compute.py::test_each_instance_pins_the_ami_and_ignores_drift_on_it`, `test_workflow_guards.py::test_no_workflow_anywhere_runs_terraform_plan_or_apply`, `::test_deploy_ignores_documentation_only_commits` |
| **H2** — the Terraform scope of record still specified two instances | 13, 21 | `test_compute.py::test_there_are_exactly_three_instances_one_per_tier`, `test_plan_assertions.py::test_the_plan_creates_exactly_three_instances_with_the_sized_classes` |
| **H4** — a multi-valued `sub` is evaluated as OR; `gha-deploy` is de-facto admin | 12 | `test_oidc.py::test_every_trust_condition_is_single_valued`, `::test_trust_pins_aud_sub_and_job_workflow_ref`, `::test_deploy_role_cannot_run_terraform_apply`, `test_plan_assertions.py::test_the_rendered_oidc_trust_policy_ands_rather_than_ors` |
| **H6** — destroy fails, or destroy deletes the graded dataset | 9 | `test_data.py::test_final_snapshot_is_taken_not_skipped`, `::test_final_snapshot_identifier_is_unique_per_database_lifecycle`, `::test_backups_are_retained_for_at_least_one_day` |
| **H7** — the cost model omits eleven line items; no hard duration control | 15, 16 | `test_budget.py::test_a_nightly_stop_schedule_exists_for_ec2_and_for_rds`, `::test_the_schedule_is_on_by_default_and_disableable_for_the_demo`, `test_docs_controls.py::test_cost_model_prices_every_previously_omitted_line_item` (13 parametrised cases) |
| **H15** — no TLS anywhere while the spec claims HTTPS | 17, 5 | `test_docs_controls.py::test_tls_decision_closes_the_named_harm_structurally`, `::test_delivery_spec_no_longer_claims_the_frontend_uses_https`, `test_security_groups.py::test_reviewer_ui_port_8503_has_no_ingress_anywhere` |
| **H16** — one security group, one instance role, one database user across three tiers | 5, 10, 11 | `test_security_groups.py::test_each_tier_listens_on_its_own_port_only`, `test_readonly_role.py::test_only_select_is_granted`, `test_iam.py::test_monitoring_cannot_read_the_rds_master_secret`, and the live `CREATE TABLE` refusal in Task 21 step 4 |
| **H27** — no container logs leave the box; no health alarm | 8, 13, 14 | `test_observability.py::test_one_log_group_per_component_at_fourteen_day_retention`, `test_compute.py::test_user_data_configures_the_awslogs_driver_against_the_terraform_log_group`, `test_health_alarm.py::test_a_metric_filter_counts_503_responses_in_the_backend_log_group` |
| **H36** — `terraform plan` on pull requests is code execution on attacker-supplied `.tf` | 12, 19 | `test_oidc.py::test_there_is_no_gha_ci_role`, `test_workflow_guards.py::test_no_workflow_anywhere_runs_terraform_plan_or_apply`, `::test_ci_holds_no_aws_identity_at_all` |
| Elastic IP per public-facing instance | 13 | `test_compute.py::test_each_instance_has_a_stable_elastic_ip`, `test_plan_assertions.py::test_the_plan_creates_three_elastic_ips` |
| IMDSv2 with the hop-limit-2 tradeoff documented | 13, 6 | `test_compute.py::test_imdsv2_is_required_with_hop_limit_two`, `test_docs_controls.py::test_runbook_documents_the_imdsv2_hop_limit_tradeoff` |
| `map_public_ip_on_launch` on public subnets | 4, 2 | `test_network.py::test_public_subnets_map_a_public_ip_on_launch`, `test_smoke_module.py::test_smoke_subnet_maps_a_public_ip_on_launch` |
| Day-9 throwaway single-instance smoke deploy | 2 | `test_smoke_module.py` (6 cases) and `test_smoke_health_server.py` (2 cases), plus the recorded checkpoint evidence in `docs/evidence/a2-smoke-deploy.md` |

Findings **touched but not owned**, implemented here because the file is in scope, and named as supporting evidence rather than as closure: H5 (the poll-to-terminal-state loop appears in the smoke procedure and in the deploy skeleton's comment), H12 (the reviewer interface is unreachable from the internet), H17 (log file validation, versioning and public-access blocking on the trail bucket; the tamper denies remain the service control policy's job), H29 (the nightly stop means the seven-day RDS timer never expires, and the final snapshot makes "destroy rather than stop" survivable), H35 (SHA-pinned actions and checksum-verified binary downloads in both workflows and in user data).

### Spec coverage

Foundation spec §7.1 network, including the explicit-egress and pinned-AMI paragraphs, is Tasks 3, 4 and 5. §7.2 compute and data, as amended to three instances, is Tasks 9 and 13. §7.3 IAM and OIDC, with `thumbprint_list` deliberately omitted, is Tasks 11 and 12. §7.4 secrets — containers only, no values in state — is Task 9. §7.5 observability is Tasks 8 and 14. §5.3 budget is Task 15. Delivery spec §4 runtime topology is Task 13; §5 the live-URL problem and Elastic IPs is Task 13; §6.3's "RDS private, security groups scoped to the instances, least-privilege instance profiles, no static keys" is Tasks 5, 9 and 11; §7's day 9 and days 10–11 rows are Tasks 2 and 21; §8's end-of-day-11 checkpoint is Task 2's gate. Rubric 3.2 "a different EC2 server", 5.1 one container per component, and 5.2 "separate EC2 instances" are Task 13 plus the four repositories in Task 7.

Four deliberate deviations, each recorded at the point of change rather than in a supersession table:

1. **No `gha-ci` role.** H36 removes `terraform plan` from pull-request CI, which was the only thing that role did. No AWS identity reachable from a pull request is strictly stronger than a narrowly scoped one.
2. **No `terraform apply` in GitHub Actions.** This is what removes `iam:*` from the deploy role, and it closes the second half of H4 at the root instead of by scoping. Apply is an operator action, which the delivery spec's day 10–11 schedule already assumed.
3. **ECR retention of 30 rather than the spec's 10.** Ten tags is under two days of commits on this schedule, and the premortem folded ECR retention into the rollback remediation.
4. **`docs/tls-decision.md` replaces the delivery spec's HTTPS claim**, and the spec text is corrected at source in Task 17.

### Placeholder scan

Every step carries real code and an exact command. There is no `TODO`, no "handle edge cases", no "similar to", and no invented identifier.

Three values cannot be known at authoring time. Each is produced by a literal command in the step that needs it, and each is guarded by a test that rejects an unsubstituted value: the AMI id comes from `aws ssm get-parameter` and `test_ami_is_pinned_in_a_committed_tfvars_file` requires a literal `ami-…` with a resolution date; the `actions/checkout` reference comes from `gh api repos/actions/checkout/commits/v5 --jq .sha` and `test_every_action_reference_is_pinned_to_a_commit_sha` requires forty hex characters; the state bucket comes from `infra/aws/bootstrap-outputs.env` and lives in a gitignored `backend.hcl`. The checkov skip list is explicitly labelled as the expected shape to be replaced by the ids the first real scan reports, with `test_every_skipped_check_has_a_rationale_line` and `test_no_rationale_is_left_as_a_placeholder` enforcing that whatever ends up there is justified in prose.

The parser the entire test harness rests on was verified by execution against `python-hcl2` 8.1.2 before this plan was written — the quoting of block labels and string literals, the `__is_block__` marker, the shape of nested `condition`, `dynamic` and `metadata_options` blocks, the rendering of `for_each` and `formatdate` expressions, and the top-level shape of `locals` and `data` — so the normaliser in `tfparse.py` and every assertion written against it match observed behaviour rather than assumed behaviour.

### Type consistency

`local.components` is `["backend", "frontend", "monitoring", "rescorer"]` and is the single `for_each` source for both `aws_ecr_repository.app` and `aws_cloudwatch_log_group.app`, so the two maps always carry identical keys, and `ecr_repository_urls` and `log_group_names` are therefore key-identical in the output contract.

The three instance tiers are `backend`, `frontend`, `monitoring`, and each of those strings is simultaneously the Terraform resource name, the `Component` tag value, the security group name suffix, the IAM role name suffix, the instance profile name suffix, the `for_each` key into the log group and repository maps, and the `instance_ids` map key. One identifier, seven uses — which is why `test_iam.py` and `test_compute.py` can be parametrised over a single list, and why a rename that misses one site fails a test rather than surfacing at deploy time.

`ssm_target_tag` is the literal `"Component"` and matches the tag `aws_instance` actually carries, asserted by `test_compute.py::test_each_instance_carries_the_tags_ssm_send_command_selects_on`. The `gha-deploy` policy's `ssm:resourceTag/Project` condition matches the `Project` tag the provider's `default_tags` applies plus the explicit `Project` tag on each instance, so `SendCommand` resolves rather than silently matching zero targets.

`var.ami_id` is the sole AMI source for all four instances across both root modules. `db_host` is `aws_db_instance.main.address` (no port) and `db_endpoint` is `.endpoint` (with port); the runbook's `psql --host` and the user-data `TOXIC_MOD_DB_HOST` use the former, and any DSN uses the latter. `db_name` is `"toxicmod"` in exactly one place, `aws_db_instance.main.db_name`, and is read from there by the SSM document parameter, the user-data environment file, and the output.

The output set is exactly what `test_outputs.py::test_outputs_match_the_published_interface_exactly` enforces. It is a superset of the Interfaces Produced block at the top of this plan by two names, `db_host` and `db_bootstrap_document`, both required by the read-only-role procedure in the no-SSH runbook §8; the Interfaces Produced block and the master plan's Interface Contracts addition in Task 21 step 6 both list the full fifteen.

## Execution Handoff

Two options:

1. **Subagent-Driven (recommended):** a fresh subagent per task, review between tasks. REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.
2. **Inline Execution:** in-session with checkpoints. REQUIRED SUB-SKILL: `superpowers:executing-plans`.

Task 2 is the day-9 checkpoint from the delivery spec schedule and it gates everything after it. If it has not succeeded by the end of day 11, the pre-committed fallback fires: provision EC2 and RDS by console in the member account, and submit this Terraform as evidence for rubric 5.2 rather than as the provisioning path. Tasks 1 and 2 are therefore the ones to run first and the ones not to defer.
