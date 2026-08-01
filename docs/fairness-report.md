# Fairness: per-identity-term slice of the held-out test set

Jigsaw's documented unintended bias is that comments which merely **mention** an identity
group are over-flagged. The number that captures it is the false-positive rate among the
**non-toxic** rows of each term slice, against the background non-toxic flag rate.

- rows evaluated: 31877
- primary label for flag rate and FPR: `toxic`
- background non-toxic flag rate: **0.0258**
- terms searched: 57
- terms present in the test set: 54
- terms with enough rows to score (n >= 30): 30
- terms reported but under-powered: 24
- terms with no rows here, omitted from the table: lgbtq, nonbinary, paralyzed
- largest false-positive gap: **0.2705** (homosexual)
- largest macro-F1 drop inside a scored slice: **0.3384** (islam)
- four-fifths ratio across scored terms: 0.0445

| term | n | n_pos | base rate | flag rate | FPR | FPR 95% CI | FPR vs background | PR-AUC | note |
|---|---|---|---|---|---|---|---|---|---|
| queer | 8 | 5 | 0.625 | 1.000 | 1.000 | [1.000, 1.000] | 38.79 | 0.927 | low power |
| homosexual | 37 | 10 | 0.270 | 0.486 | 0.296 | [0.148, 0.481] | 11.49 | 0.907 |  |
| heterosexual | 11 | 0 | 0.000 | 0.273 | 0.273 | [0.000, 0.545] | 10.58 | n/a | low power |
| gay | 196 | 95 | 0.485 | 0.561 | 0.267 | [0.188, 0.356] | 10.37 | 0.901 |  |
| teenage | 10 | 2 | 0.200 | 0.200 | 0.125 | [0.000, 0.375] | 4.85 | 0.833 | low power |
| bisexual | 11 | 1 | 0.091 | 0.182 | 0.100 | [0.000, 0.300] | 3.88 | 0.500 | low power |
| female | 71 | 6 | 0.085 | 0.141 | 0.092 | [0.031, 0.169] | 3.58 | 0.637 |  |
| male | 71 | 4 | 0.056 | 0.127 | 0.090 | [0.030, 0.164] | 3.47 | 0.706 |  |
| woman | 102 | 9 | 0.088 | 0.127 | 0.086 | [0.032, 0.151] | 3.34 | 0.523 |  |
| black | 262 | 33 | 0.126 | 0.149 | 0.079 | [0.044, 0.118] | 3.05 | 0.703 |  |
| lesbian | 19 | 6 | 0.316 | 0.316 | 0.077 | [0.000, 0.231] | 2.98 | 0.786 | low power |
| straight | 103 | 10 | 0.097 | 0.146 | 0.075 | [0.022, 0.129] | 2.92 | 0.737 |  |
| asian | 48 | 8 | 0.167 | 0.188 | 0.075 | [0.000, 0.175] | 2.91 | 0.847 |  |
| jew | 78 | 22 | 0.282 | 0.269 | 0.071 | [0.018, 0.143] | 2.77 | 0.879 |  |
| sikh | 15 | 1 | 0.067 | 0.067 | 0.071 | [0.000, 0.214] | 2.77 | 0.500 | low power |
| man | 508 | 79 | 0.156 | 0.177 | 0.068 | [0.044, 0.093] | 2.62 | 0.829 |  |
| disabled | 15 | 0 | 0.000 | 0.067 | 0.067 | [0.000, 0.200] | 2.59 | n/a | low power |
| men | 154 | 16 | 0.104 | 0.123 | 0.058 | [0.022, 0.101] | 2.25 | 0.717 |  |
| islam | 75 | 4 | 0.053 | 0.067 | 0.056 | [0.014, 0.113] | 2.19 | 0.421 |  |
| christian | 181 | 12 | 0.066 | 0.088 | 0.053 | [0.024, 0.089] | 2.07 | 0.608 |  |
| women | 156 | 19 | 0.122 | 0.141 | 0.051 | [0.015, 0.095] | 1.98 | 0.829 |  |
| atheist | 23 | 3 | 0.130 | 0.174 | 0.050 | [0.000, 0.150] | 1.94 | 1.000 | low power |
| white | 235 | 22 | 0.094 | 0.111 | 0.038 | [0.014, 0.066] | 1.46 | 0.850 |  |
| muslim | 92 | 12 | 0.130 | 0.109 | 0.037 | [0.000, 0.087] | 1.45 | 0.636 |  |
| african | 102 | 8 | 0.078 | 0.069 | 0.032 | [0.000, 0.074] | 1.24 | 0.541 |  |
| hindu | 40 | 4 | 0.100 | 0.025 | 0.028 | [0.000, 0.083] | 1.08 | 0.209 |  |
| jewish | 165 | 10 | 0.061 | 0.048 | 0.026 | [0.006, 0.052] | 1.00 | 0.624 |  |
| blind | 54 | 6 | 0.111 | 0.111 | 0.021 | [0.000, 0.062] | 0.81 | 0.948 |  |
| indian | 120 | 10 | 0.083 | 0.075 | 0.018 | [0.000, 0.045] | 0.71 | 0.782 |  |
| american | 384 | 23 | 0.060 | 0.057 | 0.017 | [0.006, 0.030] | 0.64 | 0.760 |  |
| japanese | 124 | 2 | 0.016 | 0.032 | 0.016 | [0.000, 0.041] | 0.64 | 1.000 |  |
| chinese | 141 | 12 | 0.085 | 0.071 | 0.016 | [0.000, 0.039] | 0.60 | 0.895 |  |
| catholic | 74 | 2 | 0.027 | 0.027 | 0.014 | [0.000, 0.042] | 0.54 | 0.500 |  |
| irish | 98 | 7 | 0.071 | 0.051 | 0.011 | [0.000, 0.033] | 0.43 | 0.774 |  |
| arab | 81 | 14 | 0.173 | 0.086 | 0.000 | [0.000, 0.000] | 0.00 | 0.886 |  |
| buddhist | 11 | 0 | 0.000 | 0.000 | 0.000 | [0.000, 0.000] | 0.00 | n/a | low power |
| canadian | 78 | 4 | 0.051 | 0.038 | 0.000 | [0.000, 0.000] | 0.00 | 0.950 |  |
| deaf | 8 | 3 | 0.375 | 0.250 | 0.000 | [0.000, 0.000] | 0.00 | 1.000 | low power |
| elderly | 3 | 0 | 0.000 | 0.000 | 0.000 | [0.000, 0.000] | 0.00 | n/a | low power |
| european | 129 | 6 | 0.047 | 0.031 | 0.000 | [0.000, 0.000] | 0.00 | 0.819 |  |
| hispanic | 12 | 1 | 0.083 | 0.000 | 0.000 | [0.000, 0.000] | 0.00 | 0.250 | low power |
| immigrant | 15 | 1 | 0.067 | 0.067 | 0.000 | [0.000, 0.000] | 0.00 | 1.000 | low power |
| latina | 2 | 0 | 0.000 | 0.000 | 0.000 | [0.000, 0.000] | 0.00 | n/a | low power |
| latino | 12 | 1 | 0.083 | 0.083 | 0.000 | [0.000, 0.000] | 0.00 | 1.000 | low power |
| lgbt | 21 | 0 | 0.000 | 0.000 | 0.000 | [0.000, 0.000] | 0.00 | n/a | low power |
| mexican | 25 | 1 | 0.040 | 0.040 | 0.000 | [0.000, 0.000] | 0.00 | 1.000 | low power |
| middle eastern | 13 | 0 | 0.000 | 0.000 | 0.000 | [0.000, 0.000] | 0.00 | n/a | low power |
| mormon | 15 | 0 | 0.000 | 0.000 | 0.000 | [0.000, 0.000] | 0.00 | n/a | low power |
| older | 80 | 5 | 0.062 | 0.037 | 0.000 | [0.000, 0.000] | 0.00 | 0.903 |  |
| protestant | 12 | 0 | 0.000 | 0.000 | 0.000 | [0.000, 0.000] | 0.00 | n/a | low power |
| refugee | 7 | 1 | 0.143 | 0.143 | 0.000 | [0.000, 0.000] | 0.00 | 1.000 | low power |
| trans | 9 | 2 | 0.222 | 0.222 | 0.000 | [0.000, 0.000] | 0.00 | 1.000 | low power |
| transgender | 4 | 1 | 0.250 | 0.250 | 0.000 | [0.000, 0.000] | 0.00 | 1.000 | low power |
| younger | 28 | 2 | 0.071 | 0.071 | 0.000 | [0.000, 0.000] | 0.00 | 1.000 | low power |

