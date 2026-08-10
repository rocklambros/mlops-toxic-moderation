# Public demo window: the three graded listeners are open to the internet.
#
# `demo_cidrs` defaults to [] and that default is correct. `operator_cidrs` is validated to
# reject anything wider than a /24 precisely so that opening the listeners to the world
# cannot happen by widening the allowlist -- it has to happen here, in a separate variable,
# visibly and revertibly. This file is that deliberate act.
#
# Why it exists as a committed file rather than a flag on one apply: the same reason
# grading.auto.tfvars does. `-var demo_cidrs='["0.0.0.0/0"]'` protects only the applies that
# remember it. The deploy workflow, `make aws-up`, and anyone reconciling drift would each
# re-close the listeners without deciding to, and the failure mode is silent -- no error
# anywhere, just a grader getting a connection timeout on a URL the README says is live.
# An `*.auto.tfvars` file is loaded by every apply automatically, so code and reality agree
# by construction.
#
# What this costs, stated plainly: the three listeners serve cleartext HTTP, so submitted
# comment text and predicted probabilities cross the internet readable and tamperable by
# anyone on the path. docs/tls-decision.md is the decision record, and it was re-opened and
# re-accepted on 2026-08-10 specifically because this window is open-ended rather than the
# few supervised hours its original acceptance assumed. Read that file before widening
# anything further.
#
# What is NOT opened: port 8503, the reviewer console. It has no ingress rule on any
# security group -- `toxic-mod-reviewer` is an empty group by design -- and is reached only
# through an SSM port-forward, which the service encrypts end to end. That is the structural
# control the whole cleartext acceptance rests on, and this file does not touch it.
#
# Closing the window is deleting this file:
#
#     rm infra/terraform/demo.auto.tfvars && terraform apply
#
# There is no scheduled close date: the operator closes it on request after grading.
# RESTORE_AFTER is a backstop, not a schedule. It does not shut anything down -- it turns
# tests/unit/test_demo_window.py red, so an open-ended exposure cannot quietly become a
# permanent one by being forgotten.
#
# RESTORE_AFTER: 2026-09-15

demo_cidrs = ["0.0.0.0/0"]
