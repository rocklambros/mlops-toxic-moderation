# Graded Panel Defects and Reviewer Route Exposure — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the drift panel comparing seeded rows against a baseline computed over those same rows, put a sample floor under the live-accuracy metric, and make the reviewer routes refuse a public peer.

**Architecture:** Three independent changes to running code, each one function plus one caller. No Terraform, no compose changes, no container moves, no IAM changes, no Secrets Manager operations. Rollback stays `make rollback`.

**Tech Stack:** Python 3.11, SQLAlchemy Core (`text()` with bound parameters), FastAPI/Starlette middleware, Streamlit, pytest.

## Global Constraints

- Line length 100. `ruff check .` must be clean before every commit.
- Run tests with `PYTHONHASHSEED=0`, matching the Makefile.
- SQL: label names may be interpolated (they come from `model.labels.LABELS`, a module-level tuple); **every caller-supplied value is a bound parameter**. Keep the `# nosemgrep: avoid-sqlalchemy-text` comments and their justifications.
- Do not change `app.state.rejected`'s key set — `/health` publishes it and the dashboard reads it.
- `docs/latency-baseline.md` is regenerated only by `make loadtest`. Do not commit changes to it from other runs.
- Never weaken an existing assertion to make a new change pass. If an existing test goes red, either the change is wrong or the test's claim moved — say which, in the commit message.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `monitoring/queries.py` | SQL layer. Owns `production_flag_rates`, `drift_report`, `DriftRow` | Modify |
| `monitoring/dashboard.py` | Pure caption/formatting helpers plus the Streamlit page | Modify |
| `backend/app.py` | The `_gate` middleware, which is the single place every abuse control lives | Modify |
| `tests/unit/test_drift_seeded_separation.py` | Task 1 behaviour | Create |
| `tests/unit/test_accuracy_floor.py` | Task 2 behaviour | Create |
| `tests/unit/test_review_peer_guard.py` | Task 3 behaviour | Create |
| `tests/integration/test_deployed_traversal.py` | Live proof the routes 404 from the internet | Modify |
| `SECURITY.md`, `docs/tls-decision.md` | Record the new posture | Modify (Task 4) |

---

### Task 1: The drift panel must not report a comparison against its own reference data

**Files:**
- Modify: `monitoring/queries.py` (`production_flag_rates` ~line 124, `DriftRow` ~line 92, `drift_report` ~line 145)
- Modify: `monitoring/dashboard.py` (`drift_caption` ~line 268)
- Test: `tests/unit/test_drift_seeded_separation.py` (create)

**Interfaces:**
- Consumes: `monitoring.queries.production_flag_rates(conn, since, thresholds)`, `DriftRow`, `MIN_DRIFT_SAMPLES`
- Produces: `production_flag_rates(conn, since, thresholds, seeded: bool | None = None) -> tuple[int, dict[str, float]]`; `DriftRow.live_n: int | None`; `drift_caption(alerting, alert_psi=None, n=None, live_n=None) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_drift_seeded_separation.py`:

