# Model card: `toxic-clf` v1.0.0

Multi-label toxicity classifier for English Wikipedia-talk-style comments, six labels, served
as a triage signal in front of a human review queue.

Every number below was produced by code in this repository and can be re-derived from the
committed artifacts. Where a number could not be measured, this card says so rather than
rounding an estimate into a claim.

| | |
|---|---|
| **Artifact digest** | `sha256:db678467907743fbce5d25ab8c9ad56cd0c89e053b46be81822dcb2095842454` |
| **Registry (public, logged-out)** | <https://wandb.ai/rockcyber-org/wandb-registry-model/artifacts/model/toxic-clf> |
| **Promoted stage** | `production` (collection `toxic-clf`, version `v0`) |
| **Training / evaluation run** | <https://wandb.ai/rockcyber/mlops-toxic-moderation/runs/dnvoc420> |
| **Public project** | <https://wandb.ai/rockcyber/mlops-toxic-moderation> |
| **Held-out headline** | macro-F1 **0.5991** [0.5880, 0.6109] · macro PR-AUC **0.6632** [0.6471, 0.6853] |

---

## 1. Model details

| field | value |
|---|---|
| Name | `toxic-clf` |
| Version | v1.0.0 (W&B artifact `toxic-clf:v0`, alias `production`) |
| Architecture | `Pipeline(FeatureUnion(word TF-IDF, char_wb TF-IDF) → OneVsRest(CalibratedClassifierCV(LogisticRegression)))` |
| Task | multi-label binary classification, six independent labels |
| Labels, positional order | `toxic`, `severe_toxic`, `obscene`, `threat`, `insult`, `identity_hate` |
| Output | six calibrated probabilities in `[0, 1]`, one per label, plus six boolean flags at the per-label thresholds in §4 |
| Serialization | `skops` 0.13.0 (never pickle, never joblib) |
| Artifact size | 400,229,552 bytes (400 MB) |
| Trained | 2026-07-31, single fit on all 180,633 training rows, 34.4 min wall clock, 4.49 GB peak RSS |
| Seed | 42, with `PYTHONHASHSEED=0` asserted at process start |
| Code version | git `e8f5c8c1db9fecc738e9396af473015cd0bf24b4`, branch `feat/phase-1-train-register` |
| Maintainer | `rock@rockcyber.com` — see [`SECURITY.md`](SECURITY.md) for the disclosure path |
| Licence of the model artifact | inherits the CC0 licence of the underlying Jigsaw corpus for the data-derived parameters; see §6 |

### Hyperparameters

| group | value |
|---|---|
| word vectorizer | `TfidfVectorizer(ngram_range=(1,2), min_df=2, sublinear_tf=True, strip_accents="unicode", max_features=200_000)` |
| char vectorizer | `TfidfVectorizer(analyzer="char_wb", ngram_range=(3,5), min_df=3, sublinear_tf=True, strip_accents="unicode", max_features=100_000)` |
| classifier | `LogisticRegression(solver="liblinear", C=1.0, class_weight="balanced", max_iter=2000, random_state=42)` |
| calibration | `CalibratedClassifierCV(method="sigmoid", cv=5)` |
| realized vocabulary | 200,000 word features + 100,000 char features = 300,000; **both caps are binding**, so the feature space is truncated by the cap and not by the corpus |
| inner fits | 6 labels × 5 calibration folds = 30 logistic regressions in the final model; 150 across the 5-fold cross-validation |
| convergence | every one of the 150 inner fits converged; worst `n_iter_` = 10 against a cap of 2000 |

Both `max_features` caps binding is a real limitation, not a tuning detail: the model is
operating at a deliberately imposed capacity ceiling chosen to keep the artifact loadable on a
4 GB serving host, and raising it is the most obvious untried improvement.

### Serving footprint

Measured in a fresh interpreter against the serialized artifact, on the aarch64 build box
(8-core Cortex-A78AE @ 2.2 GHz). The production host is a 4 GB `t4g.medium` (Graviton2), which
is architecturally similar but not identical, so these are indicative rather than contractual.

| | |
|---|---|
| cold load | 51.0 s |
| resident set after load | 0.625 GB (delta over interpreter + imports: 0.441 GB) |
| single-request latency | p50 25.2 ms · p90 27.6 ms · p95 28.6 ms · p99 32.2 ms · max 39.3 ms (n = 400) |
| batch total | 1 req 23.4 ms · 8 req 32.1 ms · 32 req 51.5 ms · 128 req 138.8 ms |

The 51-second cold load is a deployment constraint: a rolling restart must not treat the
process as unhealthy inside that window.

---

## 2. Intended use and out-of-scope use

### In scope

