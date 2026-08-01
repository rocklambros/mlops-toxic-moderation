# ---------------------------------------------------------------------------
# Remote state, in the S3 bucket Phase A1's bootstrap.sh created.
#
# PARTIAL CONFIGURATION, ON PURPOSE. `bucket` is deliberately absent from this
# file. The bucket is named rockcyber-mlops-toxic-tfstate-<ACCOUNT_ID> and this
# repository is public, so committing the name here would publish the member
# account id -- the same reason .gitignore excludes infra/aws/bootstrap-outputs.env.
# The name is supplied at init time from that gitignored file:
#
#   set -a; . infra/aws/bootstrap-outputs.env; set +a
#   terraform -chdir=infra/terraform init -input=false \
#     -backend-config="bucket=$TF_STATE_BUCKET"
#
# or, equivalently, from a gitignored infra/terraform/backend.hcl holding one
# line -- `bucket = "rockcyber-mlops-toxic-tfstate-<ACCOUNT_ID>"` -- and
#
#   terraform -chdir=infra/terraform init -input=false -backend-config=backend.hcl
#
# A partial backend is not optional hygiene here: `terraform init` with no
# -backend-config and no bucket prompts interactively, which is a hang rather
# than an error in any non-interactive context.
#
# `use_lockfile = true` is S3 native state locking, generally available since
# Terraform 1.11 (see the floor in versions.tf). The deprecated `dynamodb_table`
# argument is deliberately absent: this account has no lock table, by design, and
# naming one that does not exist fails every apply.
#
# `region` is a literal because a backend block cannot reference variables or
# locals. It names where the *state bucket* lives, which is a fixed fact
# established by Phase A1 and independent of var.region, so a literal is correct
# rather than a compromise. Both are us-west-2 today; if the workload ever moved
# to us-east-1 -- the only other region the Sandbox OU service control policy
# permits -- this line would stay as it is.
# ---------------------------------------------------------------------------

terraform {
  backend "s3" {
    key          = "phase-a2/terraform.tfstate"
    region       = "us-west-2"
    use_lockfile = true
    encrypt      = true
  }
}
