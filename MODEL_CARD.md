# Model Card: toxic-clf

Multi-label toxic-comment classifier served by the moderation backend.

This card is a graded, public artifact, and it is also machine-read. `backend/model_card.py`
parses the digest-of-record block, and `tests/unit/test_preprocess.py` compares the
normalization claim below against the code actually on the serving path. Claims here are
load-bearing rather than descriptive: if the card says the serving path folds homoglyphs and
it does not, the build goes red.

## Status: draft, pending Phase 1

Phase 1 — training, calibration, threshold tuning, and registry promotion — has **not**
completed at the time of writing. Every field that depends on a fitted model is marked
`PENDING PHASE 1` below and must be filled in from the Phase 1 evaluation report before this
card is presented as final.

No metric in this card has been estimated, interpolated, or carried over from another model.
A `PENDING PHASE 1` marker means the number does not exist yet, and it is deliberately not a
placeholder number that could be mistaken for a measurement.

## Model details

- **Name:** `toxic-clf`
- **Task:** multi-label binary classification over the six Jigsaw labels, in this exact
  order: `toxic`, `severe_toxic`, `obscene`, `threat`, `insult`, `identity_hate`. The order
  is fixed in `model/labels.py` and every array-to-dict conversion goes through
  `model.contract.probs_to_dict`, never through an independent `zip`.
- **Architecture:** TF-IDF character n-gram features into a one-vs-rest linear classifier
  with per-label probability calibration. Inference is CPU-only.
- **Registry version:** PENDING PHASE 1
- **Serialization:** `skops`, deserialized under the static trusted-type allowlist in
  `backend/model_loader.py`. `trusted=True` and `get_untrusted_types()` are never used, and
  a digest mismatch refuses to load rather than degrading.
- **Owner:** Rock Lambros

## Intended use

Automated triage of user-submitted comment text in the demo moderation service: score a
comment, apply per-label thresholds, and route it to `allow`, `review`, or `block`. The
`review` outcome exists so that borderline and high-consequence cases reach a human.

**Out of scope.** This model is not a safety control on its own, is not intended for
decisions about people (moderation, employment, credit, or law enforcement outcomes about a
named individual), and is not intended for languages other than English. It was fitted on
Wikipedia talk-page comments and its behaviour on other registers is unmeasured.

## Training data

- **Source:** Jigsaw Toxic Comment Classification Challenge (English Wikipedia talk-page
  comments, human-labelled for the six categories above).
- **Splits:** produced by `model/data/`, stratified by label combination, with a 15% test
  split locked by `split_version`. The locked test set is not re-cut after any model is
  registered against it.
- **Deduplication:** exact-normalized collapse under the frozen corpus normalizer
  (`model.normalize.normalize`), keeping the lowest id and OR-ing the six labels.
- **Known label bias:** the corpus over-associates identity terms (for example mentions of
  race, religion, sexuality, or disability) with toxicity, because those terms appear
  disproportionately in the toxic examples. Any model fitted on it inherits that
  association. The Phase 1 evaluation report is the artifact that quantifies it.

## Normalization on the serving path

The serving path applies `model.normalize.normalize_for_serving`, re-exported as
`backend.preprocess.normalize`. It is the **frozen corpus normalizer plus** three additions:

1. zero-width character removal,
2. **homoglyph folding** of common Cyrillic and Greek confusables to their Latin lookalikes
   (`уou` serves as `you`), plus combining-mark stripping,
3. a hard input cap of `MAX_INPUT_CHARS` characters.

Folding runs **after** the corpus normalizer and is never imported by `model/data/dedup.py`.
That ordering is the point: dedup output does not move, `split_version` does not move, and
the locked test set stays locked, while an evasion attempt is mapped *onto* the training
distribution rather than away from it.

`MAX_INPUT_CHARS` has exactly one definition, in `model/normalize.py`. It is not readable
from the environment and is not a `Settings` field, because a cap that a deploy-time variable
can widen is not a cap.

## Thresholds

Per-label decision thresholds are tuned on the validation split only, never on the locked
test split, and are shipped as a separate `thresholds.json` artifact loaded at startup.

- Per-label threshold values: PENDING PHASE 1

## Metrics

All metrics are computed on the locked 15% test split unless stated otherwise.

| Metric | Value |
|---|---|
| Macro ROC-AUC | PENDING PHASE 1 |
| Per-label ROC-AUC | PENDING PHASE 1 |
| Macro PR-AUC | PENDING PHASE 1 |
| Per-label precision / recall / F1 at the tuned threshold | PENDING PHASE 1 |
| Calibration error per label | PENDING PHASE 1 |
| Subgroup performance across identity terms | PENDING PHASE 1 |
| Inference latency, p50 / p95 / p99 | PENDING PHASE 2 load pass (`docs/latency-baseline.md`) |

## Limitations

- **Combining marks are stripped at serving time.** `händbuch` is scored as `handbuch`,
  while the corpus retains `händbuch`. This is a deliberate, bounded train/serve difference
  confined to inputs containing confusables or combining marks.
- **Homoglyph folding is not exhaustive.** It covers a fixed table of common Cyrillic and
  Greek confusables. Scripts outside that table, and novel substitutions, are not folded.
- **Paraphrase and context evasion are not addressed at all.** Nothing in the normalizer
  helps with a rephrased insult, coded language, or toxicity that depends on conversational
  context the model never sees.
- **The review queue does not mitigate evasion.** A successful evasion is, by construction,
  never flagged, so it is never queued for a human. The review queue raises the recall of
  *borderline* cases, not of *evaded* ones.
- **Severe-label decisions are conservative by design.** A severe label blocks only when it
  is well clear of its threshold; a near-threshold severe score is routed to human review
  instead, because that is the case where the machine is least reliable.
- **English Wikipedia talk pages only.** Performance on other platforms, registers, or
  languages is unmeasured, not merely lower.

## Ethical considerations

Toxicity labels encode a specific set of community norms, and the identity-term bias
described under Training data means false positives are not distributed evenly across
groups. The system is therefore built so that a machine decision is reviewable: every
prediction is logged with its input, its probabilities, and its decision, a random-audit
stratum samples unflagged traffic so that confidently-allowed false negatives are visible,
and each review row records the probability with which it was sampled so the two strata can
be weighted rather than pooled.

Raw input text is retained only in the access-restricted database, only for
`INPUT_TEXT_RETENTION_DAYS` (30), and is never written to an application log line.
