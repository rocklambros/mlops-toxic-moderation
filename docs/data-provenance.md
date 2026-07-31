# Data Provenance

| Field | Value |
|---|---|
| Source | Kaggle `julian3833/jigsaw-multilingual-toxic-comment-classification` |
| Member file | `jigsaw-toxic-comment-train.csv` |
| Endpoint | `GET /api/v1/datasets/download/{dataset}/{member}` |
| Download size | 39,078,413 bytes (zip) |
| Extracted size | 95,538,001 bytes |
| Fetched | 2026-07-31 |
| `raw_sha256` | `2acea3b6f0641a19c6c972e49c0a6dadddeca16aff3f0d5042a123a82221d898` |
| Rows | 223,549 |
| Schema | `id, comment_text, toxic, severe_toxic, obscene, threat, insult, identity_hate` |

`raw_sha256` is logged to every W&B run. If it changes, the mirror was re-uploaded and
every downstream number must be re-derived.

## Day-1 smoke check (2026-07-31)

Ran on the real corpus before any Phase 1 work, so a mirror schema mismatch would surface
outside the training window:

```
raw_sha256 = 2acea3b6f0641a19c6c972e49c0a6dadddeca16aff3f0d5042a123a82221d898
rows       = 223549
columns    = ['id', 'comment_text', 'toxic', 'severe_toxic', 'obscene', 'threat', 'insult', 'identity_hate']
counts     = {'toxic': 21384, 'severe_toxic': 1962, 'obscene': 12140, 'threat': 689, 'insult': 11304, 'identity_hate': 2117}
severe_toxic <= toxic holds
```

- `load_raw` accepted the file without raising; the six label columns match `model.labels.LABELS`
  exactly and IDs are 16-hex strings, so `dtype={"id": str}` in `load_raw` is correct.
- `assert_label_hierarchy` passes: `((severe_toxic == 1) & (toxic == 0)).sum() == 0`.
- No duplicate `id` values and no byte-identical `comment_text` values in the raw file.

### Row count differs from the plan's 159,571

The Phase 0 plan predicted 159,571 rows. The mirror actually delivers **223,549**. This is
not corruption and not the wrong file — the schema and the hierarchy invariant both hold.
The multilingual competition republishes the English six-label data as the original
challenge's train split **concatenated with its labelled test split**:

| Segment | Rows | `toxic` positives |
|---|---|---|
| Rows 0–159,570 (original `train.csv`) | 159,571 | 15,294 |
| Rows 159,571–223,548 (original labelled `test.csv`) | 63,978 | 6,090 |
| Total | 223,549 | 21,384 |

159,571 + 63,978 = 223,549, and 15,294 `toxic` in the leading segment is the published
count for the original challenge train split. The extra 63,978 rows are additional labelled
English data of the same schema, so they are kept. Every downstream figure in this repo is
derived from all 223,549 rows.

Consequence for cost: the corpus is 40% larger than planned, so the plan's "~6 min per
`make data` run" underestimates by roughly the same factor. The measured cost is recorded
below.

## Measured pipeline cost (build box, aarch64)

| Stage | Measurement |
|---|---|
| MinHash, `num_perm=128`, `update_batch` | 2.25 ms/row |
| MinHash, `num_perm=128`, per-shingle loop | 5.10 ms/row (not used) |
| Corpus signing, 223,549 rows, signed once | dominates the ~13.6 min end-to-end below |
| `make data` end to end, real corpus, run 1 | `13m34s`, exit 0 |
| `make data` end to end, real corpus, run 2 | `13m33s`, exit 0 |
| Rows in | 223,549 |
| Rows after dedup | 212,510 (11,039 collapsed at `DEDUP_JACCARD=0.70`) |
| Split at seed 42 | `train=180633 test=31877 folds=5` |
| `split_version` at seed 42 | `a24b8dd61fd539f6fae25c3955d4d8faf9036e854f3e492ae075dd522be31b8c` |
| `env_version` | `fa2aba19ecb298d7d3bddb41bc900bdfd606dfcf572af72b08ad67de2c7c06f8` |
| Firewall report | `method=lsh-blocked-exact id_overlap=0 exact_text_leak=0 near_duplicate_pairs=0 max_cross_jaccard=0.6997 worst_pair=('33cb547f695f42a8', '151ea69b8cae6462')` |

The plan budgeted ~6 min per run against an assumed 159,571 rows. The corpus is 223,549
rows and measured cost is ~13.6 min per run. Budget ~14 min for any `make data` invocation
on the real corpus, and note that `--seed` changes force a full re-sign.