```python
"""The drift panel compared the seed against a baseline computed over the same rows.

`make seed-demo` replays the locked held-out split through /predict, and
`baseline_flag_rates.json` is computed over that same split. So PSI was measured between a
distribution and itself: zero by construction, unable to move, and reported in the same
voice as a measurement -- "No label exceeds the PSI alert threshold of 0.2, over 2000
predictions in the drift window."

The fix is not to filter the seed out and stop. That takes the window from 2048 rows to 48
and mutes a graded panel. It is to carry both series and let the caption say which one is a
wiring check and which one is evidence.
"""

import datetime as dt

import pytest

from model.labels import LABELS
from monitoring.baseline import load_baseline, load_thresholds
from monitoring.dashboard import drift_caption
from monitoring.queries import MIN_DRIFT_SAMPLES, drift_report, production_flag_rates
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
THRESHOLDS = load_thresholds(FIXTURES / "thresholds.json")
BASELINE = load_baseline(FIXTURES / "baseline_flag_rates.json")
SINCE = dt.datetime(2026, 8, 16, tzinfo=dt.UTC)


class SplitWindow:
    """A `predictions` window that answers differently for the whole set and the live subset.

    It decides which to return by looking for the `is_seed` predicate in the SQL, so a change
    that stops emitting the predicate cannot pass by accident -- the fake would answer with
    the full-window row and the live counts would be wrong.
    """

    def __init__(self, total: int, live: int, flagged: dict[str, int] | None = None) -> None:
        self.total, self.live = total, live
        self.flagged = flagged or {}
        self.statements: list[str] = []
        self._last = ""

    def execute(self, statement, parameters=None):
        self._last = str(statement)
        self.statements.append(self._last)
        return self

    def mappings(self):
        return self

    def one(self):
        n = self.live if "NOT is_seed" in self._last else self.total
        row = {"n": n} | {f"flag_{label}": 0 for label in LABELS}
        for label, count in self.flagged.items():
            row[f"flag_{label}"] = min(count, n)
        return row


def test_the_live_filter_reaches_the_sql_and_changes_the_count():
    window = SplitWindow(total=2000, live=48)
    all_n, _ = production_flag_rates(window, SINCE, THRESHOLDS)
    live_n, _ = production_flag_rates(window, SINCE, THRESHOLDS, seeded=False)
    assert all_n == 2000
    assert live_n == 48
    assert any("NOT is_seed" in s for s in window.statements)


def test_the_unfiltered_call_emits_no_seed_predicate():
    """Backward compatibility: the default must be the query that already shipped."""
    window = SplitWindow(total=2000, live=48)
    production_flag_rates(window, SINCE, THRESHOLDS)
    assert not any("is_seed" in s for s in window.statements)


def test_drift_rows_carry_the_live_denominator_beside_the_full_one():
    window = SplitWindow(total=2000, live=48, flagged={"toxic": 200})
    rows = {row.label: row for row in drift_report(window, SINCE, THRESHOLDS, BASELINE)}
    assert rows["toxic"].n == 2000
    assert rows["toxic"].live_n == 48


def test_a_seed_dominated_window_is_not_reported_as_a_drift_finding():
    """The exact live shape on 2026-08-11: 2048 rows, 2000 of them replayed reference data."""
    caption = drift_caption([], alert_psi=0.2, n=2048, live_n=20)
    assert "replayed" in caption
    assert "20" in caption
    assert "No label exceeds" not in caption, (
        "a window whose rows ARE the reference distribution cannot support that claim"
    )


def test_a_window_with_enough_live_traffic_still_reports_normally():
    caption = drift_caption([], alert_psi=0.2, n=2048, live_n=MIN_DRIFT_SAMPLES + 1)
    assert "No label exceeds" in caption


def test_an_unseeded_window_is_unaffected():
    """live_n == n means nothing was replayed; the caption must not gain a caveat."""
    caption = drift_caption([], alert_psi=0.2, n=500, live_n=500)
    assert "No label exceeds" in caption
    assert "replayed" not in caption


def test_the_caption_still_handles_a_caller_that_records_no_live_count():
    caption = drift_caption([], alert_psi=0.2, n=500)
    assert "No label exceeds" in caption
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_drift_seeded_separation.py -q`
Expected: FAIL — `production_flag_rates() got an unexpected keyword argument 'seeded'`

- [ ] **Step 3: Add the filter to the query layer**

In `monitoring/queries.py`, replace `production_flag_rates` with:

```python
def production_flag_rates(
    conn, since: dt.datetime, thresholds: dict[str, float], seeded: bool | None = None
) -> tuple[int, dict[str, float]]:
    """Flag rates over the window. `seeded` selects which rows count.

    `None` is every row and is what the panel's headline series uses. `False` is live traffic
    only, and it exists because `make seed-demo` replays the locked held-out split -- the same
    split `baseline_flag_rates.json` was computed over. PSI between the seeded rows and that
    baseline is a comparison of a distribution with itself: zero by construction, and not a
    statement about production. The live subset is the only part of the window that can carry
    a drift finding.
    """
    predicate = {None: "", False: " AND NOT is_seed", True: " AND is_seed"}[seeded]
    row = conn.execute(
        # `_flag_sum_sql()` emits one `sum(...)` per label from LABELS and compares each
        # probability against a BOUND threshold placeholder; the caller's thresholds reach
        # the database through `_threshold_binds`, never through the string. `predicate` is
        # selected from a literal dict keyed on a bool, so it is not caller text either.
        # nosemgrep: avoid-sqlalchemy-text
        text(
            f"SELECT count(*) AS n, {_flag_sum_sql()} FROM predictions "
            f"WHERE ts >= :since{predicate}"
        ),
        {"since": since, **_threshold_binds(thresholds)},
    ).mappings().one()
    n = int(row["n"])
    if n == 0:
        # Placeholders, not measurements. The count is returned first precisely so a caller
        # cannot read these zeros as a flag rate: against a 9.6% baseline a genuine 0.0
        # scores PSI 1.11, which is a major-shift alert raised by an untouched database.
        return 0, {label: 0.0 for label in LABELS}
    return n, {label: float(row[f"flag_{label}"] or 0) / n for label in LABELS}
```