- Scoring free-text English comments of the shape found in Wikipedia talk pages — user-to-user
  discussion, typically under a few hundred words — to **rank a human moderation queue**.
- Producing a per-label probability that a downstream service records alongside the comment id
  for drift monitoring and for later re-scoring.
- Acting as the first stage of a two-stage pipeline in which every flagged comment is seen by a
  person before any moderation action is taken.

### Explicitly out of scope

- **Automated removal, blocking, muting, or banning without human review.** At the operating
  thresholds in §4 the model's precision on `severe_toxic` (0.289), `threat` (0.315) and
  `identity_hate` (0.321) means roughly two of every three flags on those labels are wrong.
  Automating on those columns would be a majority-error action.
- **Any language other than English.** The corpus is English; nothing here measures behaviour
  on other languages and the char n-gram features will produce confident-looking scores anyway.
- **Any domain other than public discussion comments** — not chat logs, not code review, not
  clinical notes, not customer support tickets, not search queries.
- **Judging a person.** The unit of prediction is a comment. Aggregating scores into a per-user
  reputation was not evaluated and would compound the identity-term bias documented in §7.
- **Legal, employment, credit, immigration, or law-enforcement decisions**, or any use where
  the output contributes to an adverse action against an individual.
- **Adversarial or safety-critical filtering.** The model is a linear model whose exact
  coefficients are public (§8); evasion is an offline optimisation with zero queries.
- **Long documents.** Nothing was evaluated above the corpus's comment-length distribution.

---

## 3. Factors and subgroups

Performance is reported disaggregated along the factors below. Every factor named here appears
with disaggregated metrics in §4 or §7.

| factor | why it is expected to matter |
|---|---|
| **Label** | Prevalence spans 0.31 % (`threat`) to 9.8 % (`toxic`). Aggregate numbers hide a 0.35 spread in per-label F1. |
| **Identity-term mention** | Jigsaw's best-documented failure mode is over-flagging comments that merely *mention* an identity group. 57 descriptor terms are sliced in §7. |
| **Cross-validation fold** | Five folds, to distinguish a real effect from split luck. |
| **Comment length / character composition** | The char n-gram half of the feature union is length- and obfuscation-sensitive. **Not measured.** Named here as a known gap. |
| **Time** | The corpus is a fixed 2017-era snapshot. Behaviour on 2026 traffic is unmeasured; §8 names the monitoring that substitutes for it. |

**Identity is inferred, not observed.** The six-label Jigsaw corpus carries no demographic
annotations. Every "subgroup" in §7 is a *term-presence slice* — comments containing a listed
descriptor — which is a noisy proxy: it captures who is *talked about*, not who is speaking,
and it misses every mention that uses no listed term. Slices overlap and do not partition the
test set.

---

## 4. Metrics and 95 % confidence intervals

### How the numbers were produced

- **The model was chosen by 5-fold cross-validation on the training split only.** The held-out
  test set was scored **once**, on the already-chosen model, through a durable ledger
  ([`docs/test-set-touch-log.md`](docs/test-set-touch-log.md)) that refuses a second entry for
  the same `split_version`. The test set never chose anything.
- **Intervals are stratified percentile bootstraps, 1000 replicates, seed 42.** Per-label
  intervals resample the positive and negative strata separately so no replicate can lose every
  positive of a 99-positive label and score 0.0 on a warning. Aggregate intervals resample
  within exact *label-pattern* strata, which preserves every label's positive count at once
  while keeping rows intact, so the between-label correlations survive the resample.
- **Accuracy is reported because the rubric names it, and is banned from selection.** A
  predictor that always says "not toxic" scores 0.962 mean per-label accuracy on this corpus
  while catching nothing (§5, baseline row). `model.evaluate.select_best_run` raises if asked to
  rank on an accuracy-flavoured key. Promotion is decided on `macro_f1`.

### Operating thresholds

Tuned on the 180,633 out-of-fold predictions, never on the test set. Rare, high-harm labels are
tuned on F-beta with beta > 1, which buys recall with precision deliberately.

| label | threshold | beta | rationale |
|---|---|---|---|
| `toxic` | 0.31 | 1 | balanced |
| `severe_toxic` | 0.05 | 3 | missing it is worse than over-flagging it |
| `obscene` | 0.25 | 1 | balanced |
| `threat` | 0.05 | 5 | highest real-world harm, lowest prevalence |
| `insult` | 0.28 | 1 | balanced |
| `identity_hate` | 0.05 | 3 | targeted harm |

### Held-out test set — 31,877 comments, evaluated once

