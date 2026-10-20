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
RC_KEY = f"drafts/harvest_restrictions_{RC_TAG}.zip"
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

    def put(self, key, body):
        if isinstance(body, str):
            body = body.encode()
        self.objects[key] = body

    def read_csv(self, key):
        return pandas.read_csv(io.BytesIO(self.objects[key]))

    def upload(self, local_file, key):
        with open(local_file, "rb") as f:
            self.put(key, f.read())
        self.uploads.append(key)

    def download(self, key, local_file):
        if key not in self.objects:
            raise subprocess.CalledProcessError(1, f"aws s3 cp {key}")
        os.makedirs(os.path.dirname(local_file) or ".", exist_ok=True)
        with open(local_file, "wb") as f:
            f.write(self.objects[key])

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


def put_release_log(store, release_tag, logs=None):
    """append one release's rows to the change logs, as publish_release does"""
    for log, row in LOG_ROWS.items():
        if logs and log not in logs:
            continue
        rows = pandas.DataFrame(
            [
                {
                    "release_tag": release_tag,
                    **row,
                    "area_ha": 100.0,
                    "release_date": "2026-01-01",
                    "commit": "0000000",
                }
            ]
        )
        key = f"logs/{log}.csv"
        if key in store.objects:
            rows = pandas.concat([store.read_csv(key), rows], ignore_index=True)
        store.put(key, rows.to_csv(index=False))


def put_release(store, release_tag):
    """a finished release - its change log rows, and its archive"""
    put_release_log(store, release_tag)
    store.put(hr.release_key(release_tag), f"archive {release_tag}")


def put_prerelease(store, rc_tag=RC_TAG, previous_release=PREVIOUS_RELEASE):
    """publish a release candidate's archive, as publish_prerelease does"""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("harvest_restrictions.gpkg", f"gpkg {rc_tag}")
        z.writestr("harvest_restrictions_sources.gpkg", "designations gpkg")
        z.writestr("sources.csv", SOURCES_CSV_BODY)
        for log in hr.LOGS:
            z.writestr(f"{log}_summary.csv", summary_body(log, previous_release))
    store.put(hr.prerelease_key(rc_tag), out.getvalue())


@pytest.fixture
def store(tmp_path, monkeypatch):
    """a store holding one previous release, plus a release candidate of COMMIT, which is
    checked out and tagged with the candidate and the release"""
    memory = MemoryStore()
    put_release(memory, PREVIOUS_RELEASE)
    put_prerelease(memory)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hr.subprocess, "check_output", fake_git())
    monkeypatch.setattr(hr, "add_gpkg_layer", fake_add_gpkg_layer)
    monkeypatch.setattr(hr, "zip_files", fake_zip_files)
    return memory


def checkout(monkeypatch, **kwargs):
    monkeypatch.setattr(hr.subprocess, "check_output", fake_git(**kwargs))


def run_release(store):
    hr.publish_release(store)


# --- uncommitted_changes ---


def test_uncommitted_changes(store, monkeypatch):
    checkout(monkeypatch, status=" M sql/overlay.sql\0")
    assert hr.uncommitted_changes() == [" M sql/overlay.sql"]


def test_uncommitted_changes_ignores_untracked_files(store, monkeypatch):
    # git is asked to leave untracked files out
    calls = []
    git = fake_git()

    def fake(cmd, *args, **kwargs):
        calls.append(cmd)
        return git(cmd)

    monkeypatch.setattr(hr.subprocess, "check_output", fake)
    hr.uncommitted_changes()
    assert ["git", "status", "--porcelain", "-z", "--untracked-files=no"] in calls


def test_uncommitted_changes_ignores_docs(store, monkeypatch):
    checkout(monkeypatch, status=" M README.md\0M  CHANGES.txt\0 M docs/notes.md\0")
    assert hr.uncommitted_changes() == []


def test_uncommitted_changes_lists_only_non_docs(store, monkeypatch):
    checkout(monkeypatch, status=" M README.md\0 M sql/overlay.sql\0")
    assert hr.uncommitted_changes() == [" M sql/overlay.sql"]


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
def test_uncommitted_changes_renames(store, monkeypatch, status):
    checkout(monkeypatch, status=status)
    assert len(hr.uncommitted_changes()) == 1


def test_checkout_state_ignores_docs(store, monkeypatch):
    checkout(monkeypatch, status=" M README.md\0 M sources.json\0")
    assert hr.checkout_state() == (COMMIT, [" M sources.json"])


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
    # the checkout needn't be clean - the draft is what's published, and check_draft checks it
    checkout(monkeypatch, tags=["v2026-11-rc.1"], status=" M sources.json")
    assert hr.release_candidate(store) == ("v2026-11-rc.1", COMMIT)


# --- draft ---


def test_overlay_run_json(tmp_path):
    hr.write_overlay_run(str(tmp_path), COMMIT, [" M sql/overlay.sql"])
    draft = hr.read_overlay_run(str(tmp_path))
    assert draft["commit"] == COMMIT
    assert draft["uncommitted_changes"] == [" M sql/overlay.sql"]
    assert pandas.notna(pandas.to_datetime(draft["finished_at"]))