- [ ] **Step 4: Carry the live count on every drift row**

In `monitoring/queries.py`, add the field to `DriftRow` immediately after `n`:

```python
    n: int | None = None
    # How many of those `n` predictions were live traffic rather than rows `make seed-demo`
    # replayed. The seeded rows ARE the baseline's own sample, so a PSI computed over them
    # is a wiring check. `live_n` is what a drift claim has to be measured over.
    live_n: int | None = None
```

In `drift_report`, replace the single call with both, and pass the new field:

```python
    n, production = production_flag_rates(conn, since, thresholds)
    live_n, _ = production_flag_rates(conn, since, thresholds, seeded=False)
```

and add `live_n=live_n,` to the `DriftRow(...)` construction, directly after `n=n,`.

- [ ] **Step 5: Teach the caption the difference between a control and a measurement**

In `monitoring/dashboard.py`, change the signature and add one branch. Replace:

```python
def drift_caption(
    alerting: list[str], alert_psi: float | None = None, n: int | None = None
) -> str:
```

with:

```python
def drift_caption(
    alerting: list[str],
    alert_psi: float | None = None,
    n: int | None = None,
    live_n: int | None = None,
) -> str:
```

Then, immediately after the existing `n < MIN_DRIFT_SAMPLES` branch and before the `stated = ...` line, insert:

```python
    # A window dominated by replayed rows cannot carry a drift finding in either direction.
    # `make seed-demo` replays the locked held-out split, and baseline_flag_rates.json was
    # computed over that same split, so PSI across those rows compares a distribution with
    # itself. Reporting "no label exceeds the threshold" over them states a conclusion the
    # comparison is structurally incapable of reaching.
    if live_n is not None and n is not None and live_n < int(n):
        if int(live_n) < MIN_DRIFT_SAMPLES:
            return (
                f"{int(n)} predictions in the drift window, but {int(n) - int(live_n)} of "
                f"them are replayed held-out rows that the baseline was itself computed "
                f"over -- comparing those against it is a wiring check, not a measurement. "
                f"Only {int(live_n)} are live traffic, fewer than the {MIN_DRIFT_SAMPLES} "
                f"this panel requires, so no drift finding is claimed. The bars below are "
                f"the full window."
            )
```

- [ ] **Step 6: Pass the live count at the call site**

In `monitoring/dashboard.py`, find the drift panel's caption call:

```python
    st.caption(drift_caption(alerting_labels(data), n=drift_sample_size(data)))
```

Add a `live` sibling to `drift_sample_size`. Immediately after that function's definition, add:

```python
def drift_live_sample_size(data: "Snapshot") -> int | None:
    """The live half of the drift denominator, or None if the rows did not record one."""
    return next((row.live_n for row in data.drift), None)
```

and change the caption call to:

```python
    st.caption(
        drift_caption(
            alerting_labels(data),
            n=drift_sample_size(data),
            live_n=drift_live_sample_size(data),
        )
    )
```

- [ ] **Step 7: Run the new tests**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_drift_seeded_separation.py -q`
Expected: 7 passed

- [ ] **Step 8: Run every existing drift and dashboard test**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_drift.py tests/unit/test_drift_small_sample.py tests/unit/test_dashboard.py tests/unit/test_dashboard_guards.py tests/unit/test_dashboard_window.py -q`
Expected: all pass. `FakeWindow` in `test_drift_small_sample.py` returns the same row for both calls, so `live_n == n` there and the new branch does not fire — which is correct, since nothing in that file is seeded.

- [ ] **Step 9: Lint and commit**

```bash
.venv/bin/ruff check .
git add monitoring/queries.py monitoring/dashboard.py tests/unit/test_drift_seeded_separation.py
git commit -m "Stop the drift panel comparing the seed against its own baseline

production_flag_rates read FROM predictions WHERE ts >= :since with no is_seed
predicate, while baseline_flag_rates.json is computed over the locked held-out
split that make seed-demo replays. 2000 of the window's 2048 rows are that split,
so PSI was measured between a distribution and itself -- zero by construction,
unable to move, and printed as 'No label exceeds the PSI alert threshold of 0.2,
over 2000 predictions in the drift window'.

Filtering the seed out and stopping would take the window to 48 rows and mute a
graded panel, so both series are carried instead: the full window still draws the
bars, and live_n travels on every row as the denominator a drift claim has to be
measured over. The caption now refuses to state a finding when the window is
seed-dominated and the live subset is under the floor."
```

---

### Task 2: A sample floor under the number a grader reads first

