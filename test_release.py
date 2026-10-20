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


def summary_body(log):
    """the contents of a draft's <log>_summary.csv - just the columns release reads: the
    category, its "current" area, and its area in the previous release (a column named
    after that release, eg "v2026-02").

    harvest_restrictions has a second class, dropped since the previous release, so it has
    no current area.
    """
    rows = [{**LOG_ROWS[log], "current": 110.0, PREVIOUS_RELEASE: 100.0}]
    if log == "harvest_restrictions":
        rows.append(
            {
                "harvest_restriction_class_rank": 2,
                "harvest_restriction_class_name": "Prohibited",
                "current": None,
                PREVIOUS_RELEASE: 50.0,
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


def put_draft(store, draft=DRAFT):
    """publish a draft's archive, as publish_draft does - with its files in
    ARCHIVED_FILES order"""
    overlay_run = {"commit": COMMIT, "uncommitted_changes": [], "finished_at": "?"}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("harvest_restrictions.gpkg", f"gpkg {draft}")
        z.writestr("harvest_restrictions_sources.gpkg", "designations gpkg")
        z.writestr("sources.csv", "sources")
        z.writestr("overlay_run.json", json.dumps(overlay_run))
        for log in hr.LOGS:
            z.writestr(f"{log}_summary.csv", summary_body(log))
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


def test_build_change(tmp_path):
    history = pandas.DataFrame(
        {
            "harvest_restriction_class_rank": [1, 2, 1, 2],
            "harvest_restriction_class_name": ["Protected", "Prohibited"] * 2,
            "area_ha": [999.0, 999.0, 100.0, 50.0],
            "release_tag": ["v2026-01", "v2026-01", "v2026-02", "v2026-02"],
        }
    )
    # class 2 dropped since v2026-02, class 3 new in this run
    current = tmp_path / "harvest_restrictions.csv"
    pandas.DataFrame(
        {
            "harvest_restriction_class_rank": [1, 3],
            "harvest_restriction_class_name": ["Protected", "High Restricted"],
            "area_ha": [110.4, 30.0],
        }
    ).to_csv(current, index=False)

    columns = ["harvest_restriction_class_rank", "harvest_restriction_class_name"]
    result = hr.build_change(current, history, "v2026-02", columns)

    # compared against v2026-02 only, not the older v2026-01 rows
    assert result.loc[1, "v2026-02"] == 100
    assert result.loc[1, "current"] == 110
    assert result.loc[1, "diff"] == 10
    assert result.loc[1, "pct_diff"] == 10.4

    # a dropped class keeps its name, with no current area
    assert result.loc[2, "harvest_restriction_class_name"] == "Prohibited"
    assert pandas.isna(result.loc[2, "current"])

    # a new class has its name, with no previous area
    assert result.loc[3, "harvest_restriction_class_name"] == "High Restricted"
    assert pandas.isna(result.loc[3, "v2026-02"])


# --- S3Store ---


def test_s3store_list_empty(monkeypatch):
    # the aws cli prints nothing at all when no objects match
    monkeypatch.setattr(
        hr.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=""),
    )
    assert hr.S3Store("bucket").list("logs/") == []


# --- zip_files ---


@pytest.mark.skipif(not shutil.which("sozip"), reason="requires GDAL")
def test_zip_files_readable_by_gdal(tmp_path):
    # GDAL can read a layer straight from the archive, as when re-running a release
    gpkg = tmp_path / "harvest_restrictions.gpkg"
    subprocess.run(
        f"ogr2ogr -f GPKG {gpkg} -dialect sqlite "
        "-sql 'select MakePoint(1, 2, 3005) as geom' :memory: -nln harvest_restrictions",
        shell=True,
        check=True,
    )
    archive = tmp_path / "harvest_restrictions_v1.zip"

    hr.zip_files(str(archive), [str(gpkg)])
    info = subprocess.run(
        f"ogrinfo -so /vsizip/{archive}/harvest_restrictions.gpkg harvest_restrictions",
        shell=True,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Feature Count: 1" in info


# --- uncommitted_changes ---


# git status lines end with a NUL (\0), and a rename is followed by its source
@pytest.mark.parametrize(
    "status, changes",
    [
        (" M sql/overlay.sql\0", [" M sql/overlay.sql"]),
        # changes to docs are ignored
        (" M README.md\0 M CHANGES.txt\0", []),
        # a doc renamed to code isn't a doc change
        ("R  harvest.py\0README.md\0", ["R  harvest.py <- README.md"]),
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
        ("v2026-10-15-draft12", False, True),
        ("v2026-10b", False, False),
        ("v2026-10-draft0", False, False),
        ("v2026-10-draft01", False, False),
    ],
)
def test_names(name, version, draft):
    assert bool(hr.VERSION.fullmatch(name)) == version
    assert bool(hr.DRAFT.fullmatch(name)) == draft


# --- publish_draft ---


def test_publish_draft(store, draft_dir):
    assert hr.publish_draft(store, NEW_VERSION, draft_dir) == "v2026-11-draft1"

    files = unzip(store.objects["drafts/harvest_restrictions_v2026-11-draft1.zip"])
    assert sorted(files) == sorted(hr.ARCHIVED_FILES)
    assert json.loads(files["overlay_run.json"])["commit"] == COMMIT

    # the summary compares the draft's area (120) to the previous release's (100)
    summary = pandas.read_csv(io.BytesIO(files["harvest_restrictions_summary.csv"]))
    assert summary.loc[0, "current"] == 120
    assert summary.loc[0, PREVIOUS_RELEASE] == 100


def test_publish_draft_numbering(store, draft_dir):
    # numbered after the version's published drafts - a hotfix's drafts don't count
    put_draft(store, "v2026-11-draft9")
    put_draft(store, "v2026-11-15-draft12")
    assert hr.publish_draft(store, NEW_VERSION, draft_dir) == "v2026-11-draft10"


@pytest.mark.parametrize(
    "version, error",
    [
        ("v2026-11-draft1", "isn't a version"),
        # the latest release is v2026-02
        ("v2026-02", "doesn't sort after the latest release"),
    ],
)
def test_publish_draft_bad_version(store, draft_dir, git, version, error):
    with pytest.raises(ValueError, match=error):
        hr.publish_draft(store, version, draft_dir)
    assert_nothing_published(store, git)


@pytest.mark.parametrize(
    "commit, changes, error",
    [
        ("f00", [], "of commit f00"),
        (COMMIT, [" M sources.json"], "uncommitted changes"),
    ],
)
def test_publish_draft_bad_draft(store, draft_dir, git, commit, changes, error):
    # the local draft is of another commit, or was run with uncommitted changes
    hr.write_overlay_run(draft_dir, commit, changes)
    with pytest.raises(ValueError, match=error):
        hr.publish_draft(store, NEW_VERSION, draft_dir)
    assert_nothing_published(store, git)


def test_publish_draft_then_release(store, draft_dir, git):
    # what publish_draft publishes is what publish_release reads
    draft = hr.publish_draft(store, NEW_VERSION, draft_dir)
    hr.publish_release(store, draft)

    log = store.read_csv("logs/harvest_restrictions.csv")
    assert list(log["release_tag"]) == [PREVIOUS_RELEASE, NEW_VERSION]
    assert list(log["area_ha"]) == [100, 120]
    assert git.tags == {NEW_VERSION: COMMIT}


# --- publish_release ---


def test_release(store, git):
    draft = store.objects[DRAFT_KEY]
    hr.publish_release(store, DRAFT)

    # the latest files, then the logs, then the archive - last, marking the release done
    latest_keys = [f"latest/{name}" for name in unzip(draft)]
    log_keys = ["logs/land_designations.csv", "logs/harvest_restrictions.csv"]
    assert store.uploads == latest_keys + log_keys + [ARCHIVE_KEY]

    # the archive, and the latest files, are the draft's
    assert store.objects[ARCHIVE_KEY] == draft
    for name, body in unzip(draft).items():
        assert store.objects[f"latest/{name}"] == body

    # the draft's commit is tagged with the version, and the tag pushed
    assert git.tags == {RELEASE_TAG: COMMIT}
    assert git.pushed == [f"refs/tags/{RELEASE_TAG}"]

    # each log has this release's rows after the previous release's. The class dropped
    # since the previous release (see summary_body) isn't logged
    log = store.read_csv("logs/harvest_restrictions.csv")
    assert list(log["release_tag"]) == [PREVIOUS_RELEASE, RELEASE_TAG]
    assert list(log.columns) == ["release_tag"] + hr.LOGS["harvest_restrictions"] + [
        "area_ha"
    ]


def test_release_deletes_drafts(store):
    # every draft of the released version - not other versions'
    put_draft(store, "v2026-10-draft2")
    put_draft(store, "v2026-11-draft1")

    hr.publish_release(store, DRAFT)
    assert store.list("drafts/") == ["drafts/harvest_restrictions_v2026-11-draft1.zip"]


def test_release_retry(store, git):
    # a previous attempt tagged the commit, and logged some of its rows, before failing -
    # the retry pushes the tag again, and replaces the rows rather than logging them twice
    git.tags[RELEASE_TAG] = COMMIT
    put_release_log(store, RELEASE_TAG, logs=["land_designations"])

    hr.publish_release(store, DRAFT)
    assert git.pushed == [f"refs/tags/{RELEASE_TAG}"]
    for log in hr.LOGS:
        rows = store.read_csv(f"logs/{log}.csv")
        assert list(rows["release_tag"]) == [PREVIOUS_RELEASE, RELEASE_TAG]


@pytest.mark.parametrize(
    "draft, error",
    [
        (RELEASE_TAG, "isn't a draft"),
        ("v2026-10-draft2", "isn't published"),
    ],
)
def test_release_bad_draft(store, git, draft, error):
    with pytest.raises(ValueError, match=error):
        hr.publish_release(store, draft)
    assert_nothing_published(store, git)


def test_release_already_released(store, git):
    store.put(ARCHIVE_KEY, "archive")
    with pytest.raises(ValueError, match="already been released"):
        hr.publish_release(store, DRAFT)
    assert_nothing_published(store, git)


def test_release_tagged_elsewhere(store, git):
    git.tags[RELEASE_TAG] = "f00"
    with pytest.raises(ValueError, match="already on commit f00"):
        hr.publish_release(store, DRAFT)
    assert_nothing_published(store, git)


@pytest.mark.parametrize(
    "release, error",
    [
        # a later version than the draft's
        ("v2026-11", "doesn't sort after the latest release"),
        # an earlier version, but after the release the draft's summaries compare to
        ("v2026-05", "older release"),
    ],
)
def test_release_since_draft(store, git, release, error):
    # another release was made after the draft was published
    put_release(store, release)
    with pytest.raises(ValueError, match=error):
        hr.publish_release(store, DRAFT)
    assert_nothing_published(store, git)
    # the draft is kept
    assert DRAFT_KEY in store.objects
