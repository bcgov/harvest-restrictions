# --- WARNING - this file is 100% unreviewed AI slop

# Tests of publishing drafts and releases.
#
# The real publish_draft/publish_release code runs, but against fakes:
#   - MemoryStore stands in for the s3 bucket - a dict of {key: file contents}
#   - FakeGit stands in for the git command line - it records tags and pushes
#
# Most tests start from the `store` fixture: a bucket holding one previous release
# (v2026-02), and a published draft (v2026-10-draft1) of the checked-out commit. A test
# changes that starting point, runs publish_draft or publish_release, then checks what
# ended up in the bucket and in git.

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
DRAFT = "v2026-10-draft1"
PREVIOUS_RELEASE = "v2026-02"
NEW_VERSION = "v2026-11"
DRAFT_KEY = f"drafts/harvest_restrictions_{DRAFT}.zip"
ARCHIVE_KEY = f"releases/harvest_restrictions_{RELEASE_TAG}.zip"


# --- fakes ---


class MemoryStore:
    """in-memory stand-in for S3Store - objects holds each key's contents, and uploads
    the keys published, in order"""

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

    def copy(self, key, new_key):
        # published, like an upload
        self.objects[new_key] = self.objects[key]
        self.uploads.append(new_key)

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


class FakeGit:
    """stand-in for subprocess.check_output, answering the git commands the release code
    runs - with COMMIT checked out, and self.status as the output of git status.

    Tags it's asked to create are kept in self.tags ({tag: commit}), and the refs it's
    asked to push in self.pushed. Like check_output, it returns bytes.
    """

    def __init__(self):
        self.status = ""
        self.tags = {}
        self.pushed = []

    def __call__(self, cmd, **kwargs):
        args = cmd[1:]  # without the leading "git"
        if args[0] == "status":
            return self.status.encode()
        if args == ["rev-parse", "HEAD"]:
            return COMMIT.encode()
        if args[:2] == ["tag", "--list"]:
            tag = args[2]
            return tag.encode() if tag in self.tags else b""
        if args[0] == "rev-parse":
            # the commit a tag is on, asked for as <tag>^{commit}
            tag = args[1].removesuffix("^{commit}")
            return self.tags[tag].encode()
        if args[:2] == ["tag", "-a"]:
            # git tag -a <tag> -m <message> <commit>
            tag, commit = args[2], args[-1]
            self.tags[tag] = commit
            return b""
        if args[0] == "push":
            self.pushed.append(args[-1])
            return b""
        raise AssertionError(f"unexpected command {cmd}")


def fake_zip_files(out_zip, files):
    """plain zip in place of sozip, which isn't needed to check what's archived"""
    with zipfile.ZipFile(out_zip, "w") as z:
        for f in files:
            z.write(f, os.path.basename(f))


def unzip(body):
    """the files in a zip archive's contents, as {name: contents}, in archive order"""
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


def summary_body(log, previous_release):
    """the contents of a draft's <log>_summary.csv - just the columns release reads: the
    category, its "current" area, and its area in the previous release (a column named
    after that release, eg "v2026-02").

    harvest_restrictions has a second class, dropped since the previous release, so it has
    no current area.
    """
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
    return pandas.DataFrame(rows).to_csv(index=False)


def put_release_log(store, release_tag, logs=hr.LOGS):
    """append one release's rows to the change logs, as publish_release does"""
    for log in logs:
        rows = pandas.DataFrame(
            [{"release_tag": release_tag, **LOG_ROWS[log], "area_ha": 100.0}]
        )
        key = f"logs/{log}.csv"
        if key in store.objects:
            rows = pandas.concat([store.read_csv(key), rows], ignore_index=True)
        store.put(key, rows.to_csv(index=False))


def put_release(store, release_tag):
    """a finished release - its change log rows, and its archive"""
    put_release_log(store, release_tag)
    store.put(hr.release_key(release_tag), f"archive {release_tag}")


