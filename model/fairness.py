"""Per-identity-term fairness slice of the held-out test set (premortem H31).

Jigsaw's documented unintended bias is that comments which merely MENTION an identity group are
over-flagged. The number that captures it is the false-positive rate among the NON-TOXIC rows of
each term slice, measured against the background non-toxic false-positive rate. A slice whose
flag rate is high because it genuinely contains more toxicity is not the same failure, so the
report separates the two: `selection_rate` is what the moderation queue feels, `fpr` is what an
innocent author feels, and `base_rate` is what makes the difference between them legible.

Method notes that belong in the model card, not just here:

- The original six-label Jigsaw corpus carries **no identity annotations**, so a slice is a term
  match, not a demographic. A term slice is a proxy and it is a noisy one: it captures who is
  *talked about*, not who is speaking, and it misses every mention that uses no listed term.
- Slices overlap and are not a partition. A comment matching three terms appears in three slices.
- Groups below `min_group_size` are reported with a `low_power` flag, never dropped. Dropping
  them is how the worst-affected group disappears from a fairness report.
- Every rate carries a bootstrap interval, and the per-slice PR-AUC uses the positive-preserving
  stratified bootstrap from `model.evaluate`, because a term slice can hold only a handful of
  toxic rows and a naive resample of those silently scores 0.0.
- Per-label F1 is reported inside each slice against the overall per-label F1, so a slice that is
  being over-flagged on one label (`identity_hate` is the usual one) cannot hide inside an
  aggregate.
- The report names which metrics move and by how much. It issues **no fair / not fair verdict**:
  demographic parity and equal opportunity cannot both hold when base rates differ, so choosing
  which to honour is a deployer decision, not a measurement.

`background_fpr` is `float('nan')` — not `None` — when the evaluated set contains no non-toxic
rows at all, because it is logged as a scalar metric downstream. Per-slice and per-label rates
use `None` for undefined, because they are rendered into a table rather than logged.
"""

import re
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path

import numpy as np
from sklearn.metrics import average_precision_score, f1_score

from model.labels import LABELS

# Adapted from the identity-term list in Dixon et al. 2018, "Measuring and Mitigating Unintended
# Bias in Text Classification": their axes (sexuality, gender, race and nationality, religion,
# age, disability) with the terms that do not appear in this corpus dropped and a few common
# Wikipedia-talk descriptors added. Descriptors only, never slurs, so the repository stays
# publishable; slur-bearing comments are already covered by the `identity_hate` label itself.
# Measured on 31,877 real Jigsaw comments: 56 of these 57 terms occur at least once, 26 of them
# in fewer than 30 rows -- which is why the low-power path below is load-bearing, not defensive.
IDENTITY_TERMS: tuple[str, ...] = (
    "atheist", "queer", "gay", "lesbian", "transgender", "trans", "bisexual", "homosexual",
    "heterosexual", "straight", "lgbt", "lgbtq", "nonbinary", "female", "male", "woman",
    "women", "man", "men", "black", "white", "african", "asian", "latino", "latina",
    "hispanic", "mexican", "indian", "chinese", "japanese", "arab", "middle eastern",
    "immigrant", "refugee", "american", "canadian", "european", "irish", "muslim", "islam",
    "jewish", "jew", "christian", "catholic", "protestant", "buddhist", "hindu", "sikh",
    "mormon", "deaf", "blind", "disabled", "paralyzed", "elderly", "older", "younger",
    "teenage",
)

PRIMARY_LABEL: str = "toxic"
FAIRNESS_REPORT_PATH = Path("docs/fairness-report.md")
_SEP = "[^a-z0-9]"


@dataclass(frozen=True)
class TermSlice:
    term: str
    n: int
    n_pos: int
    n_neg: int
    base_rate: float
    selection_rate: float
    fpr: float | None
    fpr_lo: float | None
    fpr_hi: float | None
    tpr: float | None
    pr_auc: float | None
    pr_auc_lo: float | None
    pr_auc_hi: float | None
    fpr_ratio_to_background: float | None
    low_power: bool
    per_label: dict[str, dict]
    macro_f1: float | None
    macro_f1_overall: float | None
    macro_f1_gap: float | None