The `(25, 5)` banding is marginally faster than the superseded `(16, 6)` despite producing
more blocking candidates, because collapsing more rows early shrinks the exact-Jaccard
work that follows.

### Determinism gate — PASS

Two independent processes, back to back, `PYTHONHASHSEED=0`, seed 42:

| Field | Run 1 | Run 2 |
|---|---|---|
| `raw_sha256` | `2acea3b6…21d898` | `2acea3b6…21d898` |
| `split_version` | `a24b8dd6…31b8c` | `a24b8dd6…31b8c` |
| `env_version` | `fa2aba19…c06f8` | `fa2aba19…c06f8` |
| `near_duplicate_pairs` | 0 | 0 |
| `max_cross_jaccard` | 0.6997 | 0.6997 |
| exit code | 0 | 0 |

`split_version` is identical across runs. Normalisation, shingling, MinHash signing, dedup
tie-breaking, and the iterative stratified split are all reproducible on the real corpus,
not just on the fixture.

## Threshold decision, 2026-07-31: `DEDUP_JACCARD` 0.80 → 0.70

### What the corpus showed

At `DEDUP_JACCARD=0.80` the gate refused to pass: 8,711 cross-split pairs at Jaccard ≥ 0.70,
worst at 0.7993. Those pairs sat in the band `[0.70, 0.80)` that dedup was designed to keep
and the gate was designed to reject, so `make data` exited non-zero on every run.

The obvious remedies both fail. Raising dedup's blocking recall does nothing, because the
pairs are below dedup's threshold and would be kept even at recall 1.0. Lowering
`DEDUP_JACCARD` while keeping the gate strictly below it just moves the band: Wikipedia
boilerplate has a continuum of similarities, so no threshold pair leaves the band empty.

### The measurement that decided it

Rather than argue from the pair count, the affected rows were counted directly:

| | |
|---|---|
| pairs at J ≥ 0.70 | 8,711 |
| **distinct test rows compromised** | **564 = 1.75% of the held-out set** |
| distinct train rows involved | 1,248 |
| pairs per compromised test row | 15.4 |
| similarity distribution | 7,371 pairs in 0.70–0.75, 1,340 in 0.75–0.80 |
| extra rows removed by moving to 0.70 | 2,160 = 1.01% of the deduped corpus |

The 15.4 pairs-per-row ratio is the finding. These are not 8,711 independent leaks; they are
a few hundred Wikipedia boilerplate templates — test-edit reverts, vandalism warnings —
each recurring many times. A pair count conflates "many rows leaked once" with "few rows
leaked many times", and those demand opposite responses.

### The decision

Both stages now use 0.70. The trade is 1.01% of training data for a held-out set with zero
known near-duplicates: there are 212,510 training rows and exactly one measurement
instrument, and Phase 1 locks against that instrument.

The gate's independence no longer comes from a lower threshold — that was unsatisfiable —
but from the blocking method. Dedup blocks at `(25, 5)`, the gate at `(17, 4)`, and the
decision in both is exact Jaccard.

### The coupling that nearly bit

Moving the threshold alone would have been wrong. `(16, 6)` has only **0.865** recall at
J=0.70, so dedup would have missed 13.5% of true duplicates and the gate would have failed
again for a new reason that looked identical to the old one.

`(32, 4)` scores better than the chosen banding — 0.9998 recall, exactly 128 permutations —
and is **disqualified**. LSH bands are consecutive permutation groups, so the gate's
`(17, 4)` bands would be a strict prefix of dedup's, and every pair the gate could block
dedup would already have blocked. The gate could never catch a dedup miss, which is the
tautology this gate exists to avoid. Rows-per-band must differ, and
`test_gate_is_independent_of_dedup_by_banding_not_by_threshold` now asserts it.

`(25, 5)` gives 0.9899 at J=0.70, the best available at rows=5 inside 128 permutations. The
residual ~1% miss sits exactly at the threshold and decays fast above it — 0.9989 by J=0.75
— so the pairs most likely to be missed are the ones least costly to miss, and the gate's
different banding is there to catch the residue.

### Superseded run, retained for provenance

The 0.80 configuration produced `split_version=09c60bf5…d5e274` and
`env_version=76141b06…39f161`. Any W&B run or artifact carrying those values predates this
decision and evaluates against a test set containing 564 compromised rows. `split_version`
hashes the realized split, the per-id label fingerprint, and the dedup parameters, so the
change is visible rather than silent.
