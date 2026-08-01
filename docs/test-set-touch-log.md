# Held-out test-set touch log

The held-out test set is evaluated **once** per `split_version`, on the single model that
cross-validation already chose. It never *chooses* between candidate models: picking the better
of two test numbers is selection on the test set and biases the winner upward.

This file is the guard. It is tracked in git, so it survives an ephemeral RunPod pod, a fresh
interpreter, and a fresh clone. A second entry for the same `split_version` is refused by
`model.evaluate.record_touch`.

## `a24b8dd61fd539f6fae25c3955d4d8faf9036e854f3e492ae075dd522be31b8c` — run `dnvoc420`

| field | value |
|---|---|
| split_version | `a24b8dd61fd539f6fae25c3955d4d8faf9036e854f3e492ae075dd522be31b8c` |
| git_sha | `e8f5c8c1db9fecc738e9396af473015cd0bf24b4` |
| run_id | `dnvoc420` |
| touched_at_utc | `2026-08-01T06:25:04+00:00` |
| macro_f1 | 0.5991 |
| macro_pr_auc | 0.6632 |
| accuracy | 0.9763 |

Accuracy is recorded because rubric 1.2 and 3.2 name it. It is never a promotion or
comparison metric; promotion is decided on `macro_f1`.

```json
{
  "accuracy": 0.9763152115945667,
  "accuracy/identity_hate": 0.9813658750823477,
  "accuracy/insult": 0.9711076952034382,
  "accuracy/obscene": 0.975499576497161,
  "accuracy/severe_toxic": 0.9807698340496283,
  "accuracy/threat": 0.9944160366408382,
  "accuracy/toxic": 0.9547322520939863,
  "f1/identity_hate": 0.45901639344262296,
  "f1/insult": 0.7266251113089938,
  "f1/obscene": 0.7857338820301784,
  "f1/severe_toxic": 0.4222431668237512,
  "f1/threat": 0.42948717948717946,
  "f1/toxic": 0.7712791250594389,
  "macro_f1": 0.5990641430253608,
  "macro_pr_auc": 0.6632080021207208,
  "pr_auc/identity_hate": 0.5442376151678221,
  "pr_auc/insult": 0.7976514294402879,
  "pr_auc/obscene": 0.8744778768476168,
  "pr_auc/severe_toxic": 0.4359487239600119,
  "pr_auc/threat": 0.4658958067068902,
  "pr_auc/toxic": 0.8610365606016956,
  "precision/identity_hate": 0.32101910828025476,
  "precision/insult": 0.7157894736842105,
  "precision/obscene": 0.7665952890792291,
  "precision/severe_toxic": 0.28903225806451616,
  "precision/threat": 0.3145539906103286,
  "precision/toxic": 0.7665406427221172,
  "recall/identity_hate": 0.805111821086262,
  "recall/insult": 0.7377938517179023,
  "recall/obscene": 0.8058525604952167,
  "recall/severe_toxic": 0.7832167832167832,
  "recall/threat": 0.6767676767676768,
  "recall/toxic": 0.7760765550239235,
  "subset_accuracy": 0.9000847005678075,
  "support/identity_hate": 313.0,
  "support/insult": 1659.0,
  "support/obscene": 1777.0,
  "support/severe_toxic": 286.0,
  "support/threat": 99.0,
  "support/toxic": 3135.0
}
```