def _stratified_bootstrap_ci():
    """`model.evaluate.stratified_bootstrap_ci`, imported at call time on purpose.

    `model.evaluate` imports this module to attach a fairness slice to the held-out evaluation.
    A module-level import in the other direction closes the cycle, and Python then raises
    `ImportError: cannot import name ...` or not, depending purely on which module the process
    happened to import first — a failure that appears in one entrypoint and not another. The
    deferred import costs one dict lookup per report and cannot be order-dependent.
    """
    from model.evaluate import stratified_bootstrap_ci

    return stratified_bootstrap_ci


def _rate_ci(
    indicator: np.ndarray, *, n_boot: int, seed: int, alpha: float = 0.05
) -> tuple[float | None, float | None]:
    """Percentile bootstrap interval for a rate (flag rate, false-positive rate).

    Resampling n Bernoulli values with replacement and taking the mean is distributed exactly
    Binomial(n, p_hat) / n, so drawing the binomial directly *is* the nonparametric bootstrap
    for a proportion rather than an approximation of it. It costs O(n_boot) instead of the
    O(n_boot x n) index matrix a naive resampler materialises, which matters here because the
    report bootstraps every one of ~56 term slices on every training run.

    `model.evaluate` deliberately owns no rate interval: its `stratified_bootstrap_ci` needs a
    positive stratum, and a false-positive rate is measured over the rows that have none.
    """
    x = np.asarray(indicator, dtype=float).ravel()
    n = x.size
    if n == 0:
        return None, None
    point = float(x.mean())
    replicates = np.random.default_rng(seed).binomial(n, point, size=n_boot) / n
    lo, hi = (float(v) for v in np.quantile(replicates, [alpha / 2.0, 1.0 - alpha / 2.0]))
    return lo, hi


@cache
def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?:^|{_SEP}){re.escape(term)}(?:{_SEP}|$)")


def _lowered(texts) -> list[str]:
    return ["" if t is None else str(t).lower() for t in texts]


def _mask_lowered(lowered: list[str], term: str) -> np.ndarray:
    """`term in text` is a pure prefilter, not a second rule.

    The compiled pattern is separator + the escaped literal + separator, so it cannot match a
    text that does not contain the literal; the cheap C-level containment check therefore only
    skips rows the regex would have rejected anyway. Measured on 31,877 real Jigsaw comments,
    the 57-term scan drops from 27.6 s to 1.7 s with byte-identical masks, which is the
    difference between a fairness report that runs on every training run and one that gets
    skipped.
    """
    pattern = _pattern(term)
    return np.fromiter(
        (term in text and pattern.search(text) is not None for text in lowered),
        dtype=bool,
        count=len(lowered),
    )


def term_mask(texts, term: str) -> np.ndarray:
    """Word-boundary-ish match so `man` does not match `woman` and `trans` misses `transgender`.

    Separators are "anything that is not a lowercase letter or a digit", which keeps hyphenated
    and punctuated mentions (`anti-muslim`) inside the slice while keeping substrings out.
    """
    return _mask_lowered(_lowered(texts), term)


def _as_matrix(values, name: str, *, dtype) -> np.ndarray:
    arr = np.asarray(values, dtype=dtype)
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    if arr.ndim != 2:
        raise ValueError(f"{name} must be 1-D or 2-D, got shape {arr.shape}")
    return arr


def _resolve_labels(n_cols: int, labels, primary_label: str) -> list[str]:
    if labels is not None:
        resolved = list(labels)
    elif n_cols == 1:
        resolved = [primary_label]
    elif n_cols == len(LABELS):
        resolved = list(LABELS)
    else:
        raise ValueError(
            f"cannot infer label names for {n_cols} columns; pass labels=(...) explicitly"
        )
    if len(resolved) != n_cols:
        raise ValueError(f"labels has {len(resolved)} names for {n_cols} columns")
    if primary_label not in resolved:
        raise ValueError(f"primary_label {primary_label!r} is not among {resolved}")
    return resolved


def _rate(indicator: np.ndarray) -> float | None:
    return float(indicator.mean()) if indicator.size else None


