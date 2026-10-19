# --- WARNING - this file is 100% unreviewed AI slop

import io
import json
import os
import shutil
import subprocess
import zipfile

import pandas
import pytest

import harvest_restrictions as hr

COMMIT = "abc1234def5678"
RELEASE_TAG = "v2026-10"
RC_TAG = "v2026-10-rc.1"
RELEASE_DATE = "2026-10-01T10:00:00-07:00"
PREVIOUS_RELEASE = "v2026-02"
RC_KEY = f"prereleases/harvest_restrictions_{RC_TAG}.zip"
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
                    {"Key": "harvest_restrictions/logs/b"},
                    {"Key": "harvest_restrictions/logs/a"},
                ]
            }
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=out)

    monkeypatch.setattr(hr.subprocess, "run", fake_run)
    store = hr.S3Store("bucket")
    # keys relative to the prefix, sorted
    assert store.list("logs/") == ["logs/a", "logs/b"]
    assert calls[0][calls[0].index("--prefix") + 1] == "harvest_restrictions/logs/"


def test_s3store_list_empty(monkeypatch):
    # the cli prints nothing at all when no objects match
    monkeypatch.setattr(
        hr.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=""),
    )
    assert hr.S3Store("bucket").list("logs/") == []


def test_s3store_tags(monkeypatch):
    out = json.dumps({"TagSet": [{"Key": "previous_release", "Value": "v2026-02"}]})
    monkeypatch.setattr(
        hr.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=out),
    )
    assert hr.S3Store("bucket").tags(RC_KEY) == {"previous_release": "v2026-02"}


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


def gpkg_layers(path):
    info = subprocess.run(
        ["ogrinfo", "-q", str(path)], check=True, capture_output=True, text=True
    ).stdout
    # eg "1: harvest_restrictions (Point)"
    return sorted(line.split(": ")[1].split(" ")[0] for line in info.splitlines())


@pytest.mark.skipif(not shutil.which("ogr2ogr"), reason="requires GDAL")
def test_add_gpkg_layer(tmp_path):
    # creating the geopackage, then adding to it - each layer named as given
    source = tmp_path / "source.gpkg"
    make_gpkg(source, "some_table")
    bundle = tmp_path / "harvest_restrictions.gpkg"

    hr.add_gpkg_layer(str(bundle), str(source), "harvest_restrictions")
    hr.add_gpkg_layer(str(bundle), str(source), "designations")
    assert gpkg_layers(bundle) == ["designations", "harvest_restrictions"]

    # just one layer of a geopackage
    latest = tmp_path / "latest.gpkg"
    hr.add_gpkg_layer(str(latest), str(bundle), "designations", "designations")
    assert gpkg_layers(latest) == ["designations"]


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