**Files:**
- Modify: `monitoring/dashboard.py` (constants block ~line 88, accuracy panel ~line 452)
- Test: `tests/unit/test_accuracy_floor.py` (create)

**Interfaces:**
- Consumes: `monitoring.stats.AccuracyReport` with fields `n: int`, `point: float | None`, `lo`, `hi`, `effective_n: float`, `strata: list[StratumStat]`
- Produces: `monitoring.dashboard.MIN_REVIEWED_FOR_ESTIMATE: int`, `accuracy_is_reportable(report: AccuracyReport) -> bool`, `accuracy_floor_notice(report: AccuracyReport) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_accuracy_floor.py`:

```python
"""Panel 3 renders the one number a grader reads off the screenshot, and had no floor.

Panel 1 requires 20 samples per bucket. Panel 2 requires 30 plus an exact binomial tail
test. Panel 3 gated its headline st.metric on `point is not None` and nothing else, so a
single reviewed row scored correct renders 100.0% in the largest type on the page. The
Wilson interval in the caption bounds that honestly, but a caption is not what a screenshot
shows.
"""

from monitoring.dashboard import (
    MIN_REVIEWED_FOR_ESTIMATE,
    accuracy_floor_notice,
    accuracy_is_reportable,
)
from monitoring.stats import AccuracyReport, StratumStat


def _report(n: int, point: float | None = 1.0) -> AccuracyReport:
    return AccuracyReport(
        n=n,
        point=point,
        lo=0.207 if point is not None else None,
        hi=1.0 if point is not None else None,
        effective_n=float(n),
        strata=[
            StratumStat(
                stratum="flagged",
                n=n,
                correct=n if point else 0,
                sample_rate=1.0,
                accuracy=point,
                lo=0.207 if point is not None else None,
                hi=1.0 if point is not None else None,
            )
        ],
    )


def test_one_perfect_review_does_not_render_a_headline_accuracy():
    assert accuracy_is_reportable(_report(n=1)) is False


def test_the_floor_matches_the_drift_panel_beside_it():
    assert MIN_REVIEWED_FOR_ESTIMATE == 30


def test_exactly_the_floor_is_reportable():
    assert accuracy_is_reportable(_report(n=MIN_REVIEWED_FOR_ESTIMATE)) is True


def test_one_below_the_floor_is_not():
    assert accuracy_is_reportable(_report(n=MIN_REVIEWED_FOR_ESTIMATE - 1)) is False


def test_the_current_live_volume_is_unaffected():
    """643 reviewed items on 2026-08-11. This guard must not change what is on screen now."""
    assert accuracy_is_reportable(_report(n=643)) is True


def test_no_estimate_at_all_is_not_reportable():
    assert accuracy_is_reportable(_report(n=0, point=None)) is False


def test_the_notice_names_both_numbers_so_the_gap_is_actionable():
    notice = accuracy_floor_notice(_report(n=7))
    assert "7" in notice
    assert str(MIN_REVIEWED_FOR_ESTIMATE) in notice
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_accuracy_floor.py -q`
Expected: FAIL — `ImportError: cannot import name 'MIN_REVIEWED_FOR_ESTIMATE'`

- [ ] **Step 3: Add the constant and the two pure helpers**

In `monitoring/dashboard.py`, beside the existing `MIN_SAMPLES_PER_BUCKET` constant, add:

```python
# The floor Panel 3 did not have. Matched to MIN_DRIFT_SAMPLES rather than chosen freshly:
# both answer the same question -- how many observations before a proportion is reported as
# a finding -- and two different numbers for one question invite the smaller to be quoted.
#
# Counted on reviewed rows, not on the Kish effective size. `effective_n` falls below the raw
# count when the design weights are uneven, which would make the floor bite hardest exactly
# when the random-audit stratum is doing its job.
MIN_REVIEWED_FOR_ESTIMATE = 30
```

Then, next to `accuracy_caption`, add:

```python
def accuracy_is_reportable(report: AccuracyReport) -> bool:
    """Whether the headline metric may be drawn, as opposed to the caption and the strata."""
    return report.point is not None and int(report.n) >= MIN_REVIEWED_FOR_ESTIMATE


def accuracy_floor_notice(report: AccuracyReport) -> str:
    return (
        f"{int(report.n)} reviewed item(s), fewer than the {MIN_REVIEWED_FOR_ESTIMATE} this "
        f"panel requires before it prints a headline accuracy. The per-stratum detail and "
        f"the confidence interval are below; the point estimate is withheld because at this "
        f"size it is one reviewer's opinion rendered as a percentage."
    )
```