| metric | value | 95 % CI |
|---|---|---|
| **macro F1** (promotion metric) | **0.5991** | [0.5880, 0.6109] |
| **macro PR-AUC** | **0.6632** | [0.6471, 0.6853] |
| mean per-label accuracy | 0.9763 | [0.9757, 0.9770] |
| subset (exact-match) accuracy | 0.9001 | [0.8978, 0.9024] |

### Held-out, per label

| label | support | F1 | F1 95 % CI | PR-AUC | PR-AUC 95 % CI | precision | recall | accuracy |
|---|---|---|---|---|---|---|---|---|
| `toxic` | 3135 | 0.7713 | [0.7599, 0.7819] | 0.8610 | [0.8510, 0.8705] | 0.7665 | 0.7761 | 0.9547 |
| `obscene` | 1777 | 0.7857 | [0.7720, 0.7985] | 0.8745 | [0.8624, 0.8863] | 0.7666 | 0.8059 | 0.9755 |
| `insult` | 1659 | 0.7266 | [0.7110, 0.7418] | 0.7977 | [0.7799, 0.8148] | 0.7158 | 0.7378 | 0.9711 |
| `identity_hate` | 313 | 0.4590 | [0.4327, 0.4883] | 0.5442 | [0.4914, 0.6052] | 0.3210 | 0.8051 | 0.9814 |
| `severe_toxic` | 286 | 0.4222 | [0.3951, 0.4485] | 0.4359 | [0.3828, 0.4965] | 0.2890 | 0.7832 | 0.9808 |
| `threat` | 99 | 0.4295 | [0.3730, 0.4851] | 0.4659 | [0.3773, 0.5663] | 0.3146 | 0.6768 | 0.9944 |

The three rare labels are the weak half of this model and the intervals say so: `threat`'s
PR-AUC interval spans 0.19 and rests on 99 positives. Read every rare-label number against its
support column.

### Cross-validated (out-of-fold, 180,633 rows) — the numbers the promotion decision used

| metric | OOF value | 95 % CI | across-fold mean | across-fold sd |
|---|---|---|---|---|
| macro F1 | 0.6038 | [0.5989, 0.6084] | 0.6039 | 0.0045 |
| macro PR-AUC | 0.6656 | [0.6575, 0.6752] | 0.6675 | 0.0086 |
| mean per-label accuracy | 0.9763 | [0.9760, 0.9765] | 0.9763 | 0.0005 |
| subset accuracy | 0.9002 | [0.8992, 0.9011] | 0.9002 | 0.0014 |

**Generalization gap: none detectable.** Held-out macro F1 0.5991 [0.5880, 0.6109] against
out-of-fold 0.6038 [0.5989, 0.6084] — the intervals overlap and the point estimates differ by
0.005, roughly one across-fold standard deviation. The out-of-fold estimate was not optimistic.

---

## 5. Comparisons

### Prior-only baseline (rubric 1.1)

`OneVsRest(DummyClassifier(strategy="prior"))`, scored on the same five folds. It predicts the
training prevalence of each label for every comment and ignores the text entirely.

| model | macro F1 | macro PR-AUC | mean per-label accuracy | subset accuracy |
|---|---|---|---|---|
| prior-only baseline | **0.0000** | 0.0380 | **0.9620** | 0.8968 |
| `toxic-clf` (OOF) | **0.6038** | 0.6656 | 0.9763 | 0.9002 |

This is the accuracy trap made concrete: the baseline catches **nothing** and still scores 96.2 %
accuracy, 1.4 points behind the real model. Any report that led with accuracy would describe
these two systems as nearly equivalent.

### DistilBERT — trained, measured, deliberately not promoted

A `distilbert-base-uncased` fine-tune was trained on the same split (3 epochs,
`problem_type="multi_label_classification"`, seed 42, max length 192) as a build-time rigor
check.

| | validation macro F1 | validation macro PR-AUC | train/val loss gap by epoch |
|---|---|---|---|
| DistilBERT, epoch 3 | 0.6744 | 0.7268 | +0.0021 → +0.0109 → +0.0216 |

DistilBERT scores higher on validation. It is **not** the promoted model and it was **not**
evaluated on the held-out test set, for three reasons recorded before the numbers were seen:

1. Choosing between two candidates on test numbers is selection on the test set and biases the
   winner upward. The held-out set measures the chosen model; it does not choose.
2. The delivery spec fixes the promoted collection as the classical model, whose 0.63 GB
   resident set and 25 ms p50 fit the 4 GB serving host that this project actually deploys.
   DistilBERT does not, without a quantized export.
3. That quantized export **failed its int8-vs-float parity gate**, so there is no verified
   deployable DistilBERT artifact. The float checkpoint and both ONNX graphs are retained as
   versioned W&B artifacts with digests; neither is aliased to any promoted stage.

