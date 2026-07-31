"""CLI: prepare the dataset, run the firewall gate, emit the profile and the versions."""

import argparse
import json
from pathlib import Path

import pandas as pd

from model.data.firewall_check import assert_no_leakage
from model.data.prepare import SplitConfig, prepare_dataset
from model.data.profile import write_profile
from model.seeds import assert_hash_seed_pinned, run_metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--profile-out", type=Path, default=Path("docs/data-profile.md"))
    args = parser.parse_args()

    assert_hash_seed_pinned()
    bundle = prepare_dataset(args.csv, SplitConfig(seed=args.seed))
    report = assert_no_leakage(bundle)
    write_profile(
        pd.concat([bundle.train_df, bundle.test_df], ignore_index=True),
        args.profile_out,
        source=str(args.csv),
        raw_sha256=bundle.raw_sha256,
    )
    print(json.dumps(run_metadata(args.seed, bundle.raw_sha256, bundle.split_version,
                                  bundle.env_version), indent=2))
    print(f"firewall: {report.summary()}")
    print(f"train={len(bundle.train_df)} test={len(bundle.test_df)} "
          f"folds={len(bundle.fold_indices)}")


if __name__ == "__main__":
    main()