`AccuracyReport` is already imported at `monitoring/dashboard.py:79`; no import change is needed.

- [ ] **Step 4: Use them in the panel**

In `monitoring/dashboard.py`, replace:

```python
    st.header("3. Live accuracy from human feedback")
    if data.accuracy.point is not None:
        st.metric("Live accuracy (design-weighted)", accuracy_metric(data.accuracy.point))
```

with:

```python
    st.header("3. Live accuracy from human feedback")
    if accuracy_is_reportable(data.accuracy):
        st.metric("Live accuracy (design-weighted)", accuracy_metric(data.accuracy.point))
    elif data.accuracy.point is not None:
        st.info(accuracy_floor_notice(data.accuracy))
```

- [ ] **Step 5: Run the new tests**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_accuracy_floor.py -q`
Expected: 7 passed

- [ ] **Step 6: Run the dashboard suite**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_dashboard.py tests/unit/test_dashboard_guards.py tests/unit/test_dashboard_window.py -q`
Expected: all pass

- [ ] **Step 7: Lint and commit**

```bash
.venv/bin/ruff check .
git add monitoring/dashboard.py tests/unit/test_accuracy_floor.py
git commit -m "Put a sample floor under the live-accuracy metric

Panel 1 requires 20 samples per bucket and Panel 2 requires 30 plus a binomial
tail test. Panel 3 -- the one number a grader reads off the screenshot -- gated
its st.metric on 'point is not None' and nothing else, so one reviewed row scored
correct renders 100.0% in the largest type on the page. The Wilson interval said
so honestly in the caption, but the caption is not what a screenshot shows.

Floor is 30, matched to MIN_DRIFT_SAMPLES rather than picked fresh, and counted on
reviewed rows rather than the Kish effective size -- effective_n drops when the
design weights are uneven, which would make the floor bite hardest exactly when
the random-audit stratum is working. At 643 current reviews nothing on screen
changes; this guards the degenerate case, which is the one that ends up in a
screenshot."
```

---

### Task 3: The reviewer routes stop answering the internet

**Files:**
- Modify: `backend/app.py` (imports, the constants block ~line 105, `_gate` ~line 188)
- Test: `tests/unit/test_review_peer_guard.py` (create)

**Interfaces:**
- Consumes: the existing `_gate` middleware and its `_reject(kind, status_code, detail, headers=None)` helper
- Produces: `backend.app.REVIEWER_PATH_PREFIX: str`, `backend.app.peer_is_public(host: str | None) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_review_peer_guard.py`:

```python
"""The reviewer routes are mounted on the same app as /predict, which demo_cidrs opens to
0.0.0.0/0. A security group rule is per-port; an application is per-path, so the port cannot
tell the moderation queue from the prediction endpoint.

Every legitimate caller is inside the VPC -- roll.sh points the console at the backend's
private address -- so a public peer on /review/* is by definition not the console.

404 rather than 403: the response should not confirm the route exists.

This does NOT remove the shared secret and does not pretend to. The public UI container
shares the frontend instance's security group, so it reaches these routes from a private
address; the secret is still the only control on that path.
"""

import pytest

from backend.app import REVIEWER_PATH_PREFIX, peer_is_public


@pytest.mark.parametrize(
    "host",
    ["<elastic-ip>", "<elastic-ip>", "<elastic-ip>", "2001:4860:4860::8888"],
)
def test_a_routable_address_is_public(host):
    assert peer_is_public(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "10.42.0.173",       # the backend's private address, from /toxic/endpoints/backend-internal
        "10.0.1.55",
        "172.17.0.4",        # docker bridge
        "192.168.1.10",
        "127.0.0.1",
        "::1",
    ],
)
def test_a_private_or_loopback_address_is_not_public(host):
    assert peer_is_public(host) is False


def test_a_peer_that_is_not_an_address_is_not_public():
    """Starlette's TestClient reports 'testclient'. A non-address peer means there is no TCP
    peer at all, which in this deployment is only ever an in-process caller. Treating it as
    public would fail every existing reviewer test for a reason unrelated to the control."""
    assert peer_is_public("testclient") is False
    assert peer_is_public(None) is False
    assert peer_is_public("") is False


def test_the_prefix_covers_login_as_well_as_the_read_and_write_routes():
    for path in ("/review/login", "/review/pending", "/review/submit"):
        assert path.startswith(REVIEWER_PATH_PREFIX)


def test_the_prefix_does_not_cover_the_graded_anonymous_feedback_route():
    """rubric 3.2 grades /feedback/user, and the user UI calls it over the internet."""
    assert not "/feedback/user".startswith(REVIEWER_PATH_PREFIX)
    assert not "/predict".startswith(REVIEWER_PATH_PREFIX)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_review_peer_guard.py -q`