def put_draft(store, draft=DRAFT, previous_release=PREVIOUS_RELEASE, commit=COMMIT):
    """publish a draft's archive, as publish_draft does - with its files in
    ARCHIVED_FILES order"""
    overlay_run = {"commit": commit, "uncommitted_changes": [], "finished_at": "?"}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("harvest_restrictions.gpkg", f"gpkg {draft}")
        z.writestr("harvest_restrictions_sources.gpkg", "designations gpkg")
        z.writestr("sources.csv", "sources")
        z.writestr("overlay_run.json", json.dumps(overlay_run))
        for log in hr.LOGS:
            z.writestr(f"{log}_summary.csv", summary_body(log, previous_release))
    store.put(hr.draft_key(draft), out.getvalue())


@pytest.fixture
def git(monkeypatch):
    """a FakeGit, answering every git command run"""
    git = FakeGit()
    monkeypatch.setattr(hr.subprocess, "check_output", git)
    return git


@pytest.fixture
def store(monkeypatch, git):
    """a bucket holding one previous release, plus a published draft of COMMIT, which is
    checked out"""
    memory = MemoryStore()
    put_release(memory, PREVIOUS_RELEASE)
    put_draft(memory)

    monkeypatch.setattr(hr, "zip_files", fake_zip_files)
    return memory


@pytest.fixture
def draft_dir(tmp_path):
    """a draft of COMMIT, as overlay writes it"""
    draft_dir = tmp_path / "drafts"
    draft_dir.mkdir()
    (draft_dir / "harvest_restrictions.gpkg").write_text("gpkg")
    (draft_dir / "harvest_restrictions_sources.gpkg").write_text("designations gpkg")
    (draft_dir / "sources.csv").write_text("sources")
    for log, row in LOG_ROWS.items():
        pandas.DataFrame([{**row, "area_ha": 120.0}]).to_csv(
            draft_dir / f"{log}.csv", index=False
        )
    hr.write_overlay_run(str(draft_dir), COMMIT, [])
    return str(draft_dir)


def assert_nothing_published(store, git):
    """for a refused draft or release - nothing uploaded, and no tag pushed"""
    assert store.uploads == []
    assert git.pushed == []


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


# --- S3Store ---


def test_s3store_list_empty(monkeypatch):
    # the cli prints nothing at all when no objects match
    monkeypatch.setattr(
        hr.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=""),
    )
    assert hr.S3Store("bucket").list("logs/") == []


# --- zip_files ---


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


@pytest.mark.skipif(not shutil.which("sozip"), reason="requires GDAL")
def test_zip_files_readable_by_gdal(tmp_path):
    gpkg = tmp_path / "harvest_restrictions.gpkg"
    make_gpkg(gpkg, "harvest_restrictions")
    csv = tmp_path / "sources.csv"
    csv.write_text("sources")
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


# --- uncommitted_changes ---


# git status lines end with a NUL (\0), and a rename or copy is followed by its source
@pytest.mark.parametrize(
    "status, changes",
    [
        (" M sql/overlay.sql\0", [" M sql/overlay.sql"]),
        (" M README.md\0M  CHANGES.txt\0 M docs/notes.md\0", []),
        (" M README.md\0 M sql/overlay.sql\0", [" M sql/overlay.sql"]),
        # a doc renamed to (or copied as) a file that isn't one, and the reverse
        (
            "R  harvest_restrictions.py\0README.md\0",
            ["R  harvest_restrictions.py <- README.md"],
        ),
        ("C  sources.json\0README.md\0", ["C  sources.json <- README.md"]),
        ("R  notes.md\0sql/overlay.sql\0", ["R  notes.md <- sql/overlay.sql"]),
    ],
    ids=[
        "change",
        "docs only",
        "docs and a change",
        "doc renamed to code",
        "doc copied to data",
        "code renamed to doc",
    ],
)
def test_checkout_state(git, status, changes):
    git.status = status
    assert hr.checkout_state() == (COMMIT, changes)


# --- versions and draft names ---


@pytest.mark.parametrize(
    "name, version, draft",
    [
        ("v2026-10", True, False),
        ("v2026-10-15", True, False),
        ("v2026-10-draft1", False, True),
        ("v2026-10-draft10", False, True),
        ("v2026-10-15-draft1", False, True),
        ("v2026-10b", False, False),
        ("2026-10", False, False),
        ("v2026-1", False, False),
        ("v2026-10-DRAFT1", False, False),
        ("v2026-10-rc.1", False, False),
        ("v2026-10-draft01", False, False),
        ("v2026-10-draft0", False, False),
        ("v2026-10-draft1\n", False, False),
    ],
)
def test_names(name, version, draft):
    assert bool(hr.VERSION.fullmatch(name)) == version
    assert bool(hr.DRAFT.fullmatch(name)) == draft