The validation-set gap is a real signal that a transformer would do better on the rare labels,
and it is the strongest argument in this card for a future version. It is not a claim about
held-out performance, because no such measurement exists and none will be made on this split.

---

## 6. Data

### Provenance

| field | value |
|---|---|
| Underlying corpus | Jigsaw / Conversation AI **Toxic Comment Classification Challenge** — English Wikipedia talk-page comments |
| Mirror actually downloaded | Kaggle `julian3833/jigsaw-multilingual-toxic-comment-classification`, member `jigsaw-toxic-comment-train.csv`, fetched 2026-07-31 (39,078,413 bytes zipped). This mirror republishes the English six-label data as the original training split **concatenated with the labelled competition test split**. |
| Licence | the original Jigsaw release is a CC0 1.0 public-domain dedication. **The mirror's own licence terms were not independently verified**; treat redistribution as governed by Kaggle's dataset page, not by this card. |
| Collection window | English Wikipedia talk-page archive; corpus published 2017–2018 |
| Raw file | `data/raw/jigsaw-toxic-comment-train.csv`, 95,538,001 bytes, **223,549 rows**, schema `id, comment_text, toxic, severe_toxic, obscene, threat, insult, identity_hate` |
| **`raw_sha256`** | `2acea3b6f0641a19c6c972e49c0a6dadddeca16aff3f0d5042a123a82221d898` |
| **`split_version`** | `a24b8dd61fd539f6fae25c3955d4d8faf9036e854f3e492ae075dd522be31b8c` |
| **`env_version`** | `fa2aba19ecb298d7d3bddb41bc900bdfd606dfcf572af72b08ad67de2c7c06f8` |
| composite `data_version` | `414a45a29f73bed6632dd966e13c64d7196cfd9d5799e8a76a9ecbe40e9294c3` |

Three provenance fields are recorded, never one composite, so that a moved number can be
attributed: did the corpus change, did the split change, or did the environment change? A single
opaque hash answers none of those.

The **223,549** row count is worth stating plainly because most write-ups of this dataset quote
159,571 — that is the original training split alone. This mirror includes the labelled test
split, so every count in this card is larger than the familiar ones and is not comparable
row-for-row with published Jigsaw leaderboard numbers.

### Preparation