# --- fakes ---
#
# candidates and releases are run against an in-memory store, with git, ogr2ogr and sozip
# stubbed out


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

    def upload(self, local_file, key, tags):
        with open(local_file, "rb") as f:
            self.put(key, f.read(), tags)
        self.uploads.append(key)

    def tags(self, key):
        return self.objects[key]["tags"]

    def download(self, key, local_file):
        if key not in self.objects:
            raise subprocess.CalledProcessError(1, f"aws s3 cp {key}")
        os.makedirs(os.path.dirname(local_file) or ".", exist_ok=True)
        with open(local_file, "wb") as f:
            f.write(self.objects[key]["body"])

    def list(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete(self, key):
        del self.objects[key]


def fake_git(tags=(RELEASE_TAG, RC_TAG), status="", head=COMMIT):
    """git, with the checked-out commit at head, tagged with tags"""

    def git(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "status"]:
            return status.encode()
        if cmd[:2] == ["git", "tag"]:
            return "\n".join(tags).encode()
        if cmd[:2] == ["git", "log"]:
            return RELEASE_DATE.encode()
        if cmd == ["git", "rev-parse", "HEAD"]:
            return head.encode()
        raise AssertionError(f"unexpected command {cmd}")

    return git


def fake_add_gpkg_layer(out_file, source, layer_name, *args):
    """record the added layer as a line in out_file"""
    with open(out_file, "a") as f:
        f.write(f"{layer_name}\t{os.path.basename(source)}\t{' '.join(args)}\n")


def fake_zip_files(out_zip, files):
    """plain zip in place of sozip, which isn't needed to check what's archived"""
    with zipfile.ZipFile(out_zip, "w") as z:
        for f in files:
            z.write(f, os.path.basename(f))


def zip_members(body):
    with zipfile.ZipFile(io.BytesIO(body)) as z:
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
    """a summary as publish_prerelease writes it - harvest_restrictions has a second class,
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


def put_release_log(store, release_tag, released_at, logs=None):
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
                    "prerelease": f"{release_tag}-rc.1",
                    "released_at": released_at,
                }
            ]
        )
        store.put(hr.release_log_key(release_tag, log), rows.to_csv(index=False))


def put_prerelease(store, rc_tag=RC_TAG, previous_release=PREVIOUS_RELEASE):
    """publish a release candidate's archive, as publish_prerelease does"""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("harvest_restrictions.gpkg", f"gpkg {rc_tag}")
        z.writestr("harvest_restrictions_sources.gpkg", "designations gpkg")
        z.writestr("sources.csv", SOURCES_CSV_BODY)
        for log in hr.LOGS:
            z.writestr(hr.summary(log), summary_body(log, previous_release))
    tags = {
        "commit": COMMIT,
        "prerelease": rc_tag,
        "previous_release": previous_release,
    }
    store.put(hr.prerelease_key(rc_tag), out.getvalue(), tags)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """a store holding one previous release, plus a release candidate of COMMIT, which is
    checked out and tagged with the candidate and the release"""
    memory = MemoryStore()
    put_release_log(memory, PREVIOUS_RELEASE, "2026-02-10T15:34:35-08:00")
    put_prerelease(memory)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hr.subprocess, "check_output", fake_git())
    monkeypatch.setattr(hr, "add_gpkg_layer", fake_add_gpkg_layer)
    monkeypatch.setattr(hr, "zip_files", fake_zip_files)
    return memory


def checkout(monkeypatch, **kwargs):
    monkeypatch.setattr(hr.subprocess, "check_output", fake_git(**kwargs))


def run_release(store):
    hr.publish_release(store, out_dir=".")


# --- committed_head ---


def test_committed_head(store):
    assert hr.committed_head() == COMMIT


def test_committed_head_uncommitted_changes(store, monkeypatch):
    checkout(monkeypatch, status=" M sql/overlay.sql")
    with pytest.raises(ValueError, match="uncommitted changes") as e:
        hr.committed_head()
    # the changed files are listed
    assert "sql/overlay.sql" in str(e.value)


def test_committed_head_ignores_untracked_files(store, monkeypatch):
    # git is asked to leave untracked files out
    calls = []
    git = fake_git()

    def fake(cmd, *args, **kwargs):
        calls.append(cmd)
        return git(cmd)

    monkeypatch.setattr(hr.subprocess, "check_output", fake)
    hr.committed_head()
    assert ["git", "status", "--porcelain", "-z", "--untracked-files=no"] in calls


def test_committed_head_allows_docs(store, monkeypatch):
    checkout(monkeypatch, status=" M README.md\0M  CHANGES.txt\0 M docs/notes.md\0")
    assert hr.committed_head() == COMMIT


def test_committed_head_lists_only_non_docs(store, monkeypatch):
    checkout(monkeypatch, status=" M README.md\0 M sql/overlay.sql\0")
    with pytest.raises(ValueError, match="uncommitted changes") as e:
        hr.committed_head()
    assert "sql/overlay.sql" in str(e.value)
    assert "README.md" not in str(e.value)


@pytest.mark.parametrize(
    "status",
    [
        # a doc renamed to (or copied as) a file that isn't one, and the reverse
        "R  harvest_restrictions.py\0README.md\0",
        "C  sources.json\0README.md\0",
        "R  notes.md\0sql/overlay.sql\0",
    ],
    ids=["doc renamed to code", "doc copied to data", "code renamed to doc"],
)
def test_committed_head_renames(store, monkeypatch, status):
    checkout(monkeypatch, status=status)
    with pytest.raises(ValueError, match="uncommitted changes"):
        hr.committed_head()