# --- publish_draft ---


def test_publish_draft(store, draft_dir):
    assert hr.publish_draft(store, NEW_VERSION, draft_dir) == "v2026-11-draft1"

    key = "drafts/harvest_restrictions_v2026-11-draft1.zip"
    assert store.uploads == [key]

    # the geopackages, sources, the draft's overlay_run.json, and summaries comparing the
    # draft's totals to the previous release
    members = unzip(store.objects[key])
    assert sorted(members) == [
        "harvest_restrictions.gpkg",
        "harvest_restrictions_sources.gpkg",
        "harvest_restrictions_summary.csv",
        "land_designations_summary.csv",
        "overlay_run.json",
        "sources.csv",
    ]
    assert json.loads(members["overlay_run.json"])["commit"] == COMMIT
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


def test_publish_draft_numbering(store, draft_dir):
    # numbered after the version's published drafts - draft10 is after draft9, and a
    # hotfix's drafts are another version's
    put_draft(store, "v2026-11-draft1")
    put_draft(store, "v2026-11-draft9")
    put_draft(store, "v2026-11-15-draft12")
    assert hr.publish_draft(store, NEW_VERSION, draft_dir) == "v2026-11-draft10"


def test_publish_draft_checkout_needn_t_be_clean(store, draft_dir, git):
    # what's published is the draft, which check_draft checks - not the checkout
    git.status = " M sources.json\0"
    assert hr.publish_draft(store, NEW_VERSION, draft_dir) == "v2026-11-draft1"


def test_publish_draft_not_a_version(store, draft_dir, git):
    with pytest.raises(ValueError, match="isn't a version"):
        hr.publish_draft(store, "v2026-11-draft1", draft_dir)
    assert_nothing_published(store, git)


@pytest.mark.parametrize("version", ["v2026-01", "v2026-02", "v2026-01-31"])
def test_publish_draft_version_order(store, draft_dir, git, version):
    # the version must sort after the latest release, v2026-02
    with pytest.raises(ValueError, match="doesn't sort after the latest release"):
        hr.publish_draft(store, version, draft_dir)
    assert_nothing_published(store, git)


def test_publish_draft_then_release(store, draft_dir, git):
    # what publish_draft publishes is what publish_release reads
    draft = hr.publish_draft(store, NEW_VERSION, draft_dir)

    hr.publish_release(store, draft)
    rows = store.read_csv("logs/harvest_restrictions.csv")
    assert list(rows[rows["release_tag"] == NEW_VERSION]["area_ha"]) == [120]
    assert git.tags == {NEW_VERSION: COMMIT}


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
def test_publish_draft_refuses_draft(store, draft_dir, git, commit, changes, match):
    hr.write_overlay_run(draft_dir, commit, changes)
    with pytest.raises(ValueError, match=match):
        hr.publish_draft(store, NEW_VERSION, draft_dir)
    assert_nothing_published(store, git)


def test_publish_draft_unfinished_draft(store, draft_dir, git):
    os.remove(os.path.join(draft_dir, "overlay_run.json"))
    with pytest.raises(ValueError, match="run overlay first"):
        hr.publish_draft(store, NEW_VERSION, draft_dir)
    assert_nothing_published(store, git)


# --- publish_release ---


def test_release_publishes(store, git):
    draft = store.objects[DRAFT_KEY]
    hr.publish_release(store, DRAFT)

    latest_keys = [
        "latest/harvest_restrictions.gpkg",
        "latest/harvest_restrictions_sources.gpkg",
        "latest/sources.csv",
        "latest/overlay_run.json",
        "latest/land_designations_summary.csv",
        "latest/harvest_restrictions_summary.csv",
    ]
    log_keys = ["logs/land_designations.csv", "logs/harvest_restrictions.csv"]

    # the archive last, marking the release as done
    assert store.uploads == latest_keys + log_keys + [ARCHIVE_KEY]

    # the archive is the draft's, as is
    assert store.objects[ARCHIVE_KEY] == draft

    # the latest files are the archive's files, as is
    for name, body in unzip(draft).items():
        assert store.objects[f"latest/{name}"] == body

    # the draft's commit is tagged with the version, and the tag pushed
    assert git.tags == {RELEASE_TAG: COMMIT}
    assert git.pushed == [f"refs/tags/{RELEASE_TAG}"]


