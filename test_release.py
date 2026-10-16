import io
import json
import os
import shutil
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone

import pandas
import pytest

import harvest_restrictions as hr

COMMIT = "abc1234def5678"
SHORT_COMMIT = "abc1234"
RELEASE_TAG = "v2026-10"
RELEASE_DATE = "2026-10-01T10:00:00-07:00"
RUN_ID = "20261001T000000Z"
PREVIOUS_RELEASE = "v2026-02"
# the run the fixture's previous release was made from
RELEASED_RUN = "20260101T000000Z"
ARCHIVE_KEY = f"releases/harvest_restrictions_{RELEASE_TAG}.zip"
SOURCES_CSV_BODY = "index,alias\n1,park_national\n"


# --- build_change ---


COLUMNS = ["harvest_restriction_class_rank", "harvest_restriction_class_name"]


@pytest.fixture
def history():
    return pandas.DataFrame(
        {
            "harvest_restriction_class_rank": [1, 2, 1, 2],
            "harvest_restriction_class_name": ["Protected", "Prohibited"] * 2,
            "area_ha": [999.0, 999.0, 100.0, 50.0],
            "release_tag": ["v2026-01", "v2026-01", "v2026-02", "v2026-02"],
        }
    )


@pytest.fixture
def current(tmp_path):
    # class 2 dropped since v2026-02, class 3 new in this run
    path = tmp_path / "harvest_restrictions.csv"
    pandas.DataFrame(
        {
            "harvest_restriction_class_rank": [1, 3],
            "harvest_restriction_class_name": ["Protected", "High Restricted"],
            "area_ha": [110.4, 30.0],
        }
    ).to_csv(path, index=False)
    return path


def test_build_change_diff(current, history):
    row = hr.build_change(current, history, "v2026-02", COLUMNS).loc[1]
    # compared against v2026-02 only, not the older v2026-01 rows
    assert row["v2026-02"] == 100
    assert row["current"] == 110
    assert row["diff"] == 10
    assert row["pct_diff"] == 10.4


def test_build_change_new_and_dropped_categories(current, history):
    result = hr.build_change(current, history, "v2026-02", COLUMNS)
    assert sorted(result.index) == [1, 2, 3]

    # dropped since the previous release - labelled from the previous release, no current value
    dropped = result.loc[2]
    assert dropped["harvest_restriction_class_name"] == "Prohibited"
    assert pandas.isna(dropped["current"])
    assert pandas.isna(dropped["diff"])

    # new in this run - labelled from this run, no previous value
    new = result.loc[3]
    assert new["harvest_restriction_class_name"] == "High Restricted"
    assert new["current"] == 30
    assert pandas.isna(new["v2026-02"])
    assert pandas.isna(new["pct_diff"])


def test_build_change_columns(current, history):
    result = hr.build_change(current, history, "v2026-02", COLUMNS)
    assert result.index.name == "harvest_restriction_class_rank"
    assert list(result.columns) == [
        "harvest_restriction_class_name",
        "current",
        "v2026-02",
        "diff",
        "pct_diff",
    ]


# --- S3Store ---


def test_s3store_list(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        out = json.dumps(
            {
                "Contents": [
                    {"Key": "harvest_restrictions/drafts/b"},
                    {"Key": "harvest_restrictions/drafts/a"},
                ]
            }
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=out)

    monkeypatch.setattr(hr.subprocess, "run", fake_run)
    store = hr.S3Store("bucket")
    # keys relative to the prefix, sorted
    assert store.list("drafts/") == ["drafts/a", "drafts/b"]
    assert calls[0][calls[0].index("--prefix") + 1] == "harvest_restrictions/drafts/"


def test_s3store_list_empty(monkeypatch):
    # the cli prints nothing at all when no objects match
    monkeypatch.setattr(
        hr.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=""),
    )
    assert hr.S3Store("bucket").list("drafts/") == []


# --- geopackages and zips ---


def make_gpkg(path, layer):
    subprocess.run(
        [
            "ogr2ogr",
            "-f",
            "GPKG",
            str(path),
            "-dialect",
            "sqlite",
            "-sql",
            "select 1 as id, MakePoint(1, 2, 3005) as geom",
            ":memory:",
            "-nln",
            layer,
        ],
        check=True,
    )


