"""Export the locked held-out split so `make seed-demo` has known-label comments.

The seeded dataset must be held out. Replaying training rows through /predict would make
the dashboard's live accuracy a measurement of memorisation, which is the one thing a
monitoring dashboard exists to detect rather than to reproduce.
"""

import argparse
from pathlib import Path

from model.data.load import REQUIRED_COLUMNS
from model.data.prepare import SplitConfig, prepare_dataset


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, type=Path, help="raw Jigsaw CSV")
    parser.add_argument("--out", default=Path("data/heldout.csv"), type=Path)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    bundle = prepare_dataset(args.csv, SplitConfig(seed=args.seed))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    bundle.test_df[list(REQUIRED_COLUMNS)].to_csv(args.out, index=False)
    print(f"wrote {len(bundle.test_df)} held-out rows to {args.out}")
    print(f"data_version={bundle.data_version}")


if __name__ == "__main__":
    main()