def test_release_logs(store):
    hr.publish_release(store, DRAFT)

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

    designations = store.read_csv("logs/land_designations.csv")
    # release_tag first, then the category columns
    assert (
        list(designations.columns[:6]) == ["release_tag"] + hr.LOGS["land_designations"]
    )


def test_release_named_draft(store, git):
    # the draft named, not the version's newest - and its commit is tagged
    put_draft(store, "v2026-10-draft2", commit="def5678")
    put_draft(store, "v2026-10-draft3")
    draft = store.objects[hr.draft_key("v2026-10-draft2")]

    hr.publish_release(store, "v2026-10-draft2")
    assert store.objects[ARCHIVE_KEY] == draft
    assert git.tags == {RELEASE_TAG: "def5678"}


def test_release_not_a_draft(store, git):
    with pytest.raises(ValueError, match="isn't a draft"):
        hr.publish_release(store, RELEASE_TAG)
    assert_nothing_published(store, git)


def test_release_draft_not_published(store, git):
    with pytest.raises(ValueError, match="v2026-10-draft2 isn't published"):
        hr.publish_release(store, "v2026-10-draft2")
    assert_nothing_published(store, git)


def test_release_already_tagged(store, git):
    # a retry, after tagging - the tag is pushed again, not recreated
    git.tags[RELEASE_TAG] = COMMIT

    hr.publish_release(store, DRAFT)
    assert git.tags == {RELEASE_TAG: COMMIT}
    assert git.pushed == [f"refs/tags/{RELEASE_TAG}"]
    assert ARCHIVE_KEY in store.objects


def test_release_tagged_elsewhere(store, git):
    git.tags[RELEASE_TAG] = "f00"

    with pytest.raises(ValueError, match="already on commit f00"):
        hr.publish_release(store, DRAFT)
    assert_nothing_published(store, git)


def test_release_already_released(store, git):
    store.put(ARCHIVE_KEY, "archive")

    with pytest.raises(ValueError, match="already been released"):
        hr.publish_release(store, DRAFT)
    assert_nothing_published(store, git)


def test_release_partially_logged(store):
    # a previous attempt failed between publishing its change log files - its rows are
    # replaced, not logged twice
    put_release_log(store, RELEASE_TAG, logs=["land_designations"])

    hr.publish_release(store, DRAFT)
    for log in hr.LOGS:
        rows = store.read_csv(f"logs/{log}.csv")
        assert list(rows["release_tag"]) == [PREVIOUS_RELEASE, RELEASE_TAG]


def test_release_version_order(store, git):
    # a later version released since the draft was published
    put_release(store, "v2026-11")

    with pytest.raises(
        ValueError, match="doesn't sort after the latest release, v2026-11"
    ):
        hr.publish_release(store, DRAFT)
    assert_nothing_published(store, git)


def test_release_stale_summaries(store, git):
    # another release since the draft ran - its diffs are against an older release
    put_release(store, "v2026-05")

    with pytest.raises(ValueError, match="older release"):
        hr.publish_release(store, DRAFT)
    assert_nothing_published(store, git)
    # the draft is kept - the release's retry (with a new draft) may still want it
    assert DRAFT_KEY in store.objects


def test_release_deletes_drafts(store):
    # every draft of the released version, not just the one released - not other
    # versions', or anything else
    put_draft(store, "v2026-10-draft2")
    put_draft(store, "v2026-11-draft1")
    store.put("drafts/notes.txt", "?")

    hr.publish_release(store, DRAFT)
    assert store.list("drafts/") == [
        "drafts/harvest_restrictions_v2026-11-draft1.zip",
        "drafts/notes.txt",
    ]