def _label_metrics(y_true: np.ndarray, y_flag: np.ndarray) -> dict:
    """Flag rate, false-positive rate, and F1 for one label over one set of rows.

    F1 is `None`, never 0.0, when the rows carry no positives for this label: a zero there means
    "undefined", and printing it as a score is how an empty slice gets read as a failing one.
    """
    n_pos = int((y_true == 1).sum())
    return {
        "n": int(y_true.size),
        "n_pos": n_pos,
        "flag_rate": _rate(y_flag),
        "fpr": _rate(y_flag[y_true == 0]),
        "tpr": _rate(y_flag[y_true == 1]),
        "f1": float(f1_score(y_true, y_flag, zero_division=0)) if n_pos else None,
    }


def identity_fairness_report(
    texts,
    y_true,
    y_flag,
    y_prob,
    *,
    terms: tuple[str, ...] = IDENTITY_TERMS,
    labels: tuple[str, ...] | None = None,
    primary_label: str = PRIMARY_LABEL,
    min_group_size: int = 30,
    seed: int = 42,
    n_boot: int = 1000,
    material_gap: float = 0.10,
) -> dict:
    """Slice the held-out set by identity-term presence and measure each slice against the whole.

    `y_true`, `y_flag` and `y_prob` are either the 1-D vectors of the `primary_label` column or
    the full (n, 6) matrices. Passing the full matrices is what turns on the per-label F1 table.
    """
    y_true = _as_matrix(y_true, "y_true", dtype=int)
    y_flag = _as_matrix(y_flag, "y_flag", dtype=int)
    y_prob = _as_matrix(y_prob, "y_prob", dtype=float)
    lowered = _lowered(texts)
    n_rows = len(lowered)
    shapes = {"y_true": y_true.shape, "y_flag": y_flag.shape, "y_prob": y_prob.shape}
    if any(shape[0] != n_rows for shape in shapes.values()):
        raise ValueError(
            f"texts and every target must have the same number of rows: texts={n_rows}, "
            + ", ".join(f"{name}={shape[0]}" for name, shape in shapes.items())
        )
    if len({shape[1] for shape in shapes.values()}) != 1:
        raise ValueError(f"y_true, y_flag and y_prob must have the same width: {shapes}")

    label_names = _resolve_labels(y_true.shape[1], labels, primary_label)
    primary = label_names.index(primary_label)
    stratified_bootstrap_ci = _stratified_bootstrap_ci()

    overall = {
        label: _label_metrics(y_true[:, j], y_flag[:, j]) for j, label in enumerate(label_names)
    }
    overall_f1 = [overall[label]["f1"] for label in label_names if overall[label]["f1"] is not None]
    background = y_true[:, primary] == 0
    background_fpr = (
        float(y_flag[background, primary].mean()) if background.any() else float("nan")
    )

    slices: list[TermSlice] = []
    absent: list[str] = []
    for term in terms:
        mask = _mask_lowered(lowered, term)
        n = int(mask.sum())
        if n == 0:
            # Omitted from the table rather than reported as a slice of zero rows, but named in
            # `terms_absent` so a silently missing group is distinguishable from an unsearched one.
            absent.append(term)
            continue
        yt = y_true[mask, primary]
        yf = y_flag[mask, primary]
        yp = y_prob[mask, primary]
        neg = yt == 0
        pos = yt == 1
        fpr = _rate(yf[neg])
        fpr_lo, fpr_hi = _rate_ci(yf[neg], n_boot=n_boot, seed=seed)
        ap_ci = (
            stratified_bootstrap_ci(yt, yp, average_precision_score, n_boot=n_boot, seed=seed)
            if pos.any() and neg.any()
            else None
        )

        per_label: dict[str, dict] = {}
        for j, label in enumerate(label_names):
            cell = _label_metrics(y_true[mask, j], y_flag[mask, j])
            cell["f1_overall"] = overall[label]["f1"]
            cell["f1_gap"] = (
                cell["f1"] - overall[label]["f1"]
                if cell["f1"] is not None and overall[label]["f1"] is not None
                else None
            )
            per_label[label] = cell

        # Compare like with like: the slice macro-F1 covers only the labels that have positives
        # inside the slice, so the overall macro it is compared against covers exactly those too.
        scored_labels = [label for label in label_names if per_label[label]["f1"] is not None]
        macro_f1 = (
            float(np.mean([per_label[label]["f1"] for label in scored_labels]))
            if scored_labels
            else None
        )
        macro_f1_overall = (
            float(np.mean([overall[label]["f1"] for label in scored_labels]))
            if scored_labels
            else None
        )
        slices.append(
            TermSlice(
                term=term,
                n=n,
                n_pos=int(pos.sum()),
                n_neg=int(neg.sum()),
                base_rate=float(yt.mean()),
                selection_rate=float(yf.mean()),
                fpr=fpr,
                fpr_lo=fpr_lo,
                fpr_hi=fpr_hi,
                tpr=_rate(yf[pos]),
                pr_auc=(ap_ci.point if ap_ci else None),
                pr_auc_lo=(ap_ci.lo if ap_ci else None),
                pr_auc_hi=(ap_ci.hi if ap_ci else None),
                fpr_ratio_to_background=(
                    fpr / background_fpr if fpr is not None and background_fpr > 0 else None
                ),
                low_power=n < min_group_size,
                per_label=per_label,
                macro_f1=macro_f1,
                macro_f1_overall=macro_f1_overall,
                macro_f1_gap=(
                    macro_f1 - macro_f1_overall if macro_f1 is not None else None
                ),
            )
        )

    scored = [s for s in slices if s.fpr is not None and not s.low_power]
    max_fpr_gap = max((s.fpr - background_fpr for s in scored), default=0.0)
    worst = max(scored, key=lambda s: s.fpr, default=None)
    rates = [s.selection_rate for s in scored]
    f1_scored = [s for s in scored if s.macro_f1_gap is not None]
    worst_f1 = min(f1_scored, key=lambda s: s.macro_f1_gap, default=None)
    if worst_f1 is not None and worst_f1.macro_f1_gap >= 0.0:
        # Every scored slice does at least as well as the whole set. Naming a "worst" term here
        # would invent a finding out of the ordinary spread between slices.
        worst_f1 = None
    return {
        "background_fpr": background_fpr,
        "background_flag_rate": float(y_flag[:, primary].mean()) if n_rows else float("nan"),
        "n_rows": n_rows,
        "n_terms_checked": len(terms),
        "n_terms_present": len(slices),
        "terms_absent": absent,
        "n_terms_scored": len(scored),
        "n_terms_low_power": len(slices) - len(scored),
        "max_fpr_gap": float(max_fpr_gap),
        "worst_term": (worst.term if worst else None),
        "max_f1_drop": (
            float(max(0.0, -worst_f1.macro_f1_gap)) if worst_f1 is not None else 0.0
        ),
        "worst_f1_term": (worst_f1.term if worst_f1 is not None else None),
        "four_fifths_ratio": (min(rates) / max(rates) if rates and max(rates) > 0 else None),
        "material": bool(max_fpr_gap > material_gap),
        "min_group_size": min_group_size,
        "primary_label": primary_label,
        "labels": label_names,
        "overall": overall,
        "overall_macro_f1": float(np.mean(overall_f1)) if overall_f1 else None,
        "n_boot": n_boot,
        "seed": seed,
        "slices": [asdict(s) for s in slices],
    }


