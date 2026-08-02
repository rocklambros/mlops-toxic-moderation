# Grading window: the nightly stop is OFF.
#
# `nightly_stop_enabled` defaults to true and that default is correct -- the SCP's
# instance-type allowlist caps the hourly rate and says nothing about duration, so three
# allowlisted instances plus RDS left running reach the $100 ceiling inside a month without
# a single policy violation (premortem H7, scenario C). This file does not disagree with
# that reasoning. It records a deliberate, bounded exception to it.
#
# Why it exists as a committed file rather than a flag on one apply: the schedules were
# first disabled out of band with `aws scheduler update-schedule`, which left the live
# state at DISABLED and the code at ENABLED. `terraform plan` on 2026-08-02 duly reported
# "state = DISABLED -> ENABLED" on both schedules, meaning the very next apply -- the deploy
# workflow, `make aws-up`, anyone reconciling drift -- would have re-armed nightly shutdown
# without anyone deciding to. A graded stack would then have been down every night from
# 23:00, and the failure would have looked like an outage rather than a config revert.
# `-var nightly_stop_enabled=false` has the same hole: it protects only the applies that
# remember it. An `*.auto.tfvars` file is loaded by every apply automatically, so code and
# reality agree by construction.
#
# The project is due 2026-08-18 and the grading date is not known in advance, so the window
# cannot be closed on a schedule. Restoring the control is deleting this file:
#
#     rm infra/terraform/grading.auto.tfvars && terraform apply
#
# tests/unit/test_grading_window.py fails once RESTORE_AFTER has passed, so the exception
# cannot outlive the reason for it by being forgotten.
#
# RESTORE_AFTER: 2026-09-15

nightly_stop_enabled = false