@pytest.mark.skipif(not shutil.which("ogr2ogr"), reason="requires GDAL")
def test_add_gpkg_layer(tmp_path):
    bundle = tmp_path / "harvest_restrictions.gpkg"
    make_gpkg(bundle, "harvest_restrictions")
    # the source layer is renamed, whatever it's called
    sources = tmp_path / "harvest_restrictions_sources.gpkg"
    make_gpkg(sources, "some_table")

    hr.add_gpkg_layer(str(bundle), str(sources), "designations")
    info = subprocess.run(
        ["ogrinfo", "-q", str(bundle)], check=True, capture_output=True, text=True
    ).stdout
    assert "harvest_restrictions" in info
    assert "designations" in info


@pytest.mark.skipif(not shutil.which("sozip"), reason="requires GDAL")
def test_zip_files_readable_by_gdal(tmp_path):
    gpkg = tmp_path / "harvest_restrictions.gpkg"
    make_gpkg(gpkg, "harvest_restrictions")
    csv = tmp_path / "sources.csv"
    csv.write_text(SOURCES_CSV_BODY)
    archive = tmp_path / "harvest_restrictions_v1.zip"

    hr.zip_files(str(archive), [str(gpkg), str(csv)])
    with zipfile.ZipFile(archive) as z:
        assert sorted(z.namelist()) == ["harvest_restrictions.gpkg", "sources.csv"]
    info = subprocess.run(
        [
            "ogrinfo",
            "-so",
            f"/vsizip/{archive}/harvest_restrictions.gpkg",
            "harvest_restrictions",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Feature Count: 1" in info


def test_unzip_file(tmp_path):
    # the file inside needn't match the name it's extracted to
    in_zip = tmp_path / "harvest_restrictions.gpkg.zip"
    with zipfile.ZipFile(in_zip, "w") as z:
        z.writestr("custom_name.gpkg", "gpkg")

    out_file = tmp_path / "harvest_restrictions.gpkg"
    hr.unzip_file(in_zip, out_file)
    assert out_file.read_text() == "gpkg"


# --- release ---
#
# releases are run against an in-memory store, with git, ogr2ogr and sozip stubbed out


class MemoryStore:
    """in-memory stand-in for S3Store"""

    def __init__(self):
        self.objects = {}
        self.uploads = []

    def url(self, key):
        return f"memory://{key}"

    def put(self, key, body, tags=None):
        if isinstance(body, str):
            body = body.encode()
        self.objects[key] = {"body": body, "tags": dict(tags or {})}

    def read_csv(self, key):
        return pandas.read_csv(io.BytesIO(self.objects[key]["body"]))

    def upload(self, local_file, key, tags=None):
        with open(local_file, "rb") as f:
            self.put(key, f.read(), tags)
        self.uploads.append(key)

    def download(self, key, local_file):
        if key not in self.objects:
            raise subprocess.CalledProcessError(1, f"aws s3 cp {key}")
        os.makedirs(os.path.dirname(local_file) or ".", exist_ok=True)
        with open(local_file, "wb") as f:
            f.write(self.objects[key]["body"])

    def list(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete_prefix(self, prefix):
        for key in self.list(prefix + "/"):
            del self.objects[key]


def fake_git(cmd, *args, **kwargs):
    if cmd[:2] == ["git", "status"]:
        return b""
    if cmd[:2] == ["git", "describe"]:
        return RELEASE_TAG.encode()
    if cmd[:2] == ["git", "log"]:
        return RELEASE_DATE.encode()
    if cmd[:3] == ["git", "rev-parse", "--short"]:
        return SHORT_COMMIT.encode()
    if cmd[:2] == ["git", "rev-parse"]:
        return COMMIT.encode()
    raise AssertionError(f"unexpected command {cmd}")


def fake_add_gpkg_layer(out_file, source_file, layer_name):
    """record the added layer as a line in out_file, with the source's contents"""
    with open(source_file) as f:
        source = f.read().strip()
    with open(out_file, "a") as f:
        f.write(f"{layer_name}\t{source}\n")


def fake_zip_files(out_zip, files):
    """plain zip in place of sozip, which isn't needed to check what's archived"""
    with zipfile.ZipFile(out_zip, "w") as z:
        for f in files:
            z.write(f, os.path.basename(f))


def zipped(name, body):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(name, body)
    return out.getvalue()


def archive_members(store, key):
    with zipfile.ZipFile(io.BytesIO(store.objects[key]["body"])) as z:
        return {name: z.read(name) for name in z.namelist()}


# one category per log, with a value in the previous release
LOG_ROWS = {
    "land_designations": {
        "land_designation_type_rank": 1,
        "harvest_restriction_class_rank": 1,
        "harvest_restriction_class_name": "Protected",
        "land_designation_type_code": "park_national",
        "land_designation_type_name": "National Park",
    },
    "harvest_restrictions": {
        "harvest_restriction_class_rank": 1,
        "harvest_restriction_class_name": "Protected",
    },
}


def summary_body(log, previous_release=PREVIOUS_RELEASE):
    """a summary as publish_draft writes it - harvest_restrictions has a second class,
    dropped since the previous release, so it has no current value"""
    rows = [{**LOG_ROWS[log], "current": 110.0, previous_release: 100.0}]
    if log == "harvest_restrictions":
        rows.append(
            {
                "harvest_restriction_class_rank": 2,
                "harvest_restriction_class_name": "Prohibited",
                "current": None,
                previous_release: 50.0,
            }
        )
    df = pandas.DataFrame(rows)
    df["diff"] = df["current"] - df[previous_release]
    df["pct_diff"] = df["diff"] / df[previous_release] * 100
    return df.to_csv(index=False)


def put_release_log(store, release_tag, released_at, logs=None, run_id=RELEASED_RUN):
    """publish one release's change log rows, as publish_release does"""
    for log, row in LOG_ROWS.items():
        if logs and log not in logs:
            continue
        rows = pandas.DataFrame(
            [
                {
                    **row,
                    "area_ha": 100.0,
                    "release_tag": release_tag,
                    "release_date": "2026-01-01",
                    "commit": "0000000",
                    "run_id": run_id,
                    "released_at": released_at,
                }
            ]
        )
        store.put(hr.release_log_key(release_tag, log), rows.to_csv(index=False))


def put_run(
    store,
    run_id=RUN_ID,
    commit=COMMIT,
    previous_release=PREVIOUS_RELEASE,
    complete=True,
):
    """publish a full set of draft files, as publish_draft does"""
    bodies = {
        hr.OVERLAY_ZIP: zipped("harvest_restrictions.gpkg", f"overlay {run_id}"),
        hr.SOURCES_ZIP: zipped("harvest_restrictions_sources.gpkg", "designations"),
        hr.SOURCES_CSV: SOURCES_CSV_BODY,
    }
    for log in hr.LOGS:
        bodies[hr.summary(log)] = summary_body(log, previous_release)
    for name, body in bodies.items():
        store.put(hr.draft_key(run_id, name), body)
    # an incomplete run failed before publishing its manifest
    if complete:
        manifest = {
            "run_id": run_id,
            "commit": commit,
            "previous_release": previous_release,
            "created": "2026-10-01T00:00:00+00:00",
            "files": list(bodies),
        }
        store.put(hr.draft_key(run_id, hr.MANIFEST), json.dumps(manifest))


@pytest.fixture
def store(tmp_path, monkeypatch):
    """a store holding one previous release, plus one run of COMMIT"""
    memory = MemoryStore()
    put_release_log(memory, PREVIOUS_RELEASE, "2026-02-10T15:34:35-08:00")
    put_run(memory)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hr.subprocess, "check_output", fake_git)
    monkeypatch.setattr(hr, "add_gpkg_layer", fake_add_gpkg_layer)
    monkeypatch.setattr(hr, "zip_files", fake_zip_files)
    return memory


def run_release(store, run_id=None):
    hr.publish_release(store, run_id, out_dir=".")


def test_release_publishes(store):
    run_release(store)

    latest_keys = [
        "latest/harvest_restrictions.gpkg",
        "latest/harvest_restrictions_sources.gpkg",
        "latest/sources.csv",
        "latest/land_designations_summary.csv",
        "latest/harvest_restrictions_summary.csv",
    ]
    combined_log_keys = ["logs/land_designations.csv", "logs/harvest_restrictions.csv"]
    release_log_keys = [
        f"logs/{RELEASE_TAG}/land_designations.csv",
        f"logs/{RELEASE_TAG}/harvest_restrictions.csv",
    ]

    # the archive first, this release's change log rows last
    assert (
        store.uploads
        == [ARCHIVE_KEY] + latest_keys + combined_log_keys + release_log_keys
    )

    tags = {"commit": COMMIT, "run_id": RUN_ID, "release": RELEASE_TAG}
    for key in store.uploads:
        assert store.objects[key]["tags"] == tags

    # the archive holds one geopackage with both layers, plus the csvs as is
    members = archive_members(store, ARCHIVE_KEY)
    assert sorted(members) == [
        "harvest_restrictions.gpkg",
        "harvest_restrictions_summary.csv",
        "land_designations_summary.csv",
        "sources.csv",
    ]
    assert members["harvest_restrictions.gpkg"] == (
        f"overlay {RUN_ID}designations\tdesignations\n".encode()
    )
    assert members["sources.csv"] == SOURCES_CSV_BODY.encode()

    # the latest geopackages are the drafts' unzipped, each with just its own layer
    latest = store.objects
    assert (
        latest["latest/harvest_restrictions.gpkg"]["body"]
        == f"overlay {RUN_ID}".encode()
    )
    assert latest["latest/harvest_restrictions_sources.gpkg"]["body"] == b"designations"
    assert latest["latest/sources.csv"]["body"] == SOURCES_CSV_BODY.encode()

    # drafts are left for prune-drafts
    assert hr.draft_key(RUN_ID, hr.MANIFEST) in store.objects


def test_release_logs(store):
    run_release(store)

    # this release's own rows - the class dropped since the previous release isn't logged
    # (as zero, or at all)
    rows = store.read_csv(hr.release_log_key(RELEASE_TAG, "harvest_restrictions"))
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row["harvest_restriction_class_rank"] == 1
    assert row["area_ha"] == 110
    assert row["release_tag"] == RELEASE_TAG
    assert row["release_date"] == RELEASE_DATE
    assert row["commit"] == SHORT_COMMIT
    assert row["run_id"] == RUN_ID
    assert pandas.notna(row["released_at"])

    designations = store.read_csv(hr.release_log_key(RELEASE_TAG, "land_designations"))
    assert list(designations.columns[:5]) == hr.LOGS["land_designations"]

    # the combined logs, rebuilt from every release's rows
    for log in hr.LOGS:
        combined = store.read_csv(hr.combined_log_key(log))
        assert list(combined["release_tag"]) == [PREVIOUS_RELEASE, RELEASE_TAG]


def test_read_release_logs_after_release(store, tmp_path):
    # the combined logs a release publishes (every release's rows) sit next to the per-release
    # folders under logs/ - reading the logs back mustn't count their rows again
    run_release(store)

    histories = hr.read_release_logs(store, str(tmp_path / "read"))
    for history in histories.values():
        assert list(history["release_tag"]) == [PREVIOUS_RELEASE, RELEASE_TAG]


def test_read_release_logs_ordered_by_released_at(store, tmp_path):
    # tag names (and so the order keys are listed in) needn't follow release order - here
    # v2025-08 was released after v2026-02. Mixed timestamp formats, as in backfilled logs
    put_release_log(store, "v2025-08", "2026-08-20T09:00:00-07:00")
    put_release_log(store, "v2024-04", "2024-04-01")

    histories = hr.read_release_logs(store, str(tmp_path))
    for history in histories.values():
        assert list(history["release_tag"]) == [
            "v2024-04",
            PREVIOUS_RELEASE,
            "v2025-08",
        ]
    assert hr.latest_release(histories["harvest_restrictions"]) == "v2025-08"


def test_read_release_logs_ignores_other_keys(store, tmp_path):
    # other files under logs/, and log rows in other layouts, aren't read
    store.put(f"logs/{RELEASE_TAG}/notes.txt", "notes")
    # (real log rows, so they'd show up if read)
    key = hr.release_log_key(PREVIOUS_RELEASE, "harvest_restrictions")
    rows = store.objects[key]["body"]
    store.put(
        "logs/log/v2026-05/harvest_restrictions.csv",
        rows.replace(b"v2026-02", b"v2026-05"),
    )
    # the combined log
    store.put("logs/harvest_restrictions.csv", rows.replace(b"v2026-02", b"v2026-06"))

    histories = hr.read_release_logs(store, str(tmp_path))
    assert list(histories["harvest_restrictions"]["release_tag"]) == [PREVIOUS_RELEASE]


def test_release_defaults_to_latest_run(store, tmp_path):
    later_run = "20261002T000000Z"
    put_run(store, run_id=later_run)
    # still later runs that don't qualify: one that never finished publishing, and one of
    # another commit
    put_run(store, run_id="20261003T000000Z", complete=False)
    put_run(store, run_id="20261004T000000Z", commit="f00")

    run_release(store)
    assert store.objects[ARCHIVE_KEY]["tags"]["run_id"] == later_run
    latest = store.objects["latest/harvest_restrictions.gpkg"]["body"]
    assert latest == f"overlay {later_run}".encode()


def test_release_pinned_run(store):
    put_run(store, run_id="20261002T000000Z")

    run_release(store, run_id=RUN_ID)
    assert store.objects[ARCHIVE_KEY]["tags"]["run_id"] == RUN_ID


def test_release_pinned_run_of_other_commit(store):
    put_run(store, run_id="20261002T000000Z", commit="f00")

    with pytest.raises(ValueError, match="is of commit f00"):
        run_release(store, run_id="20261002T000000Z")
    assert store.uploads == []


def test_release_no_run(store):
    with pytest.raises(ValueError, match="against this commit"):
        run_release(store, run_id="20200101T000000Z")
    assert store.uploads == []


def test_release_missing_draft(store):
    del store.objects[hr.draft_key(RUN_ID, "land_designations_summary.csv")]

    with pytest.raises(ValueError, match="missing land_designations_summary.csv"):
        run_release(store)
    assert store.uploads == []


def test_release_already_released(store):
    put_release_log(store, RELEASE_TAG, "2026-10-02T00:00:00+00:00")

    with pytest.raises(ValueError, match="already been released"):
        run_release(store)
    assert store.uploads == []


def test_release_partially_logged(store):
    # a previous attempt failed between publishing its change log files
    put_release_log(
        store, RELEASE_TAG, "2026-10-02T00:00:00+00:00", logs=["land_designations"]
    )

    run_release(store)
    for log in hr.LOGS:
        combined = store.read_csv(hr.combined_log_key(log))
        assert list(combined["release_tag"]) == [PREVIOUS_RELEASE, RELEASE_TAG]


def test_release_stale_summaries(store):
    # another release since the run - its diffs are against an older release
    put_release_log(store, "v2026-05", "2026-05-01T00:00:00+00:00")

    with pytest.raises(ValueError, match="older release"):
        run_release(store)
    assert store.uploads == []


def test_release_clears_leftover_files(store, tmp_path):
    # a bundle from a failed release attempt
    leftover = tmp_path / "releases" / RELEASE_TAG / "bundle"
    leftover.mkdir(parents=True)
    (leftover / "harvest_restrictions.gpkg").write_text("stale\n")

    run_release(store)
    members = archive_members(store, ARCHIVE_KEY)
    assert b"stale" not in members["harvest_restrictions.gpkg"]


def test_release_builds_locally_by_key(store, tmp_path):
    run_release(store)

    # each published file is built at its key, under releases/<release_tag>/
    release_dir = tmp_path / "releases" / RELEASE_TAG
    for key in store.uploads:
        assert (release_dir / key).read_bytes() == store.objects[key]["body"]


# --- publish_draft ---


@pytest.fixture
def run_dirs(tmp_path):
    """a run's local outputs and area totals, as overlay writes them"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for name in (hr.OVERLAY_ZIP, hr.SOURCES_ZIP, hr.SOURCES_CSV):
        (run_dir / name).write_text(name)
    totals_dir = tmp_path / "totals"
    totals_dir.mkdir()
    for log, row in LOG_ROWS.items():
        pandas.DataFrame([{**row, "area_ha": 120.0}]).to_csv(
            totals_dir / f"{log}.csv", index=False
        )
    return str(run_dir), str(totals_dir)


def test_publish_draft(store, run_dirs):
    run_dir, totals_dir = run_dirs
    run_id = "20261006T000000Z"
    hr.publish_draft(store, run_id, run_dir, totals_dir, COMMIT)

    # every draft file, then the manifest last, marking the run complete
    manifest_key = hr.draft_key(run_id, hr.MANIFEST)
    assert store.uploads == [hr.draft_key(run_id, f) for f in hr.DRAFT_FILES] + [
        manifest_key
    ]
    manifest = json.loads(store.objects[manifest_key]["body"])
    assert manifest["commit"] == COMMIT
    assert manifest["previous_release"] == PREVIOUS_RELEASE
    assert manifest["files"] == hr.DRAFT_FILES

    # summaries comparing the run's totals to the previous release, written to the run's
    # local folder too
    summary = store.read_csv(hr.draft_key(run_id, "harvest_restrictions_summary.csv"))
    assert list(summary.columns) == [
        "harvest_restriction_class_rank",
        "harvest_restriction_class_name",
        "current",
        PREVIOUS_RELEASE,
        "diff",
        "pct_diff",
    ]
    assert summary.iloc[0]["diff"] == 20
    assert os.path.exists(os.path.join(run_dir, "harvest_restrictions_summary.csv"))
    assert os.path.exists(os.path.join(run_dir, hr.MANIFEST))


def test_publish_draft_then_release(store, run_dirs):
    # what publish_draft writes is what publish_release reads
    run_dir, totals_dir = run_dirs
    for name, body in (
        (hr.OVERLAY_ZIP, zipped("harvest_restrictions.gpkg", "overlay")),
        (hr.SOURCES_ZIP, zipped("harvest_restrictions_sources.gpkg", "designations")),
    ):
        with open(os.path.join(run_dir, name), "wb") as f:
            f.write(body)
    run_id = "20261006T000000Z"
    hr.publish_draft(store, run_id, run_dir, totals_dir, COMMIT)

    run_release(store, run_id=run_id)
    rows = store.read_csv(hr.release_log_key(RELEASE_TAG, "harvest_restrictions"))
    assert list(rows["area_ha"]) == [120]


def test_publish_draft_no_release_yet(store, run_dirs):
    for key in store.list("logs/"):
        del store.objects[key]
    with pytest.raises(ValueError, match="release at least one version"):
        hr.publish_draft(store, "20261006T000000Z", *run_dirs, COMMIT)
    assert store.uploads == []


def git_with(status="", head=COMMIT):
    """fake_git, with the given working tree status and HEAD"""

    def fake(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "status"]:
            return status.encode()
        if cmd == ["git", "rev-parse", "HEAD"]:
            return head.encode()
        return fake_git(cmd)

    return fake


def test_committed_head(store):
    assert hr.committed_head() == COMMIT


def test_committed_head_uncommitted_changes(store, monkeypatch):
    monkeypatch.setattr(hr.subprocess, "check_output", git_with(" M sql/overlay.sql"))
    with pytest.raises(ValueError, match="uncommitted changes") as e:
        hr.committed_head()
    # the changed files are listed
    assert "sql/overlay.sql" in str(e.value)


def test_committed_head_untracked_files(store, monkeypatch):
    # only tracked files are checked - git is asked to leave untracked files out
    calls = []

    def fake(cmd, *args, **kwargs):
        calls.append(cmd)
        return fake_git(cmd)

    monkeypatch.setattr(hr.subprocess, "check_output", fake)
    hr.committed_head()
    assert ["git", "status", "--porcelain", "--untracked-files=no"] in calls


def test_publish_draft_uncommitted_changes(store, run_dirs, monkeypatch):
    # a file edited during the run
    monkeypatch.setattr(hr.subprocess, "check_output", git_with(" M sources.json"))
    with pytest.raises(ValueError, match="uncommitted changes"):
        hr.publish_draft(store, "20261006T000000Z", *run_dirs, COMMIT)
    assert store.uploads == []


def test_publish_draft_head_moved(store, run_dirs, monkeypatch):
    # a commit made (or another commit checked out) during the run
    monkeypatch.setattr(hr.subprocess, "check_output", git_with(head="f00"))
    with pytest.raises(ValueError, match="moved from"):
        hr.publish_draft(store, "20261006T000000Z", *run_dirs, COMMIT)
    assert store.uploads == []


# --- delete_drafts ---


def run_prune(store, older_than=None, dry_run=False, out_dir="."):
    hr.delete_drafts(store, older_than, dry_run, out_dir)


def runs(store):
    return sorted({k.split("/")[1] for k in store.list("drafts/") if k.count("/") == 2})


def test_prune_released_runs(store):
    put_run(store, run_id=RELEASED_RUN)
    put_run(store, run_id="20200101T000000Z")

    run_prune(store)
    # only the released run - unreleased runs are kept, however old, without older_than
    assert runs(store) == ["20200101T000000Z", RUN_ID]


def test_prune_older_than(store):
    # runs relative to now, rather than the fixture's run, whose age depends on the date
    store.delete_prefix(hr.run_prefix(RUN_ID))
    recent = [
        (datetime.now(timezone.utc) - timedelta(days=d)).strftime(hr.RUN_ID_FORMAT)
        for d in (2, 1)
    ]
    put_run(store, run_id="20200101T000000Z")
    for run_id in recent:
        put_run(store, run_id=run_id)

    run_prune(store, older_than=90)
    assert runs(store) == recent


def test_prune_keeps_newest_unreleased(store):
    # every unreleased run is old - the newest may still be under review, so it's kept
    put_run(store, run_id="20200101T000000Z")
    put_run(store, run_id="20200201T000000Z")
    store.delete_prefix(hr.run_prefix(RUN_ID))

    run_prune(store, older_than=90)
    assert runs(store) == ["20200201T000000Z"]


def test_prune_keeps_partially_released_run(store):
    # a release that failed between publishing its change log files - its retry still
    # needs the draft
    put_release_log(
        store,
        "v2026-05",
        "2026-05-01T00:00:00+00:00",
        logs=["land_designations"],
        run_id=RUN_ID,
    )

    run_prune(store)
    assert runs(store) == [RUN_ID]


def test_prune_dry_run(store):
    put_run(store, run_id=RELEASED_RUN)

    run_prune(store, older_than=0, dry_run=True)
    assert runs(store) == [RELEASED_RUN, RUN_ID]


def test_prune_ignores_other_keys(store):
    # files outside any run prefix, and a prefix that isn't a run's
    store.put("drafts/harvest_restrictions.gpkg.zip", "old layout")
    store.put("drafts/notarun/file.csv", "?")
    # nor does a non-run prefix (which sorts after every run_id) count as the newest
    # unreleased run - the actual newest unreleased run is still kept
    put_run(store, run_id="20200101T000000Z")

    run_prune(store, older_than=0)
    assert "drafts/harvest_restrictions.gpkg.zip" in store.objects
    assert "drafts/notarun/file.csv" in store.objects
    assert runs(store) == [RUN_ID, "notarun"]


def test_prune_local_copies(store, tmp_path):
    put_run(store, run_id=RELEASED_RUN)
    for run_id in (RELEASED_RUN, RUN_ID):
        run_dir = tmp_path / hr.run_prefix(run_id)
        run_dir.mkdir(parents=True)
        (run_dir / hr.MANIFEST).write_text("{}")

    run_prune(store, dry_run=True, out_dir=str(tmp_path))
    assert (tmp_path / hr.run_prefix(RELEASED_RUN)).exists()

    run_prune(store, out_dir=str(tmp_path))
    # the released run's local copy goes with it - the unreleased run's is kept
    assert not (tmp_path / hr.run_prefix(RELEASED_RUN)).exists()
    assert (tmp_path / hr.run_prefix(RUN_ID)).exists()


def test_prune_without_local_copy(store, tmp_path):
    # a run published from elsewhere (eg CI) has no local copy to delete
    put_run(store, run_id=RELEASED_RUN)

    run_prune(store, out_dir=str(tmp_path))
    assert runs(store) == [RUN_ID]
