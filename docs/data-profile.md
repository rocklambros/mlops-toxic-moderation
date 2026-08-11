# Data Profile

- Source: `tests/fixtures/mini_jigsaw.csv`
- `raw_sha256`: `7b4ca5182545791ea8aa2a3262b63738ddd0a22e64e9f401b8dccae37387b42a`
- Rows after dedup: 64
- Rows with no positive label: 16

## Per-label counts

| Label | Positives | Rate |
|---|---:|---:|
| `toxic` | 48 | 75.0000% |
| `severe_toxic` | 12 | 18.7500% |
| `obscene` | 12 | 18.7500% |
| `threat` | 12 | 18.7500% |
| `insult` | 14 | 21.8750% |
| `identity_hate` | 12 | 18.7500% |

## Co-occurrence (6x6)

| | `toxic` | `severe_toxic` | `obscene` | `threat` | `insult` | `identity_hate` |
|---|---:|---:|---:|---:|---:|---:|
| `toxic` | 48 | 12 | 12 | 12 | 14 | 12 |
| `severe_toxic` | 12 | 12 | 1 | 6 | 2 | 4 |
| `obscene` | 12 | 1 | 12 | 0 | 1 | 0 |
| `threat` | 12 | 6 | 0 | 12 | 1 | 0 |
| `insult` | 14 | 2 | 1 | 1 | 14 | 0 |
| `identity_hate` | 12 | 4 | 0 | 0 | 0 | 12 |

`severe_toxic <= toxic` asserted by `assert_label_hierarchy`.