def test_checkout_state_ignores_docs(store, monkeypatch):
    checkout(monkeypatch, status=" M README.md\0 M sources.json\0")
    assert hr.checkout_state() == (COMMIT, " M sources.json")


# --- release_candidate ---


def test_release_candidate(store, monkeypatch):
    # the newest candidate of the commit - rc.10 is newer than rc.9
    checkout(monkeypatch, tags=["v2026-11-rc.9", "v2026-11-rc.10"])
    assert hr.release_candidate(store) == ("v2026-11-rc.10", COMMIT)


def test_release_candidate_untagged(store, monkeypatch):
    checkout(monkeypatch, tags=[RELEASE_TAG])
    with pytest.raises(ValueError, match="no release candidate tag"):
        hr.release_candidate(store)


def test_release_candidate_already_published(store, monkeypatch):
    checkout(monkeypatch, tags=[RC_TAG])
    with pytest.raises(ValueError, match="already published"):
        hr.release_candidate(store)


def test_release_candidate_uncommitted_changes(store, monkeypatch):
    checkout(monkeypatch, tags=["v2026-11-rc.1"], status=" M sources.json")
    with pytest.raises(ValueError, match="uncommitted changes"):
        hr.release_candidate(store)


# --- check_overlay_run ---


def test_check_overlay_run():
    hr.check_overlay_run({"git_commit": COMMIT, "uncommitted_changes": ""}, COMMIT)


@pytest.mark.parametrize(
    "overlay_run, match",
    [
        (None, "run overlay first"),
        ({"git_commit": "f00", "uncommitted_changes": ""}, "of commit f00"),
        (
            {"git_commit": COMMIT, "uncommitted_changes": " M sql/overlay.sql"},
            "uncommitted changes",
        ),
    ],
    ids=["no run", "other commit", "uncommitted changes"],
)
def test_check_overlay_run_refuses(overlay_run, match):
    with pytest.raises(ValueError, match=match):
        hr.check_overlay_run(overlay_run, COMMIT)


# --- publish_prerelease ---

NEW_RC = "v2026-11-rc.1"


