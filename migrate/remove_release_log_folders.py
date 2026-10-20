"""One-off: build the change logs (logs/<log>.csv) from the old per-release folders
(logs/<release_tag>/<log>.csv), as the old release did, then delete the folders

Everything is downloaded to release_logs_backup/, and the built change logs written there for
review. Pass --delete to publish them and delete the folders. Run from the repo root:

    docker compose run --rm runner python migrate/remove_release_log_folders.py [--delete]
"""

import os
import sys

import pandas

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harvest_restrictions as hr  # noqa: E402

store = hr.S3Store(os.environ["BUCKET"])
keys = store.list("logs/")
for log in hr.LOGS:
    if f"logs/{log}.csv" in keys:
        sys.exit(f"{store.url(f'logs/{log}.csv')} already exists")
for log in hr.LOGS:
    key = f"logs/{log}.csv"
    folder_keys = [k for k in keys if k.count("/") == 2 and k.endswith(f"/{log}.csv")]
    frames = []
    for k in folder_keys:
        store.download(k, f"release_logs_backup/{k}")
        frames.append(pandas.read_csv(f"release_logs_backup/{k}"))
    # in release order - the release tags (vYYYY-MM) sort by date. Only the columns release
    # writes are kept: run_id was never filled in by the backfill, and the release tag gives
    # the release's date and (as a git tag) its commit
    rows = (
        pandas.concat(frames, ignore_index=True)
        .drop(
            columns=["run_id", "released_at", "release_date", "commit"], errors="ignore"
        )
        .sort_values("release_tag", kind="stable")
    )
    # first, for sorting and filtering the logs by eye
    rows.insert(0, "release_tag", rows.pop("release_tag"))
    rows.to_csv(f"release_logs_backup/{key}", index=False)
    print(f"release_logs_backup/{key}: {list(rows['release_tag'].unique())}")
    if "--delete" in sys.argv:
        store.upload(f"release_logs_backup/{key}", key)
        for k in folder_keys:
            store.delete(k)
        print(f"published {store.url(key)}, deleted {len(folder_keys)} folder files")