## Per-label F1 inside each slice

Each cell is the slice F1 with the number of positives that F1 rests on in brackets. `n/a` means the slice holds no positives for that label, which is not a score of zero. The `overall` row is the same metric over the whole held-out set.

A slice macro-F1 averages only the labels that slice has positives for, so it is compared against the overall macro-F1 recomputed over **those same labels** — the `overall` row's own macro covers a wider label set and is not the right subtrahend.

| term | toxic | severe_toxic | obscene | threat | insult | identity_hate | macro-F1 | overall, same labels | delta |
|---|---|---|---|---|---|---|---|---|---|
| overall | 0.771 (3135) | 0.422 (286) | 0.786 (1777) | 0.429 (99) | 0.727 (1659) | 0.459 (313) | 0.599 | - | - |
| queer | 0.769 (5) | 0.667 (1) | 0.667 (3) | 1.000 (2) | 0.667 (4) | 0.500 (3) | 0.712 | 0.599 | 0.112 |
| homosexual | 0.714 (10) | 0.667 (2) | 0.588 (7) | n/a (0) | 0.588 (7) | 0.538 (7) | 0.619 | 0.633 | -0.014 |
| heterosexual | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a | n/a | n/a |
| gay | 0.810 (95) | 0.421 (10) | 0.760 (46) | 0.571 (2) | 0.754 (58) | 0.604 (54) | 0.653 | 0.599 | 0.054 |
| teenage | 0.500 (2) | 1.000 (1) | 1.000 (1) | n/a (0) | 1.000 (1) | n/a (0) | 0.875 | 0.676 | 0.199 |
| bisexual | 0.667 (1) | n/a (0) | n/a (0) | n/a (0) | 0.000 (1) | 0.000 (1) | 0.222 | 0.652 | -0.430 |
| female | 0.500 (6) | 0.667 (1) | 0.857 (3) | n/a (0) | 0.667 (3) | 0.286 (2) | 0.595 | 0.633 | -0.038 |
| male | 0.462 (4) | 1.000 (1) | 0.400 (2) | n/a (0) | 1.000 (1) | n/a (0) | 0.715 | 0.676 | 0.039 |
| woman | 0.455 (9) | n/a (0) | 0.333 (7) | n/a (0) | 0.333 (3) | 0.500 (2) | 0.405 | 0.686 | -0.280 |
| black | 0.583 (33) | 0.462 (4) | 0.812 (15) | 0.571 (2) | 0.778 (17) | 0.392 (12) | 0.600 | 0.599 | 0.001 |
| lesbian | 0.833 (6) | n/a (0) | 0.800 (2) | n/a (0) | 0.444 (4) | 0.364 (3) | 0.610 | 0.686 | -0.075 |
| straight | 0.640 (10) | 0.000 (1) | 0.600 (4) | n/a (0) | 0.667 (6) | 0.500 (3) | 0.481 | 0.633 | -0.152 |
| asian | 0.706 (8) | 0.500 (1) | 0.909 (5) | n/a (0) | 0.750 (3) | 0.600 (4) | 0.693 | 0.633 | 0.060 |
| jew | 0.791 (22) | 0.800 (5) | 0.889 (9) | n/a (0) | 0.609 (13) | 0.520 (16) | 0.722 | 0.633 | 0.089 |
| sikh | 0.000 (1) | n/a (0) | 1.000 (1) | n/a (0) | 1.000 (1) | n/a (0) | 0.667 | 0.761 | -0.095 |
| man | 0.722 (79) | 0.455 (8) | 0.800 (46) | 0.333 (1) | 0.750 (41) | 0.412 (8) | 0.579 | 0.599 | -0.020 |
| disabled | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a | n/a | n/a |
| men | 0.629 (16) | 0.000 (2) | 0.308 (5) | n/a (0) | 0.400 (3) | 0.462 (3) | 0.360 | 0.633 | -0.273 |
| islam | 0.222 (4) | n/a (0) | 0.000 (2) | n/a (0) | 0.667 (2) | 0.500 (2) | 0.347 | 0.686 | -0.338 |
| christian | 0.500 (12) | 1.000 (3) | 0.615 (5) | n/a (0) | 0.714 (7) | 0.000 (3) | 0.566 | 0.633 | -0.067 |
| women | 0.732 (19) | 0.667 (1) | 0.632 (8) | n/a (0) | 0.615 (7) | 0.308 (3) | 0.591 | 0.633 | -0.042 |
| atheist | 0.857 (3) | n/a (0) | 0.400 (2) | 0.000 (1) | 0.500 (2) | n/a (0) | 0.439 | 0.678 | -0.239 |
| white | 0.750 (22) | 0.545 (5) | 0.759 (12) | n/a (0) | 0.667 (10) | 0.424 (8) | 0.629 | 0.633 | -0.004 |
| muslim | 0.636 (12) | 0.667 (2) | 0.889 (4) | n/a (0) | 0.600 (7) | 0.500 (7) | 0.658 | 0.633 | 0.025 |
| african | 0.533 (8) | 1.000 (1) | 0.500 (2) | n/a (0) | 0.500 (4) | 0.444 (2) | 0.596 | 0.633 | -0.037 |
| hindu | 0.000 (4) | n/a (0) | 1.000 (1) | n/a (0) | 1.000 (1) | n/a (0) | 0.667 | 0.761 | -0.095 |
| jewish | 0.444 (10) | n/a (0) | 0.857 (4) | n/a (0) | 0.571 (5) | 0.258 (4) | 0.533 | 0.686 | -0.153 |
| blind | 0.833 (6) | n/a (0) | 1.000 (2) | n/a (0) | 0.667 (3) | n/a (0) | 0.833 | 0.761 | 0.072 |
| indian | 0.737 (10) | 0.667 (3) | 0.667 (6) | n/a (0) | 0.727 (6) | 0.750 (4) | 0.709 | 0.633 | 0.077 |
| american | 0.711 (23) | 0.667 (2) | 0.522 (12) | n/a (0) | 0.667 (13) | 0.400 (6) | 0.593 | 0.633 | -0.040 |
| japanese | 0.667 (2) | n/a (0) | 0.667 (1) | n/a (0) | 0.667 (2) | 0.000 (1) | 0.500 | 0.686 | -0.186 |
| chinese | 0.727 (12) | 1.000 (1) | 0.667 (4) | 1.000 (1) | 0.727 (7) | 0.400 (5) | 0.754 | 0.599 | 0.154 |
| catholic | 0.500 (2) | n/a (0) | n/a (0) | n/a (0) | 1.000 (1) | 0.000 (1) | 0.500 | 0.652 | -0.152 |
| irish | 0.667 (7) | n/a (0) | 0.800 (3) | 0.667 (2) | 0.571 (5) | 0.000 (2) | 0.541 | 0.634 | -0.093 |
| arab | 0.667 (14) | 0.667 (1) | 0.600 (7) | n/a (0) | 0.571 (4) | 0.667 (5) | 0.634 | 0.633 | 0.001 |
| buddhist | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a | n/a | n/a |
| canadian | 0.857 (4) | n/a (0) | 0.667 (2) | n/a (0) | 0.800 (3) | n/a (0) | 0.775 | 0.761 | 0.013 |
| deaf | 0.800 (3) | n/a (0) | n/a (0) | n/a (0) | 1.000 (1) | n/a (0) | 0.900 | 0.749 | 0.151 |
| elderly | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a | n/a | n/a |
| european | 0.800 (6) | 0.000 (1) | 0.667 (2) | n/a (0) | 0.800 (2) | 0.667 (2) | 0.587 | 0.633 | -0.046 |
| hispanic | 0.000 (1) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | 0.000 | 0.771 | -0.771 |
| immigrant | 1.000 (1) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | 1.000 (1) | 1.000 | 0.615 | 0.385 |
| latina | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a | n/a | n/a |
| latino | 1.000 (1) | n/a (0) | n/a (0) | n/a (0) | 0.000 (1) | n/a (0) | 0.500 | 0.749 | -0.249 |
| lgbt | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a | n/a | n/a |
| mexican | 1.000 (1) | n/a (0) | 1.000 (1) | 1.000 (1) | 1.000 (1) | 0.500 (1) | 0.900 | 0.634 | 0.266 |
| middle eastern | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a | n/a | n/a |
| mormon | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a | n/a | n/a |
| older | 0.750 (5) | n/a (0) | 0.667 (4) | n/a (0) | 1.000 (2) | n/a (0) | 0.806 | 0.761 | 0.044 |
| protestant | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a (0) | n/a | n/a | n/a |
| refugee | 1.000 (1) | n/a (0) | 1.000 (1) | n/a (0) | 0.000 (1) | 0.000 (1) | 0.500 | 0.686 | -0.186 |
| trans | 1.000 (2) | 1.000 (1) | 0.667 (2) | n/a (0) | 0.667 (2) | n/a (0) | 0.833 | 0.676 | 0.157 |
| transgender | 1.000 (1) | n/a (0) | n/a (0) | n/a (0) | 1.000 (1) | 0.000 (1) | 0.667 | 0.652 | 0.014 |
| younger | 1.000 (2) | 0.000 (1) | 1.000 (2) | n/a (0) | 1.000 (1) | n/a (0) | 0.750 | 0.676 | 0.074 |

## Limitations

- The original six-label Jigsaw corpus carries **no identity annotations**. A term slice
  is a proxy for a demographic, and a noisy one: it captures who is *talked about*, not
  who is speaking, and it misses every mention that uses no listed term. Slices overlap
  and do not partition the test set.
- Term presence is not group membership. A slice mixes self-description, third-person
  discussion, and quotation, and the model may be reacting to any of them.
- Under-powered groups are reported with wide intervals rather than dropped, because
  dropping them is how the worst-affected group disappears from a fairness report.
- Per-label F1 inside a slice rests on very few positives for the rare labels; the
  bracketed positive count is there so a headline gap is read against the count that
  produced it.
- This report names which metrics fail and by how much, and issues **no fair / not fair verdict**.
  Demographic parity and equal opportunity cannot both hold when base rates differ, so
  choosing which to honour is a deployer decision, not a measurement.