Expected: FAIL — `ImportError: cannot import name 'REVIEWER_PATH_PREFIX'`

- [ ] **Step 3: Add the predicate**

In `backend/app.py`, add `import ipaddress` to the imports. Then beside the other path constants add:

```python
# Every route the reviewer capability is reached through. `/review/login` is included
# deliberately: it is the one route the demo key cannot cover, so it is the one most worth
# taking off the internet.
REVIEWER_PATH_PREFIX = "/review/"


def peer_is_public(host: str | None) -> bool:
    """Whether the TCP peer is a globally routable address.

    `is_global` is the whole test and needs no configuration: it is false for 10/8,
    172.16/12, 192.168/16 and loopback -- every legitimate caller, since roll.sh points the
    console at the backend's PRIVATE address -- and true for any real internet address.

    A peer that does not parse is treated as not public. There is no reverse proxy in front
    of this listener (docs/tls-decision.md), so `request.client.host` is the true peer and the
    only non-address value it takes is an in-process test client. X-Forwarded-For is never
    consulted, here or in `caller_identity`: a header a caller sets cannot be a trust input.
    """
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return False
```

- [ ] **Step 4: Enforce it in the gate**

In `backend/app.py`, inside `_gate`, immediately after the Content-Length/Transfer-Encoding smuggling check and **before** the body-size and API-key checks, insert:

```python
        # Ahead of the key check on purpose: a 401 here would tell an internet caller that
        # the reviewer routes exist and are merely locked. Counted under "unauthenticated"
        # rather than adding a key, because /health publishes `rejected` and the dashboard
        # reads its shape.
        if path.startswith(REVIEWER_PATH_PREFIX) and peer_is_public(
            request.client.host if request.client else None
        ):
            return _reject("unauthenticated", 404, "Not Found")
```

- [ ] **Step 5: Run the new tests**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_review_peer_guard.py -q`
Expected: 15 passed

- [ ] **Step 6: Run every gate and reviewer test**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_request_gate.py tests/unit/test_reviewer_auth.py tests/unit/test_reviewer_ui.py tests/unit/test_interface_contracts.py -q`
Expected: all pass. The TestClient peer is `testclient`, which is not public, so the clause does not fire in-process.

- [ ] **Step 7: Add the live assertion**

In `tests/integration/test_deployed_traversal.py`, add:

```python
def test_the_reviewer_routes_do_not_answer_the_internet(endpoints):
    """They are mounted on the same app as /predict, which the demo window opens to
    0.0.0.0/0. Before 2026-08-11 they answered 401 from their own handlers -- which is a
    route confirming it exists to anyone who asks."""
    for path in ("/review/login", "/review/pending", "/review/submit"):
        response = httpx.post(f"{endpoints['backend_url']}{path}", json={}, timeout=15)
        assert response.status_code == 404, f"{path} answered {response.status_code}"


def test_the_guard_discriminates_by_path_rather_than_hiding_every_route(endpoints):
    """A blanket 404 would also hide the graded anonymous feedback route. /feedback/user must
    still answer 401 -- present, and requiring the demo key."""
    response = httpx.post(
        f"{endpoints['backend_url']}/feedback/user", json={}, timeout=15
    )
    assert response.status_code == 401
```

- [ ] **Step 8: Lint, run the full unit suite, and commit**

```bash
.venv/bin/ruff check .
PYTHONHASHSEED=0 .venv/bin/pytest -m "not integration and not perf" -q
git add backend/app.py tests/unit/test_review_peer_guard.py tests/integration/test_deployed_traversal.py
git commit -m "Refuse a public peer on the reviewer routes

/review/* is mounted on the same app as /predict, and demo_cidrs opens 8000 to
0.0.0.0/0. A security group rule is per-port and an application is per-path, so
the port cannot tell the moderation queue from the prediction endpoint. Probed
from the internet the routes answered 401 from their own handlers, which is a
route confirming it exists to anyone who asks.

Every legitimate caller is inside the VPC -- roll.sh points the console at the
backend's private address -- so ipaddress.is_global is the whole test, with no
new configuration to set or forget. A peer that does not parse is treated as not
public, which is what keeps the in-process test client working. 404 rather than
403 so the response does not confirm the route.

This does not delete the shared secret and does not pretend to. The public UI
container shares the frontend instance's security group and so reaches these
routes from a private address; the secret is still the only control there."
```

---

### Task 4: Make the security documents describe what now runs

