# Amazon Linux 2023 arm64 (al2023-ami-2023.12.20260727.0-kernel-6.1-arm64),
# resolved 2026-07-31 from
# /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 in us-west-2.
#
# Pinned deliberately: see premortem C7 and the comment on var.ami_id. Never wire
# this to an aws_ssm_parameter data source -- an AL2023 republication would force
# replacement of all three instances at an arbitrary moment. Re-resolve and bump
# it on purpose, then `terraform apply -replace=aws_instance.<tier>` one instance
# at a time, per docs/runbooks/no-ssh-debug.md.
ami_id = "ami-0159f3bbb387b05a7"
