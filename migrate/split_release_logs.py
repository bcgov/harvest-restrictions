"""One-off: split the combined change logs into per-release files for releases/<tag>/

usage: python migrate/split_release_logs.py LOG_land_designations.csv LOG_harvest_restrictions.csv OUT_DIR

Writes OUT_DIR/releases/<release_tag>/{land_designations,harvest_restrictions}.csv, adding
released_at (copied from release_date - for these backfilled releases it's the best available
record of release order). Upload with:

    aws s3 cp --recursive OUT_DIR/releases s3://$BUCKET/harvest_restrictions/releases
"""

import os
import sys

import pandas

land_log, harvest_log, out_dir = sys.argv[1:4]
for log_file, name in [
    (land_log, "land_designations.csv"),
    (harvest_log, "harvest_restrictions.csv"),
]:
    log = pandas.read_csv(log_file, dtype=str, keep_default_na=False)
    if "released_at" not in log.columns:
        log["released_at"] = log["release_date"]
    for tag, rows in log.groupby("release_tag", sort=False):
        path = os.path.join(out_dir, "releases", tag, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows.to_csv(path, index=False)
        print(f"{path}: {len(rows)} rows")
