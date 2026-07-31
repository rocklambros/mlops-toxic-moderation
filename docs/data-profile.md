# Data Profile

- Source: `data/raw/jigsaw-toxic-comment-train.csv`
- `raw_sha256`: `2acea3b6f0641a19c6c972e49c0a6dadddeca16aff3f0d5042a123a82221d898`
- Rows after dedup: 212510
- Rows with no positive label: 190573

## Per-label counts

| Label | Positives | Rate |
|---|---:|---:|
| `toxic` | 20899 | 9.8344% |
| `severe_toxic` | 1909 | 0.8983% |
| `obscene` | 11844 | 5.5734% |
| `threat` | 662 | 0.3115% |
| `insult` | 11057 | 5.2030% |
| `identity_hate` | 2086 | 0.9816% |

## Co-occurrence (6x6)

| | `toxic` | `severe_toxic` | `obscene` | `threat` | `insult` | `identity_hate` |
|---|---:|---:|---:|---:|---:|---:|
| `toxic` | 20899 | 1909 | 11286 | 630 | 10468 | 1969 |
| `severe_toxic` | 1909 | 1909 | 1834 | 158 | 1669 | 452 |
| `obscene` | 11286 | 1834 | 11844 | 425 | 8686 | 1546 |
| `threat` | 630 | 158 | 425 | 662 | 430 | 145 |
| `insult` | 10468 | 1669 | 8686 | 430 | 11057 | 1736 |
| `identity_hate` | 1969 | 452 | 1546 | 145 | 1736 | 2086 |

`severe_toxic <= toxic` asserted by `assert_label_hierarchy`.