def test_read_overlay_run_unfinished(tmp_path):
    # no overlay_run.json - the overlay didn't finish (or hasn't run)
    assert hr.read_overlay_run(str(tmp_path)) is None


def test_check_draft():
    hr.check_draft({"commit": COMMIT, "uncommitted_changes": []}, COMMIT)


@pytest.mark.parametrize(
    "draft, match",
    [
        (None, "run overlay first"),
        ({"commit": "f00", "uncommitted_changes": []}, "of commit f00"),
        (
            {"commit": COMMIT, "uncommitted_changes": [" M sql/overlay.sql"]},
            "uncommitted changes",
        ),
    ],
    ids=["no draft", "other commit", "uncommitted changes"],
)
def test_check_draft_refuses(draft, match):
    with pytest.raises(ValueError, match=match):
        hr.check_draft(draft, COMMIT)


# --- publish_prerelease ---

NEW_RC = "v2026-11-rc.1"


@pytest.fixture
def draft_dir(tmp_path):
    """a draft of COMMIT, as overlay writes it"""
    draft_dir = tmp_path / "drafts"
    draft_dir.mkdir()
    (draft_dir / "harvest_restrictions.gpkg").write_text("gpkg")
    (draft_dir / "harvest_restrictions_sources.gpkg").write_text("designations gpkg")
    (draft_dir / "sources.csv").write_text(SOURCES_CSV_BODY)
    for log, row in LOG_ROWS.items():
        pandas.DataFrame([{**row, "area_ha": 120.0}]).to_csv(
            draft_dir / f"{log}.csv", index=False
        )
    hr.write_overlay_run(str(draft_dir), COMMIT, [])
    return str(draft_dir)


def test_publish_prerelease(store, draft_dir, tmp_path):
    hr.publish_prerelease(store, NEW_RC, COMMIT, draft_dir)

    key = hr.prerelease_key(NEW_RC)
    assert store.uploads == [key]
    assert key == "drafts/harvest_restrictions_v2026-11-rc.1.zip"

    # the geopackages, sources and summaries comparing the draft's totals to the previous
    # release - not overlay_run.json
    members = zip_members(store.objects[key])
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

    # nothing is kept locally but the draft (in tmp_path/drafts/), left as is - the summaries
    # aren't written to it
    assert os.listdir(tmp_path) == ["drafts"]
    assert sorted(os.listdir(draft_dir)) == sorted(
        [
            "overlay_run.json",
            "sources.csv",
            "harvest_restrictions.gpkg",
            "harvest_restrictions_sources.gpkg",
        ]
        + [f"{log}.csv" for log in hr.LOGS]
    )


def test_publish_prerelease_then_release(store, draft_dir, monkeypatch):
    # what publish_prerelease publishes is what publish_release reads
    hr.publish_prerelease(store, NEW_RC, COMMIT, draft_dir)

    checkout(monkeypatch, tags=["v2026-11", NEW_RC])
    run_release(store)
    rows = store.read_csv("logs/harvest_restrictions.csv")
    assert list(rows[rows["release_tag"] == "v2026-11"]["area_ha"]) == [120]


def test_publish_prerelease_no_release_yet(store, draft_dir):
    for key in store.list("logs/"):
        del store.objects[key]
    with pytest.raises(ValueError, match="release at least one version"):
        hr.publish_prerelease(store, NEW_RC, COMMIT, draft_dir)
    assert store.uploads == []


@pytest.mark.parametrize(
    "commit, changes, match",
    [
        ("f00", [], "of commit f00"),
        (COMMIT, [" M sources.json"], "uncommitted changes"),
        (
            COMMIT,
            ["the checkout changed during the run"],
            "the checkout changed during the run",
        ),
    ],
    ids=["other commit", "uncommitted changes", "checkout changed"],
)
def test_publish_prerelease_refuses_draft(store, draft_dir, commit, changes, match):
    hr.write_overlay_run(draft_dir, commit, changes)
    with pytest.raises(ValueError, match=match):
        hr.publish_prerelease(store, NEW_RC, COMMIT, draft_dir)
    assert store.uploads == []


def test_publish_prerelease_unfinished_draft(store, draft_dir):
    os.remove(os.path.join(draft_dir, "overlay_run.json"))
    with pytest.raises(ValueError, match="run overlay first"):
        hr.publish_prerelease(store, NEW_RC, COMMIT, draft_dir)
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
    log_keys = ["logs/land_designations.csv", "logs/harvest_restrictions.csv"]

    # the archive last, marking the release as done
    assert store.uploads == latest_keys + log_keys + [ARCHIVE_KEY]

    # the archive is the candidate's, as is
    assert store.objects[ARCHIVE_KEY] == store.objects[RC_KEY]

    # the latest files are the archive's, as is
    members = zip_members(store.objects[RC_KEY])
    assert latest_keys == [f"latest/{name}" for name in members]
    for name, body in members.items():
        assert store.objects[f"latest/{name}"] == body

    # the candidate is left for prune-prereleases
    assert RC_KEY in store.objects


