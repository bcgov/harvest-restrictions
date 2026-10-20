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
    # ordered by released_at - tag names and commit dates needn't follow release order
    rows = pandas.concat(frames, ignore_index=True)
    released_at = pandas.to_datetime(rows["released_at"], utc=True, format="ISO8601")
    rows = rows.iloc[released_at.argsort(kind="stable")]
    rows.to_csv(f"release_logs_backup/{key}", index=False)
    print(f"release_logs_backup/{key}: {list(rows['release_tag'].unique())}")
    if "--delete" in sys.argv:
        store.upload(f"release_logs_backup/{key}", key)
        for k in folder_keys:
            store.delete(k)
        print(f"published {store.url(key)}, deleted {len(folder_keys)} folder files")