def _fmt(value: float | None, places: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def _f1_cell(cell: dict) -> str:
    return f"{_fmt(cell['f1'], 3)} ({cell['n_pos']})"


def _absent_summary(absent: list[str], *, limit: int = 12) -> str:
    if not absent:
        return "none"
    head = ", ".join(absent[:limit])
    return head if len(absent) <= limit else f"{head}, +{len(absent) - limit} more"


def render_fairness_markdown(report: dict) -> str:
    ordered = sorted(report["slices"], key=lambda s: (-(s["fpr"] or 0.0), s["term"]))
    lines = [
        "# Fairness: per-identity-term slice of the held-out test set",
        "",
        "Jigsaw's documented unintended bias is that comments which merely **mention** an identity",
        "group are over-flagged. The number that captures it is the false-positive rate among the",
        "**non-toxic** rows of each term slice, against the background non-toxic flag rate.",
        "",
        f"- rows evaluated: {report['n_rows']}",
        f"- primary label for flag rate and FPR: `{report['primary_label']}`",
        f"- background non-toxic flag rate: **{_fmt(report['background_fpr'])}**",
        f"- terms searched: {report['n_terms_checked']}",
        f"- terms present in the test set: {report['n_terms_present']}",
        f"- terms with enough rows to score (n >= {report['min_group_size']}): "
        f"{report['n_terms_scored']}",
        f"- terms reported but under-powered: {report['n_terms_low_power']}",
        f"- terms with no rows here, omitted from the table: "
        f"{_absent_summary(report['terms_absent'])}",
        f"- largest false-positive gap: **{_fmt(report['max_fpr_gap'])}** "
        f"({report['worst_term'] or 'n/a'})",
        f"- largest macro-F1 drop inside a scored slice: **{_fmt(report['max_f1_drop'])}** "
        f"({report['worst_f1_term'] or 'n/a'})",
        f"- four-fifths ratio across scored terms: {_fmt(report['four_fifths_ratio'])}",
        "",
        "| term | n | n_pos | base rate | flag rate | FPR | FPR 95% CI | FPR vs background | "
        "PR-AUC | note |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in ordered:
        note = "low power" if s["low_power"] else ""
        ci = f"[{_fmt(s['fpr_lo'], 3)}, {_fmt(s['fpr_hi'], 3)}]"
        lines.append(
            f"| {s['term']} | {s['n']} | {s['n_pos']} | {_fmt(s['base_rate'], 3)} | "
            f"{_fmt(s['selection_rate'], 3)} | {_fmt(s['fpr'], 3)} | {ci} | "
            f"{_fmt(s['fpr_ratio_to_background'], 2)} | {_fmt(s['pr_auc'], 3)} | {note} |"
        )

    labels = report["labels"]
    if len(labels) > 1:
        lines += [
            "",
            "## Per-label F1 inside each slice",
            "",
            "Each cell is the slice F1 with the number of positives that F1 rests on in brackets."
            " `n/a` means the slice holds no positives for that label, which is not a score of"
            " zero. The `overall` row is the same metric over the whole held-out set.",
            "",
            "A slice macro-F1 averages only the labels that slice has positives for, so it is"
            " compared against the overall macro-F1 recomputed over **those same labels** — the"
            " `overall` row's own macro covers a wider label set and is not the right subtrahend.",
            "",
            "| term | " + " | ".join(labels) + " | macro-F1 | overall, same labels | delta |",
            "|---" * (len(labels) + 4) + "|",
            "| overall | "
            + " | ".join(_f1_cell(report["overall"][label]) for label in labels)
            + f" | {_fmt(report['overall_macro_f1'], 3)} | - | - |",
        ]
        for s in ordered:
            cells = " | ".join(_f1_cell(s["per_label"][label]) for label in labels)
            lines.append(
                f"| {s['term']} | {cells} | {_fmt(s['macro_f1'], 3)} | "
                f"{_fmt(s['macro_f1_overall'], 3)} | {_fmt(s['macro_f1_gap'], 3)} |"
            )

    lines += [
        "",
        "## Limitations",
        "",
        "- The original six-label Jigsaw corpus carries **no identity annotations**. A term slice",
        "  is a proxy for a demographic, and a noisy one: it captures who is *talked about*, not",
        "  who is speaking, and it misses every mention that uses no listed term. Slices overlap",
        "  and do not partition the test set.",
        "- Term presence is not group membership. A slice mixes self-description, third-person",
        "  discussion, and quotation, and the model may be reacting to any of them.",
        "- Under-powered groups are reported with wide intervals rather than dropped, because",
        "  dropping them is how the worst-affected group disappears from a fairness report.",
        "- Per-label F1 inside a slice rests on very few positives for the rare labels; the",
        "  bracketed positive count is there so a headline gap is read against the count that",
        "  produced it.",
        "- This report names which metrics fail and by how much, and issues"
        " **no fair / not fair verdict**.",
        "  Demographic parity and equal opportunity cannot both hold when base rates differ, so",
        "  choosing which to honour is a deployer decision, not a measurement.",
    ]
    return "\n".join(lines) + "\n"


def write_fairness_report(path: Path, report: dict) -> Path:
    """Render and write `docs/fairness-report.md`, the model card's fairness section."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_fairness_markdown(report))
    return path