def test_release_logs(store):
    run_release(store)

    # this release's rows, appended to the previous release's
    for log in hr.LOGS:
        rows = store.read_csv(f"logs/{log}.csv")
        assert list(rows["release_tag"]) == [PREVIOUS_RELEASE, RELEASE_TAG]

    # the class dropped since the previous release isn't logged (as zero, or at all)
    rows = store.read_csv("logs/harvest_restrictions.csv")
    rows = rows[rows["release_tag"] == RELEASE_TAG]
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row["harvest_restriction_class_rank"] == 1
    assert row["area_ha"] == 110
    assert row["release_tag"] == RELEASE_TAG
    assert row["release_date"] == RELEASE_DATE
    assert row["commit"] == COMMIT[:7]
    assert "prerelease" not in rows.columns

    designations = store.read_csv("logs/land_designations.csv")
    # release_tag first, then the category columns
    assert (
        list(designations.columns[:6]) == ["release_tag"] + hr.LOGS["land_designations"]
    )


def test_release_newest_candidate(store, monkeypatch):
    # the newest candidate of the version on the commit - not one of another version
    put_prerelease(store, "v2026-10-rc.2")
    put_prerelease(store, "v2026-11-rc.3")
    checkout(monkeypatch, tags=[RELEASE_TAG, RC_TAG, "v2026-10-rc.2", "v2026-11-rc.3"])

    run_release(store)
    assert (
        store.objects[ARCHIVE_KEY] == store.objects[hr.prerelease_key("v2026-10-rc.2")]
    )


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
    store.put(ARCHIVE_KEY, "archive")

    with pytest.raises(ValueError, match="already been released"):
        run_release(store)
    assert store.uploads == []


def test_release_partially_logged(store):
    # a previous attempt failed between publishing its change log files - its rows are
    # replaced, not logged twice
    put_release_log(store, RELEASE_TAG, logs=["land_designations"])

    run_release(store)
    for log in hr.LOGS:
        rows = store.read_csv(f"logs/{log}.csv")
        assert list(rows["release_tag"]) == [PREVIOUS_RELEASE, RELEASE_TAG]


def test_release_stale_summaries(store):
    # another release since the candidate ran - its diffs are against an older release
    put_release(store, "v2026-05")

    with pytest.raises(ValueError, match="older release"):
        run_release(store)
    assert store.uploads == []


def test_release_keeps_nothing_locally(store, tmp_path):
    run_release(store)
    assert os.listdir(tmp_path) == []


# --- read_release_logs ---


def test_read_release_logs(store, tmp_path):
    # the logs in release order, as appended - tag names needn't follow it
    put_release(store, "v2025-08")

    histories = hr.read_release_logs(store, str(tmp_path))
    for history in histories.values():
        assert list(history["release_tag"]) == [PREVIOUS_RELEASE, "v2025-08"]
    assert hr.latest_release(histories["harvest_restrictions"]) == "v2025-08"


def test_read_release_logs_ignores_other_keys(store, tmp_path):
    # other files under logs/, eg the per-release folders of earlier releases, aren't read
    rows = store.objects["logs/harvest_restrictions.csv"]
    store.put(
        "logs/v2026-05/harvest_restrictions.csv", rows.replace(b"v2026-02", b"v2026-05")
    )
    store.put("logs/notes.txt", "notes")

    histories = hr.read_release_logs(store, str(tmp_path))
    assert list(histories["harvest_restrictions"]["release_tag"]) == [PREVIOUS_RELEASE]


def test_read_release_logs_none_yet(tmp_path):
    histories = hr.read_release_logs(MemoryStore(), str(tmp_path))
    assert histories == {log: None for log in hr.LOGS}


# --- delete_prereleases ---


def candidates(store):
    return [os.path.basename(k) for k in store.list("drafts/")]


def test_prune_released_versions(store):
    put_prerelease(store, "v2026-02-rc.1")
    put_prerelease(store, "v2026-02-rc.2")

    hr.delete_prereleases(store)
    # just the released version's - v2026-10 isn't released yet
    assert candidates(store) == [f"harvest_restrictions_{RC_TAG}.zip"]


def test_prune_after_release(store):
    run_release(store)

    hr.delete_prereleases(store)
    assert candidates(store) == []


def test_prune_keeps_partially_released_version(store):
    # a release that failed before publishing its archive - its retry still needs the
    # candidate
    put_release_log(store, RELEASE_TAG)

    hr.delete_prereleases(store)
    assert candidates(store) == [f"harvest_restrictions_{RC_TAG}.zip"]


def test_prune_dry_run(store):
    put_prerelease(store, "v2026-02-rc.1")

    hr.delete_prereleases(store, dry_run=True)
    assert len(candidates(store)) == 2


def test_prune_ignores_other_keys(store):
    store.put("drafts/notes.txt", "?")
    store.put("drafts/harvest_restrictions_v2026-02.zip", "not a candidate")

    hr.delete_prereleases(store)
    assert "drafts/notes.txt" in store.objects
    assert "drafts/harvest_restrictions_v2026-02.zip" in store.objects