**Files:**
- Modify: `SECURITY.md` (the practices table)
- Modify: `docs/tls-decision.md` (the exposure table ~line 84, and the two bullets ~line 67)
- Test: `tests/unit/test_security_md.py` (existing — must stay green)

**Interfaces:**
- Consumes: nothing. Documentation only.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Correct the cleartext bullet in the TLS record**

`docs/tls-decision.md` line 67 currently reads that the reviewer secret "is POSTed to `/review/login` on 8000. A network observer on the path reads it." Sitting in a section about the `0.0.0.0/0` listener, that reads as an internet observer, and it is not one: the console posts to the backend's private address and the seeder only calls `/predict`. Replace that bullet with:

```markdown
- **A credential crosses a cleartext listener, inside the VPC.** The reviewer shared secret is
  POSTed to `/review/login`, and until 2026-08-11 that route answered the internet. It no
  longer does: `backend/app.py` refuses any `/review/*` request from a globally routable peer
  with a 404. What remains is a private hop in cleartext -- the console posts to the backend's
  private address -- so an observer inside the VPC reads it and an observer outside cannot
  reach the route at all. No path this project operates ever sent the secret across the public
  listener; the exposure was that the route *accepted* it, which made it brute-forceable
  online, and `backend/review_api.py:161` metered that at five attempts a minute per peer.
```

- [ ] **Step 2: Correct the exposure table row**

In the same file, change the reviewer/feedback routes row so the ingress column no longer says the reviewer routes are open to `0.0.0.0/0`:

```markdown
| **FastAPI reviewer routes** | **8000, the same listener** | **VPC peers only** -- a globally routable peer gets 404 (`backend/app.py`) | **The reviewer shared secret on `/review/login`, a bearer session token, and raw comment text from `/review/pending`** |
| FastAPI `/feedback/user` | 8000, the same listener | **`0.0.0.0/0`** plus the operator address | An anonymous agree/disagree verdict, behind the demo key. Graded under rubric 3.2 |
```

- [ ] **Step 3: State plainly what this did not fix**

Append to the same section:

```markdown
This narrows the exposure; it does not remove the credential, and the reason is worth
recording. The user-facing Streamlit and the reviewer console share EC2 #2 and therefore share
one security group, so the internet-facing container reaches these routes from a private
address and the peer guard does not see it. The shared secret is the only control on that path.
A 2026-08-11 design that would have removed it -- by relocating the console and serving the
routes on an unpublished port -- was refuted: it moved an adversarial-input renderer onto the
tier that holds the RDS master credential, reversing premortem H16. See
`docs/superpowers/specs/2026-08-11-reviewer-loopback-no-secret-design.md` section 0.
```

- [ ] **Step 4: Update the SECURITY.md row**

Find the row asserting "Every route on the backend enforces a demo API key, a rate limit, and an input-size cap. The key has three named exemptions". Append to its description, keeping the status `Enforced`:

```
Since 2026-08-11 the four reviewer routes additionally require a non-public TCP peer, and answer 404 rather than 401 to anyone else.
```

- [ ] **Step 5: Verify the documents still pass their own guards**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_security_md.py -q`

There is no `test_tls_decision.py`; `docs/tls-decision.md` is guarded only by the redaction
scan below and by `test_security_md.py`'s cross-references to it.
Expected: pass. If `test_security_md.py` fails on a banned phrase, reword the prose — do not relax the test. That guard exists because a previous edit tripped it and the correct response was to change the sentence.

Run: `.venv/bin/python scripts/redact.py SECURITY.md docs/tls-decision.md`
Expected: no findings.

- [ ] **Step 6: Commit**

```bash
git add SECURITY.md docs/tls-decision.md
git commit -m "Record that the reviewer routes no longer answer the internet

The TLS record said the reviewer secret is POSTed to a route on the world-open
listener and 'a network observer on the path reads it'. In a section about the
0.0.0.0/0 listener that reads as an internet observer, and it never was one: the
console posts to the backend's private address and the seeder only calls /predict.
The live exposure was that the route ACCEPTED the secret from the internet, which
made it brute-forceable -- a guessing surface, not an interception surface.

That surface is now closed at the gate, so the row and the bullet say so. Also
records what this did not fix: the public UI container shares the frontend
instance's security group, reaches these routes from a private address, and is
still separated from them only by the shared secret."
```

---

### Task 5: Deploy, verify against the live stack, and refresh the graded evidence

**Files:**
- Modify: `docs/submission-manifest.yml` (dashboard screenshot counts, if the panel changed)
- Modify: `docs/evidence/screenshots/monitoring-dashboard-populated.png`

**Interfaces:**
- Consumes: everything above.
- Produces: the deployed state.

- [ ] **Step 1: Open the PR and wait for all seven checks**

```bash
git push -u origin feat/reviewer-loopback-no-secret
gh pr create --base main --title "Fix two graded panel defects and take the reviewer routes off the internet" --body "See docs/superpowers/specs/2026-08-11-review-exposure-and-graded-panels-design.md"
gh pr checks --watch
```

Expected: `ci-gate`, `deps-audit`, `lint`, `sast`, `secrets-scan`, `terraform`, `test` all pass.

- [ ] **Step 2: Merge and confirm the deploy ran**

```bash
gh pr merge --squash --delete-branch
gh run watch
```

This change touches `backend/` and `monitoring/`, so `paths-ignore` does **not** skip the deploy. Confirm it ran rather than assuming it did:

```bash
aws ssm get-parameter --name /toxic/deploy/current-sha --query Parameter.Value --output text
git rev-parse HEAD
```

Expected: identical.

- [ ] **Step 3: Prove the guard from outside the VPC**

```bash
for p in /review/login /review/pending /review/submit; do
  printf '%-18s %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 \
    -X POST http://44.239.182.162:8000$p -H 'Content-Type: application/json' -d '{}')"
done
printf '%-18s %s\n' "/feedback/user" "$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 \
  -X POST http://44.239.182.162:8000/feedback/user -H 'Content-Type: application/json' -d '{}')"
```

Expected: `404`, `404`, `404`, then `401`. The last one is the point — a blanket 404 would also have hidden the graded feedback route.

- [ ] **Step 4: Prove the console still works from inside**

Open the tunnel, sign in, and complete one review end to end:

```bash
INSTANCE=$(aws ec2 describe-instances --filters "Name=tag:Name,Values=toxic-mod-frontend" \
  "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
aws ssm start-session --target "$INSTANCE" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8503"],"localPortNumber":["8503"]}'
```

Then browse `http://localhost:8503`, sign in with the secret from `pass`, and submit one item. Expected: the item leaves the queue. **This is the step that proves the peer guard did not lock out the legitimate caller** — do not skip it and do not substitute a health probe for it.

- [ ] **Step 5: Run the deploy gate and the integration suite**

```bash
make deploy-verify
PYTHONHASHSEED=0 .venv/bin/pytest -m integration -q
```

Expected: five probes green; integration suite green including the two new live assertions.

- [ ] **Step 6: Retake the dashboard screenshot and reconcile the manifest**

The drift caption changed, so the committed screenshot no longer matches the page. Retake `docs/evidence/screenshots/monitoring-dashboard-populated.png` against `http://52.43.232.239:8502`, then confirm the manifest's recorded counts still hold:

```bash
PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_submission_manifest.py -q
.venv/bin/python scripts/redact.py docs/evidence/screenshots/
```

Expected: pass, and no account id in the image.

- [ ] **Step 7: Commit the evidence**

```bash
git add docs/evidence/screenshots/monitoring-dashboard-populated.png docs/submission-manifest.yml
git commit -m "Retake the dashboard evidence after the drift caption changed"
```

---

## Self-Review

**Spec coverage.** §3.1 → Task 1. §3.2 → Task 2. §3.3 → Task 3. §4's documentation line → Task 4. §5's live assertions → Task 3 step 7 and Task 5 step 3. §6's "asserted by a live round trip through the queue before merge" → Task 5 step 4. §7's out-of-scope items are recorded in the spec and deliberately have no task.

**Placeholders.** None. Every code step carries the code; every command carries its expected output.

**Type consistency.** `production_flag_rates` takes `seeded: bool | None` in Task 1 step 3 and is called with `seeded=False` in step 4 and in the test. `DriftRow.live_n: int | None` is set in step 4 and read by `drift_live_sample_size` in step 6 and by the test in step 1. `drift_caption`'s `live_n` keyword matches across steps 5, 6 and the tests. `accuracy_is_reportable` and `accuracy_floor_notice` both take `AccuracyReport` and are used with that type in Task 2 step 4. `peer_is_public(host: str | None) -> bool` matches its call site in Task 3 step 4.

**One risk the plan carries rather than hides.** Task 1's new caption branch fires only when `live_n < n`. `FakeWindow` in the existing `test_drift_small_sample.py` returns one row for both calls, so `live_n == n` there and those tests keep their current meaning. That is correct — nothing in that file is seeded — but it does mean the existing suite cannot catch a regression in the new branch. Task 1's own test file is the only thing covering it, which is why step 1 includes both the seed-dominated and the unseeded case.