| step | result |
|---|---|
| Near-duplicate removal | MinHash-LSH blocking + exact Jaccard decision at **J ≥ 0.70** on word shingles → **212,510** rows retained (11,039 removed, 4.9 %) |
| Split | 85 / 15 via `MultilabelStratifiedShuffleSplit(test_size=0.15, random_state=42)` → **train 180,633** / **test 31,877** |
| Folds | 5 via `MultilabelStratifiedKFold(shuffle=True, random_state=42)`, on the training split only |
| Label-hierarchy invariant | `severe_toxic == 1 ⟹ toxic == 1` holds on the raw file (0 violations), and is asserted at load |
| Leakage firewall | an executable gate, structurally independent of the deduplicator (different LSH banding — (17, 4) versus dedup's (25, 5), and an exact-Jaccard decision rather than a band collision), asserts no train/test pair exceeds J = 0.70. Verified in situ: `test.csv.gz` was absent from the training pod's filesystem. |

Deduplicating *before* splitting is the load-bearing choice. Jigsaw contains near-identical
comments; splitting first would place copies of the same comment on both sides and inflate every
test number.

### Label prevalence, training split

| label | prevalence | positives |
|---|---|---|
| `toxic` | 9.83 % | 17,764 |
| `obscene` | 5.57 % | 10,067 |
| `insult` | 5.20 % | 9,398 |
| `identity_hate` | 0.98 % | 1,773 |
| `severe_toxic` | 0.90 % | 1,623 |
| `threat` | 0.31 % | 563 |

### Known biases in the data

- **Annotator subjectivity.** Labels are crowd judgements of "toxicity", a contested construct.
  Reclaimed slurs, in-group speech, quotation of abuse, and sarcasm are systematically
  mislabelled in corpora of this kind, and the model learns those errors as ground truth.
- **Identity-term confounding.** In this corpus, comments mentioning some identity groups are
  disproportionately toxic. A model that has only the text learns the term as a shortcut. §7
  measures exactly this and finds it.
- **Domain and era.** English Wikipedia talk pages, roughly a decade old. Platform norms,
  slang, and evasion techniques have all moved.
- **No demographic annotations at all**, which is why §7 must use term proxies.
- **The label set is not a taxonomy.** `severe_toxic` is a near-subset of `toxic`; the six
  columns are correlated, not independent, and the model treats them as independent.

---

## 7. Fairness: per-identity-term analysis

Full table, all 54 present terms with per-label breakdowns:
[`docs/fairness-report.md`](docs/fairness-report.md).

**Method.** 57 identity *descriptor* terms (adapted from Dixon et al. 2018, "Measuring and
Mitigating Unintended Bias in Text Classification" — descriptors only, never slurs, so the
repository stays publishable). For each term, the slice of held-out comments containing it is
compared to the whole. The number that captures Jigsaw's documented failure is the
**false-positive rate among the non-toxic rows of the slice** — what an innocent author feels —
against the background non-toxic flag rate. `selection_rate` is what the moderation queue feels;
`base_rate` is what makes the difference between the two legible. Every rate carries a bootstrap
interval. Under-powered slices are reported *with* a low-power flag, never dropped, because
dropping them is how the worst-affected group disappears from a fairness report.

**Coverage.** 57 terms searched · 54 present in the test set (`lgbtq`, `nonbinary`, `paralyzed`
absent) · 30 with n ≥ 30 · 24 reported as under-powered.

### Headline

| | |
|---|---|
| Background false-positive rate (non-toxic comments, `toxic` label) | **0.0258** |
| Background flag rate (all comments) | 0.0996 |
| Largest false-positive gap | **+0.2705**, term `homosexual` |
| Largest macro-F1 drop inside a scored slice | **−0.3384**, term `islam` |
| Four-fifths ratio across scored terms | **0.045** |
| Material disparity (gap > 0.10)? | **Yes** |

### The finding

**The model over-flags non-toxic comments that mention sexual-orientation terms by roughly an
order of magnitude.**

| term | n | base rate | flag rate | FPR | FPR 95 % CI | FPR ÷ background |
|---|---|---|---|---|---|---|
| `homosexual` | 37 | 0.270 | 0.486 | **0.296** | [0.148, 0.481] | **11.5×** |
| `gay` | 196 | 0.485 | 0.561 | **0.267** | [0.188, 0.356] | **10.4×** |
| `female` | 71 | 0.085 | 0.141 | 0.092 | [0.031, 0.169] | 3.6× |
| `male` | 71 | 0.056 | 0.127 | 0.090 | [0.030, 0.164] | 3.5× |
| `black` | 262 | 0.126 | 0.149 | 0.079 | [0.044, 0.118] | 3.1× |
| `jew` | 78 | 0.282 | 0.269 | 0.071 | [0.018, 0.143] | 2.8× |
| `muslim` | 92 | 0.130 | 0.109 | 0.037 | [0.000, 0.087] | 1.5× |
| `white` | 235 | 0.094 | 0.111 | 0.038 | [0.014, 0.066] | 1.5× |
| *background* | 31,877 | 0.098 | 0.100 | 0.0258 | — | 1.0× |

`gay` is the one to read closely, because it is the only large slice among the worst offenders.
196 comments, 95 of them genuinely toxic — the base rate really is elevated, at 48.5 % against a
corpus-wide 9.8 %. But among the **101 non-toxic** comments in that slice, the model flags
**26.7 %** as toxic, against 2.6 % elsewhere, and the interval [0.188, 0.356] excludes the
background by a wide margin. Inside the same slice, `identity_hate` recall reaches 0.944 while
its false-positive rate reaches **0.451**: nearly half of the non-`identity_hate` comments that
mention the word "gay" are flagged as identity hate. That is the Dixon failure mode reproduced
exactly — the term itself is carrying the signal.

`homosexual` points the same way with a wider interval; at n = 37 it is close to the power floor
and the direction, not the magnitude, is what should be believed.

The **four-fifths ratio of 0.045** simply confirms the spread is enormous. It is reported for
completeness, not as a legal test: it is a selection-rate ratio between overlapping term slices
whose true toxicity base rates genuinely differ by a factor of five, so demographic parity is the
wrong yardstick here. The FPR-against-background comparison in the table is the one that
isolates model error from data composition.

### The second finding

`islam` is the largest macro-F1 drop: 0.347 inside the slice against 0.686 for the same labels
overall. It rests on very few positives (4 toxic, 2 obscene, 2 insult in 75 comments) and the
drop is driven mainly by low recall — the model *misses* toxicity in comments mentioning `islam`
(`toxic` recall 0.25 in-slice) rather than over-flagging it. Under-detection for one group and
over-flagging for another are different harms in different directions, and both are present.

### What this card does **not** claim

This report names which metrics move and by how much. It issues **no fair / not fair verdict**.
Demographic parity and equal opportunity cannot both hold when base rates differ, so choosing
which to honour is a deployer decision, not a measurement. And term presence is not group
membership: a slice mixes self-description, third-person discussion, and quotation, and the model
may be reacting to any of them.

---

## 8. Ethical considerations, caveats, and recommendations

### Named harms and their mitigations

| harm | who bears it | mitigation in force |
|---|---|---|
| **Over-flagging comments that mention LGBTQ identity** (10–11× background FPR) sends disproportionately many innocent LGBTQ-related comments to a moderation queue, and — if a queue is ever drained by policy rather than by reading — silences a group for naming itself. | Comment authors discussing sexual orientation. | Human review is mandatory before any moderation action (§2). Per-slice FPR is a monitored metric, not a one-off. **This is not fixed. It is disclosed.** |
| **Under-detection of toxicity in comments mentioning `islam`** (in-slice `toxic` recall 0.25). | Targets of that abuse, who are under-protected. | Same review queue; per-slice recall monitored. |
| **Low precision on the three rare labels** (0.289–0.321) means roughly 2 of 3 flags on `severe_toxic`, `threat` and `identity_hate` are false. | Authors of wrongly flagged comments; reviewers, whose time is spent on noise. | Deliberate: those thresholds were tuned on F-beta with beta 3–5 because a missed threat costs more than a wasted review. Automating on these columns is out of scope (§2). |
| **Missed threats** (recall 0.677 — roughly one in three real threats is not flagged). | Targets of threats. | Named, not solved. The model is a triage aid layered on existing reporting paths, never their replacement. |
| **Accuracy-driven overconfidence.** 97.6 % accuracy reads like a solved problem; the prior-only baseline scores 96.2 % catching nothing. | Anyone reading a summary rather than this card. | Accuracy is banned from run selection in code (`select_best_run` raises); macro-F1 is the promotion metric; the baseline row is reported beside it everywhere. |
| **Silent degradation on 2026 traffic.** The corpus is a 2017-era snapshot. | Everyone. | Per-label flag rates from this evaluation are persisted as the drift reference; the serving path logs every prediction for later re-scoring. |

### Accepted disclosure: white-box evasion via the public registry

The model artifact, its per-label decision thresholds, and this card are **all publicly
readable**, by the owner's explicit decision recorded in the delivery spec (§13) on 2026-07-31.

This is a real and accepted trade. `toxic-clf` is a linear model over a fixed TF-IDF feature
space. Publishing the artifact hands an attacker the exact coefficient vector and the exact
per-label decision boundary. Constructing an evasive comment therefore becomes an **offline
optimisation with zero queries against the service and no log entry** — no rate limit, no
anomaly detector, and no monitoring dashboard can see it happen, because it does not happen on
the service. The premortem recommended keeping the artifact in a private project for exactly
this reason.

It was published anyway, because the graded requirement is a **visibly promoted registry entry**
and a screenshot is weaker evidence than a page an assessor can open. This is the correct trade
for a coursework deliverable whose subject is the MLOps lifecycle, and it would be the wrong
trade for a production moderation service. **Anyone reusing this artifact in production should
treat its parameters as compromised and keep the registry private.**

Compensating controls that remain in force on the serving path — none of which mitigate offline
evasion, and they are not claimed to: a `/predict` rate limit, an input-size cap, and a demo API
key / source allowlist. The human-review queue does not mitigate it either: an evasive comment
is designed to score below threshold and therefore never reaches the queue.

### Pretraining contamination

The DistilBERT comparison model in §5 is a fine-tune of `distilbert-base-uncased`, whose
pretraining corpus includes English Wikipedia. **The Jigsaw comments are drawn from English
Wikipedia talk pages.** It is therefore possible — and not checkable from outside — that some
evaluation comments were seen during pretraining. If so, DistilBERT's validation numbers are
optimistic by an unknown amount.

This is not fixable and not gradeable. Naming it is the rigor. It is one more reason the
promoted model is the classical one, which has no pretraining at all and whose only exposure to
this corpus is the 180,633 training rows recorded in §6.

### Other caveats

- **Both feature caps bind.** The vocabulary is truncated by design, not by the corpus.
- **The rare-label numbers are thin.** `threat` rests on 99 held-out positives; its PR-AUC
  interval spans 0.19. Do not compare a future model to this one on `threat` without an
  interval.
- **Calibration was fitted, not verified.** `CalibratedClassifierCV(method="sigmoid")` was used,
  but no reliability diagram or calibration-error measurement was produced. The probabilities
  should be treated as ranked scores, not as calibrated likelihoods, until that is measured.
- **The 51-second cold load** is long enough to matter to any orchestrator's health check.
- **`skops` loading still requires an explicit trust list.** Loading this artifact trusts
  `sklearn.calibration._CalibratedClassifier` and `sklearn.calibration._SigmoidCalibration`. A
  loader must pin the digest above, not merely the filename.

### Recommendations for the next version

1. Raise or remove the `max_features` caps and re-measure; both bind today.
2. Measure calibration explicitly and publish a reliability curve.
3. Attack the identity-term bias directly. The lowest-cost intervention with published evidence
   behind it is Dixon et al.'s: add non-toxic examples containing the over-flagged terms until
   the term's in-corpus toxicity rate matches the background, and re-measure the same slices.
4. Land the DistilBERT quantized export with a passing parity gate and compare it to this model
   **on cross-validation folds** — never on this test split, which is spent.
5. Slice by comment length and by non-ASCII character fraction, both of which the char n-gram
   features are sensitive to and neither of which is measured.

---

## 9. AIBOM addendum — supply chain

### Model artifact

| | |
|---|---|
| File | `toxic-clf.skops` |
| SHA-256 | `db678467907743fbce5d25ab8c9ad56cd0c89e053b46be81822dcb2095842454` |
| Size | 400,229,552 bytes |
| Format | `skops` archive (zip container with `schema.json`), verified as such before upload |
| Registry | `rockcyber-org/wandb-registry-model/toxic-clf:production` |

### Training data

| | |
|---|---|
| Raw corpus SHA-256 | `2acea3b6f0641a19c6c972e49c0a6dadddeca16aff3f0d5042a123a82221d898` |
| Realized split SHA-256 (`split_version`) | `a24b8dd61fd539f6fae25c3955d4d8faf9036e854f3e492ae075dd522be31b8c` |
| Environment SHA-256 (`env_version`) | `fa2aba19ecb298d7d3bddb41bc900bdfd606dfcf572af72b08ad67de2c7c06f8` |

### Direct dependencies

Pinned exactly and installed with `--require-hashes --only-binary=:all:` from
`requirements/dev.lock`, so no source distribution executes code at install time. First
recorded wheel hash shown; the lock carries every platform hash.

| package | version | sha256 (first 16, from `requirements/dev.lock`) |
|---|---|---|
| `scikit-learn` | 1.5.2 | `03b6158efa3faaf1…` |
| `skops` | 0.13.0 | `55e2cccb18c86f59…` |
| `numpy` | 2.1.3 | `016d0f6f5e77b0f0…` |
| `scipy` | 1.14.1 | `0c2f95de3b04e26f…` |
| `pandas` | 2.2.3 | `062309c1b9ea12a5…` |
| `iterative-stratification` | 0.1.9 | `476f8deff6753fb1…` |
| `datasketch` | 1.6.5 | `59311b2925b2f375…` |
| `pydantic` | 2.9.2 | `d155cef71265d1e9…` |

No pretrained backbone, no external embedding model, no lookup table: every parameter in this
artifact was fitted from the training rows named above. (The unpromoted DistilBERT comparison
model in §5 *does* have a backbone — `distilbert-base-uncased`, Apache-2.0 — and is registered
separately as `distilbert-toxic` with its own tree digest.)

### Build and evaluation environment

| | |
|---|---|
| Python | 3.11.15, `PYTHONHASHSEED=0` asserted at process start |
| Final fit and evaluation host | aarch64 (Jetson, 8-core Cortex-A78AE @ 2.2 GHz), 61 GB RAM |
| Cross-validation host | rented NVIDIA A40 pod, 9 vCPU (CPU-bound workload; GPU unused by the classical fit) |
| Serving target | AWS `t4g.medium`, 2 vCPU / 4 GB, Graviton2 |
| `skops` advisory floor | ≥ 0.13.0 enforced in code before any artifact is written or uploaded |

**Not yet produced:** a CycloneDX AIBOM JSON companion scored against the
genai-security-project completeness evaluator. The fields above cover its substance; the machine
-readable form is outstanding.

---

## 10. Reproducing every number in this card

| number | command / artifact |
|---|---|
| Prepared corpus, split, folds | `make fetch-data`, then `PYTHONHASHSEED=0 python -m model.data.run --csv data/raw/jigsaw-toxic-comment-train.csv --seed 42` (≈14 min; the result is cached as a bundle directory whose manifest carries all three version hashes and a per-file digest) |
| Baseline, CV, thresholds, final fit, latency | `python run_phase1.py baseline cv thresholds final latency` → `artifacts/baseline_metrics.json`, `artifacts/classical-cv/cv_metrics.json`, `artifacts/classical-cv/thresholds.json`, `artifacts/feature_footprint.json`, `artifacts/serving_footprint.json`. The `cv` stage was run on rented hardware via `python infra/runpod/deploy_runpod.py --classical` (84.1 min of fit time); every other stage ran locally. |
| Held-out evaluation, fairness, registry | `python run_phase1_release.py` → `artifacts/test_metrics.json`, `docs/fairness-report.md`, `docs/test-set-touch-log.md`, `artifacts/registry_receipt.json`. **Refuses to re-score this split.** |
| Held-out predictions | `artifacts/test_probabilities.npz` — `y_true`, `y_prob`, `y_flag`, 31,877 × 6 |
| Registry is public and promoted | `python scripts/verify_public_registry.py` → `artifacts/registry_public_check.json` |

---

## 11. Sign-off

| | |
|---|---|
| Maintainer | Rock Lambros — `rock@rockcyber.com` |
| Card version | 1.0.0 |
| Card date | 2026-08-01 |
| Model version described | `toxic-clf` v1.0.0 / W&B `toxic-clf:v0` @ `production` |
| Next review | on the next promotion to `production`, or on any change to `split_version`, whichever comes first |
| Change log | git history of this file; the model's own history is the W&B collection version list |
| Security contact | [`SECURITY.md`](SECURITY.md) |

## Artifact digest of record

| Artifact | sha256 |
|---|---|
| `toxic-clf.skops` | `db678467907743fbce5d25ab8c9ad56cd0c89e053b46be81822dcb2095842454` |
| `thresholds.json` | `56d2e48834b676d03eadc4920330da73e6a6af8c13ffe306646a63bbcb8c6635` |
| `baseline_flag_rates.json` | `fbd42f2ef2db72f44eb1efbfd64a1655ada5919405eab538e095c91829d04105` |

Three artifacts, because two of them are as security-relevant as the coefficients.
`thresholds.json` **is** the decision boundary — an unverified copy of it is a silent policy
change that no metric would flag — and `baseline_flag_rates.json` is the reference the drift
panel measures production against. Both are mounted read-only into a container that fails
closed without them, so both are fetched and digest-verified exactly the way the model is.

**`baseline_flag_rates.json` has not been produced.** Its row above is not a digest of a
file: it is the SHA-256 of the sentence

```
PENDING PHASE 1 PROMOTION: baseline_flag_rates.json has not been produced
```

which no artifact can match — the same fail-closed sentinel this section carried for the
model before Phase 1 promoted one. The monitoring instance's roll therefore stops with
`digest mismatch on baseline_flag_rates.json` rather than starting a dashboard whose drift
panel has nothing to drift from. Producing it needs the held-out probabilities in
`artifacts/test_probabilities.npz` and `model.thresholds.compute_baseline_flag_rates`; it is
**not** byte-reproducible (it stamps `generated_at_utc`), so the digest of record must be
computed from the exact file that is uploaded, and this row replaced with it.

The table is keyed on the **filename**, and it is read that way rather than by position.
`infra/deploy/instance/fetch_artifacts.sh` looks each artifact up by its own name, because
"the first 64-hex string in this file" silently becomes the corpus digest, the split digest
or the environment digest the moment section 9 is reordered — and the fetcher would then be
verifying a value the serving loader never checks. It is also the key of the S3 mirror the
deploy falls back to, so a row here is what makes an object findable at all.

The serving backend refuses to load any artifact whose SHA-256 differs from this value, and
refuses to start if the `MODEL_DIGEST` environment variable differs from it. This block is the
provenance anchor: it lives in git rather than in the registry, so an attacker holding the
registry credential cannot supply both the artifact and its expected digest. It is read by
`backend/model_card.py::read_expected_digest` and cross-checked in
`backend/model_loader.py::load_from_settings`.

This value was **computed from the artifact in hand**, not transcribed from the registry UI:

```bash
sha256sum artifacts/toxic-clf.skops | cut -d' ' -f1
# db678467907743fbce5d25ab8c9ad56cd0c89e053b46be81822dcb2095842454
```

A transcribed digest is a digest the registry supplied, which is exactly the co-location this
control exists to break. It also could not have been recomputed by re-dumping: `skops.io.dump`
embeds ZIP entry timestamps and is **not** byte-reproducible — three dumps of the same fitted
pipeline produced three different SHA-256 values — so the digest of record must come from the
exact file that was uploaded, and a "rebuild and compare" check would fail on a legitimate
artifact.

This block replaced a deliberate fail-closed sentinel (the SHA-256 of the string
`PENDING PHASE 1 PROMOTION: no artifact has this digest`) that no file could match, so the
backend refused to start until a real model was promoted. Phase 1 has now promoted one.

- MODEL_ARTIFACT: toxic-clf
- MODEL_REGISTRY_VERSION: 0
- MODEL_DIGEST: sha256:db678467907743fbce5d25ab8c9ad56cd0c89e053b46be81822dcb2095842454