@pytest.fixture
def run_dir(tmp_path):
    """a run's outputs and area totals, as overlay writes them"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / hr.OVERLAY_GPKG).write_text("gpkg")
    (run_dir / hr.SOURCES_GPKG).write_text("designations gpkg")
    (run_dir / hr.SOURCES_CSV).write_text(SOURCES_CSV_BODY)
    for log, row in LOG_ROWS.items():
        pandas.DataFrame([{**row, "area_ha": 120.0}]).to_csv(
            run_dir / f"{log}.csv", index=False
        )
    return str(run_dir)


def test_publish_prerelease(store, run_dir, tmp_path):
    hr.publish_prerelease(store, NEW_RC, COMMIT, run_dir, out_dir=str(tmp_path))

    key = hr.prerelease_key(NEW_RC)
    assert store.uploads == [key]
    assert key == "prereleases/harvest_restrictions_v2026-11-rc.1.zip"
    assert store.tags(key) == {
        "commit": COMMIT,
        "prerelease": NEW_RC,
        "previous_release": PREVIOUS_RELEASE,
    }

    # the geopackages, sources and summaries comparing the run's totals to the previous
    # release
    members = zip_members(store.objects[key]["body"])
    assert sorted(members) == [
        "harvest_restrictions.gpkg",
        "harvest_restrictions_sources.gpkg",
        "harvest_restrictions_summary.csv",
        "land_designations_summary.csv",
        "sources.csv",
    ]
    summary = pandas.read_csv(io.BytesIO(members["harvest_restrictions_summary.csv"]))
    assert list(summary.columns) == [
        "harvest_restriction_class_rank",
        "harvest_restriction_class_name",
        "current",
        PREVIOUS_RELEASE,
        "diff",
        "pct_diff",
    ]
    assert summary.iloc[0]["diff"] == 20

    # kept locally
    assert (tmp_path / key).read_bytes() == store.objects[key]["body"]


def test_publish_prerelease_then_release(store, run_dir, tmp_path, monkeypatch):
    # what publish_prerelease publishes is what publish_release reads
    hr.publish_prerelease(store, NEW_RC, COMMIT, run_dir, out_dir=str(tmp_path))

    checkout(monkeypatch, tags=["v2026-11", NEW_RC])
    run_release(store)
    rows = store.read_csv(hr.release_log_key("v2026-11", "harvest_restrictions"))
    assert list(rows["area_ha"]) == [120]


def test_publish_prerelease_no_release_yet(store, run_dir):
    for key in store.list("logs/"):
        del store.objects[key]
    with pytest.raises(ValueError, match="release at least one version"):
        hr.publish_prerelease(store, NEW_RC, COMMIT, run_dir)
    assert store.uploads == []


def test_publish_prerelease_uncommitted_changes(store, run_dir, monkeypatch):
    # a file edited during the run
    checkout(monkeypatch, status=" M sources.json")
    with pytest.raises(ValueError, match="uncommitted changes"):
        hr.publish_prerelease(store, NEW_RC, COMMIT, run_dir)
    assert store.uploads == []


def test_publish_prerelease_head_moved(store, run_dir, monkeypatch):
    # a commit made (or another commit checked out) during the run
    checkout(monkeypatch, head="f00")
    with pytest.raises(ValueError, match="moved from"):
        hr.publish_prerelease(store, NEW_RC, COMMIT, run_dir)
    assert store.uploads == []


# --- publish_release ---


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

    tags = {"commit": COMMIT, "release": RELEASE_TAG, "prerelease": RC_TAG}
    for key in store.uploads:
        assert store.tags(key) == tags

    # the archive is the candidate's, as is
    assert store.objects[ARCHIVE_KEY]["body"] == store.objects[RC_KEY]["body"]

    # the latest files are the archive's, as is
    members = zip_members(store.objects[RC_KEY]["body"])
    assert latest_keys == [f"latest/{name}" for name in members]
    for name, body in members.items():
        assert store.objects[f"latest/{name}"]["body"] == body

    # the candidate is left for prune-prereleases
    assert RC_KEY in store.objects


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
    assert row["commit"] == COMMIT[:7]
    assert row["prerelease"] == RC_TAG
    assert pandas.notna(row["released_at"])

    designations = store.read_csv(hr.release_log_key(RELEASE_TAG, "land_designations"))
    assert list(designations.columns[:5]) == hr.LOGS["land_designations"]

    # the combined logs, rebuilt from every release's rows
    for log in hr.LOGS:
        combined = store.read_csv(hr.combined_log_key(log))
        assert list(combined["release_tag"]) == [PREVIOUS_RELEASE, RELEASE_TAG]


def test_release_newest_candidate(store, monkeypatch):
    # the newest candidate of the version on the commit - not one of another version
    put_prerelease(store, "v2026-10-rc.2")
    put_prerelease(store, "v2026-11-rc.3")
    checkout(monkeypatch, tags=[RELEASE_TAG, RC_TAG, "v2026-10-rc.2", "v2026-11-rc.3"])

    run_release(store)
    assert store.tags(ARCHIVE_KEY)["prerelease"] == "v2026-10-rc.2"


def test_release_no_candidate_on_commit(store, monkeypatch):
    # candidates of the version on other commits don't count
    checkout(monkeypatch, tags=[RELEASE_TAG, "v2026-09-rc.1"])
    with pytest.raises(ValueError, match="No release candidate of v2026-10"):
        run_release(store)
    assert store.uploads == []


@pytest.mark.parametrize("tags", [[RC_TAG], [RELEASE_TAG, "v2026-10b", RC_TAG]])
def test_release_needs_one_release_tag(store, monkeypatch, tags):
    checkout(monkeypatch, tags=tags)
    with pytest.raises(ValueError, match="exactly one release tag"):
        run_release(store)
    assert store.uploads == []


def test_release_candidate_not_published(store, monkeypatch):
    checkout(monkeypatch, tags=[RELEASE_TAG, RC_TAG, "v2026-10-rc.2"])
    with pytest.raises(ValueError, match="v2026-10-rc.2 isn't published"):
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
    # another release since the candidate ran - its diffs are against an older release
    put_release_log(store, "v2026-05", "2026-05-01T00:00:00+00:00")

    with pytest.raises(ValueError, match="older release"):
        run_release(store)
    assert store.uploads == []


def test_release_clears_leftover_files(store, tmp_path):
    # a latest file from a failed release attempt
    leftover = tmp_path / "releases" / RELEASE_TAG / "latest"
    leftover.mkdir(parents=True)
    (leftover / "harvest_restrictions.gpkg").write_text("stale\n")

    run_release(store)
    assert b"stale" not in store.objects["latest/harvest_restrictions.gpkg"]["body"]


def test_release_builds_locally_by_key(store, tmp_path):
    run_release(store)

    # each published file is built at its key, under releases/<release_tag>/
    release_dir = tmp_path / "releases" / RELEASE_TAG
    for key in store.uploads:
        assert (release_dir / key).read_bytes() == store.objects[key]["body"]


# --- read_release_logs ---


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


# --- delete_prereleases ---


def candidates(store):
    return [os.path.basename(k) for k in store.list("prereleases/")]


def test_prune_released_versions(store):
    put_prerelease(store, "v2026-02-rc.1")
    put_prerelease(store, "v2026-02-rc.2")

    hr.delete_prereleases(store, out_dir=".")
    # just the released version's - v2026-10 isn't released yet
    assert candidates(store) == [f"harvest_restrictions_{RC_TAG}.zip"]


def test_prune_after_release(store):
    run_release(store)

    hr.delete_prereleases(store, out_dir=".")
    assert candidates(store) == []


def test_prune_keeps_partially_released_version(store):
    # a release that failed between publishing its change log files - its retry still
    # needs the candidate
    put_release_log(
        store, RELEASE_TAG, "2026-10-02T00:00:00+00:00", logs=["land_designations"]
    )

    hr.delete_prereleases(store, out_dir=".")
    assert candidates(store) == [f"harvest_restrictions_{RC_TAG}.zip"]


def test_prune_dry_run(store):
    put_prerelease(store, "v2026-02-rc.1")

    hr.delete_prereleases(store, dry_run=True, out_dir=".")
    assert len(candidates(store)) == 2


def test_prune_ignores_other_keys(store):
    store.put("prereleases/notes.txt", "?")
    store.put("prereleases/harvest_restrictions_v2026-02.zip", "not a candidate")

    hr.delete_prereleases(store, out_dir=".")
    assert "prereleases/notes.txt" in store.objects
    assert "prereleases/harvest_restrictions_v2026-02.zip" in store.objects


def test_prune_local_copies(store, tmp_path):
    put_prerelease(store, "v2026-02-rc.1")
    for rc_tag in ("v2026-02-rc.1", RC_TAG):
        local = tmp_path / hr.prerelease_key(rc_tag)
        local.parent.mkdir(exist_ok=True)
        local.write_text("zip")

    hr.delete_prereleases(store, dry_run=True, out_dir=str(tmp_path))
    assert (tmp_path / hr.prerelease_key("v2026-02-rc.1")).exists()

    hr.delete_prereleases(store, out_dir=str(tmp_path))
    # the released version's local copy goes with it - the unreleased version's is kept
    assert not (tmp_path / hr.prerelease_key("v2026-02-rc.1")).exists()
    assert (tmp_path / hr.prerelease_key(RC_TAG)).exists()


def test_prune_without_local_copy(store, tmp_path):
    # a candidate published from elsewhere (eg CI) has no local copy to delete
    put_prerelease(store, "v2026-02-rc.1")

    hr.delete_prereleases(store, out_dir=str(tmp_path))
    assert candidates(store) == [f"harvest_restrictions_{RC_TAG}.zip"]
