import copy
import io
import json
import os
import shutil
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone

import jsonschema
import pandas
import pytest

import datarelease as dr

# A project unrelated to harvest restrictions - datarelease knows nothing of the project it
# releases beyond its config. The colours log's summary is drafted (release needs it for the
# log) but not published as a latest file, or archived
CONFIG = {
    "prefix": "widgets",
    "archive": "widgets_{release_tag}.zip",
    # the same name as a latest file - the bundle holds more layers than that file
    "bundle": "widgets.gpkg",
    "outputs": [
        {"file": "widgets.gpkg.zip", "layer": "widgets", "latest": "widgets.gpkg"},
        {
            "file": "parts.csv",
            "layer": "parts",
            "latest": "parts.csv",
            "archive": True,
        },
        {
            "file": "widget_summary.csv",
            "latest": "widget_summary.csv",
            "archive": True,
        },
    ],
    "logs": [
        {
            "name": "widgets",
            "summary": "widget_summary.csv",
            "key": "widget_id",
            "columns": ["widget_id", "widget_name"],
            "value": "count",
        },
        {
            "name": "colours",
            "summary": "colour_summary.csv",
            "key": "colour",
            "columns": ["colour"],
            "value": "count",
        },
    ],
}

COMMIT = "abc1234def5678"
SHORT_COMMIT = "abc1234"
RELEASE_TAG = "v2026-10"
RELEASE_DATE = "2026-10-01T10:00:00-07:00"
RUN_ID = "20261001T000000Z"
PREVIOUS_RELEASE = "v2026-02"
# the run the fixture's previous release was made from
RELEASED_RUN = "20260101T000000Z"


# --- config ---


def test_config_valid():
    dr.validate_config(CONFIG)


def test_config_load(tmp_path):
    path = tmp_path / "release.json"
    path.write_text(json.dumps(CONFIG))
    assert dr.load_config(path) == CONFIG


@pytest.mark.parametrize(
    "change, error",
    [
        (lambda c: c.pop("prefix"), jsonschema.ValidationError),
        (lambda c: c.update(unknown=1), jsonschema.ValidationError),
        (lambda c: c["outputs"][0].update(latest="widgets.csv"), ValueError),
        (lambda c: c.pop("bundle"), ValueError),
        (lambda c: c["logs"][0].update(key="widget_size"), ValueError),
        (lambda c: c.update(archive="widgets.zip"), ValueError),
        (lambda c: c.pop("archive"), ValueError),
        (
            lambda c: (
                c.pop("archive"),
                c.pop("bundle"),
                c["outputs"][0].pop("layer"),
                c["outputs"][1].pop("layer"),
            ),
            ValueError,
        ),
        (lambda c: c.update(bundle="parts.csv"), ValueError),
    ],
    ids=[
        "no prefix",
        "unknown key",
        "latest not gpkg",
        "layer without bundle",
        "bad key",
        "archive not dated",
        "bundle without archive",
        "archived output without archive",
        "bundle named as archived output",
    ],
)
def test_config_invalid(change, error):
    config = copy.deepcopy(CONFIG)
    change(config)
    with pytest.raises(error):
        dr.validate_config(config)


def test_draft_files():
    assert dr.draft_files(CONFIG) == [
        "widgets.gpkg.zip",
        "parts.csv",
        "widget_summary.csv",
        "colour_summary.csv",
    ]


# --- build_change ---


@pytest.fixture
def history():
    return pandas.DataFrame(
        {
            "widget_id": [1, 2, 1, 2],
            "widget_name": ["sprocket", "gear"] * 2,
            "count": [999.0, 999.0, 100.0, 50.0],
            "release_tag": ["v2026-01", "v2026-01", "v2026-02", "v2026-02"],
        }
    )


@pytest.fixture
def current(tmp_path):
    # widget 2 dropped since v2026-02, widget 3 new in this run
    path = tmp_path / "widgets.csv"
    pandas.DataFrame(
        {
            "widget_id": [1, 3],
            "widget_name": ["sprocket", "cog"],
            "count": [110.4, 30.0],
        }
    ).to_csv(path, index=False)
    return path


def change(current, history, decimals=0):
    return dr.build_change(
        current,
        history,
        "v2026-02",
        "widget_id",
        ["widget_id", "widget_name"],
        "count",
        decimals,
    )


def test_build_change_diff(current, history):
    row = change(current, history).loc[1]
    # compared against v2026-02 only, not the older v2026-01 rows
    assert row["v2026-02"] == 100
    assert row["current"] == 110
    assert row["diff"] == 10
    assert row["pct_diff"] == 10.4


def test_build_change_decimals(current, history):
    row = change(current, history, decimals=1).loc[1]
    assert row["current"] == 110.4
    assert row["diff"] == 10.4


def test_build_change_new_and_dropped_categories(current, history):
    result = change(current, history)
    assert sorted(result.index) == [1, 2, 3]

    # dropped since the previous release - labelled from the previous release, no current value
    dropped = result.loc[2]
    assert dropped["widget_name"] == "gear"
    assert pandas.isna(dropped["current"])
    assert pandas.isna(dropped["diff"])

    # new in this run - labelled from this run, no previous value
    new = result.loc[3]
    assert new["widget_name"] == "cog"
    assert new["current"] == 30
    assert pandas.isna(new["v2026-02"])
    assert pandas.isna(new["pct_diff"])


def test_build_change_columns(current, history):
    result = change(current, history)
    assert result.index.name == "widget_id"
    assert list(result.columns) == [
        "widget_name",
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
            {"Contents": [{"Key": "widgets/drafts/b"}, {"Key": "widgets/drafts/a"}]}
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=out)

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    store = dr.S3Store("bucket", "widgets")
    # keys relative to the store's prefix, sorted
    assert store.list("drafts/") == ["drafts/a", "drafts/b"]
    assert calls[0][calls[0].index("--prefix") + 1] == "widgets/drafts/"


def test_s3store_list_empty(monkeypatch):
    # the cli prints nothing at all when no objects match
    monkeypatch.setattr(
        dr.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=""),
    )
    assert dr.S3Store("bucket", "widgets").list("drafts/") == []


@pytest.mark.skipif(not shutil.which("ogr2ogr"), reason="requires GDAL")
def test_add_gpkg_layer_from_zip(tmp_path):
    # a single-file zip is read without naming the file inside, which needn't match the zip's
    # name - e.g. a project's custom-named output published under its configured name
    source = tmp_path / "custom_name.gpkg"
    subprocess.run(
        [
            "ogr2ogr",
            "-f",
            "GPKG",
            str(source),
            "-dialect",
            "sqlite",
            "-sql",
            "select 1 as id, MakePoint(1, 2, 3005) as geom",
            ":memory:",
        ],
        check=True,
    )
    published = tmp_path / "widgets.gpkg.zip"
    with zipfile.ZipFile(published, "w") as z:
        z.write(source, source.name)

    out = tmp_path / "bundle.gpkg"
    dr.add_gpkg_layer(str(out), dr.ogr_path(str(published)), "widgets")
    info = subprocess.run(
        ["ogrinfo", "-so", "-al", str(out)], check=True, capture_output=True, text=True
    ).stdout
    assert "Layer name: widgets" in info
    assert "Feature Count: 1" in info


@pytest.mark.skipif(not shutil.which("sozip"), reason="requires GDAL")
def test_zip_files_readable_by_gdal(tmp_path):
    gpkg = tmp_path / "bundle.gpkg"
    subprocess.run(
        [
            "ogr2ogr",
            "-f",
            "GPKG",
            str(gpkg),
            "-dialect",
            "sqlite",
            "-sql",
            "select 1 as id, MakePoint(1, 2, 3005) as geom",
            ":memory:",
            "-nln",
            "widgets",
        ],
        check=True,
    )
    csv = tmp_path / "parts.csv"
    csv.write_text("part\nbolt\n")
    archive = tmp_path / "widgets_v1.zip"

    dr.zip_files(str(archive), [str(gpkg), str(csv)])
    with zipfile.ZipFile(archive) as z:
        assert sorted(z.namelist()) == ["bundle.gpkg", "parts.csv"]
    info = subprocess.run(
        ["ogrinfo", "-so", f"/vsizip/{archive}/bundle.gpkg", "widgets"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Feature Count: 1" in info


# --- release ---
#
# release() is run against an in-memory store, with git and ogr2ogr stubbed out


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
    if cmd[:2] == ["git", "describe"]:
        return RELEASE_TAG.encode()
    if cmd[:2] == ["git", "log"]:
        return RELEASE_DATE.encode()
    if cmd[:3] == ["git", "rev-parse", "--short"]:
        return SHORT_COMMIT.encode()
    if cmd[:2] == ["git", "rev-parse"]:
        return COMMIT.encode()
    raise AssertionError(f"unexpected command {cmd}")


def fake_add_gpkg_layer(out_file, source_file, layer_name, spatial=True):
    """record each layer as a line in out_file - failing, like ogr2ogr -update does, if the
    layer is already there"""
    try:
        with open(out_file) as f:
            layers = [line.split("\t")[0] for line in f.read().splitlines()]
    except FileNotFoundError:
        layers = []
    if layer_name in layers:
        raise subprocess.CalledProcessError(1, f"ogr2ogr: {layer_name} already exists")
    with open(out_file, "a") as f:
        f.write(f"{layer_name}\t{source_file}\t{spatial}\n")


def widget_summary(previous_release=PREVIOUS_RELEASE):
    """widget summary as publish_draft writes it - widget 2 dropped since the previous
    release, so it has no current value"""
    return pandas.DataFrame(
        {
            "widget_id": [1, 2],
            "widget_name": ["sprocket", "gear"],
            "current": [110.0, None],
            previous_release: [100.0, 50.0],
            "diff": [10.0, None],
            "pct_diff": [10.0, None],
        }
    ).to_csv(index=False)


def colour_summary(previous_release=PREVIOUS_RELEASE):
    return pandas.DataFrame(
        {
            "colour": ["red"],
            "current": [6.0],
            previous_release: [5.0],
            "diff": [1.0],
            "pct_diff": [20.0],
        }
    ).to_csv(index=False)


def put_release_log(store, release_tag, released_at, names=None, run_id=RELEASED_RUN):
    """publish one release's change log rows, as release() does"""
    rows = {
        "widgets": {"widget_id": 1, "widget_name": "sprocket", "count": 100.0},
        "colours": {"colour": "red", "count": 5.0},
    }
    for name, row in rows.items():
        if names and name not in names:
            continue
        log = pandas.DataFrame(
            [
                {
                    **row,
                    "release_tag": release_tag,
                    "release_date": "2026-01-01",
                    "commit": "0000000",
                    "run_id": run_id,
                    "released_at": released_at,
                }
            ]
        )
        store.put(dr.release_log_key(release_tag, name), log.to_csv(index=False))


def put_run(
    store,
    run_id=RUN_ID,
    commit=COMMIT,
    previous_release=PREVIOUS_RELEASE,
    complete=True,
):
    """publish a full set of draft files, as publish_draft does"""
    bodies = {
        "widgets.gpkg.zip": f"widgets zip {run_id}",
        "parts.csv": "part,widget_id\nbolt,1\n",
        "widget_summary.csv": widget_summary(previous_release),
        "colour_summary.csv": colour_summary(previous_release),
    }
    for name, body in bodies.items():
        store.put(dr.draft_key(run_id, name), body)
    # an incomplete run failed before publishing its manifest
    if complete:
        manifest = {
            "run_id": run_id,
            "commit": commit,
            "previous_release": previous_release,
            "created": "2026-10-01T00:00:00+00:00",
            "files": list(bodies),
        }
        store.put(dr.draft_key(run_id, dr.MANIFEST), json.dumps(manifest))


@pytest.fixture
def store(tmp_path, monkeypatch):
    """a store holding one previous release, plus one run of COMMIT"""
    memory = MemoryStore()
    put_release_log(memory, PREVIOUS_RELEASE, "2026-02-10T15:34:35-08:00")
    put_run(memory)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dr.subprocess, "check_output", fake_git)
    monkeypatch.setattr(dr, "add_gpkg_layer", fake_add_gpkg_layer)
    monkeypatch.setattr(dr, "zip_files", fake_zip_files)
    return memory


def run_release(store, run_id=None, config=CONFIG):
    dr.release(config, store, run_id)


def fake_zip_files(out_zip, files):
    """plain zip in place of sozip, which isn't needed to check what's archived"""
    with zipfile.ZipFile(out_zip, "w") as z:
        for f in files:
            z.write(f, os.path.basename(f))


def layers(body):
    return [line.split("\t") for line in body.decode().splitlines()]


def archive_members(store, key):
    with zipfile.ZipFile(io.BytesIO(store.objects[key]["body"])) as z:
        return {name: z.read(name) for name in z.namelist()}


def test_release_publishes(store):
    run_release(store)

    archive_key = f"releases/{RELEASE_TAG}/widgets_{RELEASE_TAG}.zip"
    latest_keys = [
        "widgets.gpkg",
        "parts.csv",
        "widget_summary.csv",
        "LOG_widgets.csv",
        "LOG_colours.csv",
    ]
    release_log_keys = [
        f"releases/{RELEASE_TAG}/widgets.csv",
        f"releases/{RELEASE_TAG}/colours.csv",
    ]

    # the archive first, this release's change log rows last
    assert store.uploads == [archive_key] + latest_keys + release_log_keys

    tags = {"commit": COMMIT, "run_id": RUN_ID, "release": RELEASE_TAG}
    for key in store.uploads:
        assert store.objects[key]["tags"] == tags

    # the archive holds the bundle and the archived outputs, as is
    members = archive_members(store, archive_key)
    assert sorted(members) == sorted(
        ["widgets.gpkg", "parts.csv", "widget_summary.csv"]
    )
    assert members["parts.csv"] == b"part,widget_id\nbolt,1\n"

    # configured layers in the bundle - zipped files read via /vsizip/, csvs as tables
    bundle = layers(members["widgets.gpkg"])
    assert [layer[0] for layer in bundle] == ["widgets", "parts"]
    assert bundle[0][1].startswith("/vsizip/") and bundle[0][2] == "True"
    assert bundle[1][1].endswith("parts.csv") and bundle[1][2] == "False"

    # a latest file of another name is converted, one of the same name is copied as is
    widgets = layers(store.objects["widgets.gpkg"]["body"])
    assert [layer[0] for layer in widgets] == ["widgets"]
    assert store.objects["parts.csv"]["body"] == b"part,widget_id\nbolt,1\n"

    # drafts are left for prune_drafts
    assert dr.draft_key(RUN_ID, dr.MANIFEST) in store.objects


def test_release_without_bundle(store):
    config = copy.deepcopy(CONFIG)
    del config["bundle"]
    for output in config["outputs"]:
        output.pop("layer", None)

    run_release(store, config=config)
    members = archive_members(
        store, f"releases/{RELEASE_TAG}/widgets_{RELEASE_TAG}.zip"
    )
    assert sorted(members) == ["parts.csv", "widget_summary.csv"]


def test_release_without_archive(store):
    config = copy.deepcopy(CONFIG)
    for key in ("archive", "bundle"):
        del config[key]
    for output in config["outputs"]:
        output.pop("layer", None)
        output.pop("archive", None)

    run_release(store, config=config)
    # just the latest files and the release's change log rows
    assert store.uploads[0] == "widgets.gpkg"
    assert [k for k in store.uploads if k.startswith("releases/")] == [
        f"releases/{RELEASE_TAG}/widgets.csv",
        f"releases/{RELEASE_TAG}/colours.csv",
    ]


def test_release_logs(store):
    run_release(store)

    # this release's own rows - the widget dropped since the previous release isn't logged
    # (as zero, or at all)
    rows = store.read_csv(dr.release_log_key(RELEASE_TAG, "widgets"))
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row["widget_id"] == 1
    assert row["count"] == 110
    assert row["release_tag"] == RELEASE_TAG
    assert row["release_date"] == RELEASE_DATE
    assert row["commit"] == SHORT_COMMIT
    assert row["run_id"] == RUN_ID
    assert pandas.notna(row["released_at"])

    colours = store.read_csv(dr.release_log_key(RELEASE_TAG, "colours"))
    assert list(colours["count"]) == [6]

    # the combined logs, rebuilt from every release's rows
    for name in ("widgets", "colours"):
        combined = store.read_csv(dr.combined_log_key(name))
        assert list(combined["release_tag"]) == [PREVIOUS_RELEASE, RELEASE_TAG]


def test_read_release_logs_ordered_by_released_at(store, tmp_path):
    # tag names (and so the order keys are listed in) needn't follow release order - here
    # v2025-08 was released after v2026-02. Mixed timestamp formats, as in backfilled logs
    put_release_log(store, "v2025-08", "2026-08-20T09:00:00-07:00")
    put_release_log(store, "v2024-04", "2024-04-01")

    histories = dr.read_release_logs(CONFIG, store, str(tmp_path))
    for history in histories.values():
        assert list(history["release_tag"]) == [
            "v2024-04",
            PREVIOUS_RELEASE,
            "v2025-08",
        ]
    assert dr.latest_release(histories["widgets"]) == "v2025-08"


def test_read_release_logs_ignores_other_keys(store, tmp_path):
    # an archive, and logs in another layout, aren't change log rows
    store.put(f"releases/{RELEASE_TAG}/widgets_{RELEASE_TAG}.zip", "zip")
    # (real log rows, so they'd show up if read)
    rows = store.objects[dr.release_log_key(PREVIOUS_RELEASE, "widgets")]["body"]
    store.put(
        "releases/log/v2026-05/widgets.csv", rows.replace(b"v2026-02", b"v2026-05")
    )
    store.put("releases/widgets.csv", rows.replace(b"v2026-02", b"v2026-06"))

    histories = dr.read_release_logs(CONFIG, store, str(tmp_path))
    assert list(histories["widgets"]["release_tag"]) == [PREVIOUS_RELEASE]


def test_release_defaults_to_latest_run(store, tmp_path):
    later_run = "20261002T000000Z"
    put_run(store, run_id=later_run)
    # still later runs that don't qualify: one that never finished publishing, and one of
    # another commit
    put_run(store, run_id="20261003T000000Z", complete=False)
    put_run(store, run_id="20261004T000000Z", commit="f00")

    run_release(store)
    assert store.objects["LOG_widgets.csv"]["tags"]["run_id"] == later_run
    release_dir = tmp_path / f"release_{RELEASE_TAG}"
    assert (release_dir / "widgets.gpkg.zip").read_text() == f"widgets zip {later_run}"


def test_release_pinned_run(store):
    put_run(store, run_id="20261002T000000Z")

    run_release(store, run_id=RUN_ID)
    assert store.objects["LOG_widgets.csv"]["tags"]["run_id"] == RUN_ID


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
    del store.objects[dr.draft_key(RUN_ID, "colour_summary.csv")]

    with pytest.raises(ValueError, match="missing colour_summary.csv"):
        run_release(store)
    assert store.uploads == []


def test_release_already_released(store):
    put_release_log(store, RELEASE_TAG, "2026-10-02T00:00:00+00:00")

    with pytest.raises(ValueError, match="already been released"):
        run_release(store)
    assert store.uploads == []


def test_release_partially_logged(store):
    # a previous attempt failed between publishing its change log files
    put_release_log(store, RELEASE_TAG, "2026-10-02T00:00:00+00:00", names=["widgets"])

    run_release(store)
    for name in ("widgets", "colours"):
        combined = store.read_csv(dr.combined_log_key(name))
        assert list(combined["release_tag"]) == [PREVIOUS_RELEASE, RELEASE_TAG]


def test_release_stale_summaries(store):
    # another release since the run - its diffs are against an older release
    put_release_log(store, "v2026-05", "2026-05-01T00:00:00+00:00")

    with pytest.raises(ValueError, match="older release"):
        run_release(store)
    assert store.uploads == []


def test_release_ignores_leftover_local_files(store, tmp_path):
    # the project's own local output, and a bundle from a failed release attempt
    (tmp_path / "widgets.gpkg").write_text("project output")
    leftover = tmp_path / f"release_{RELEASE_TAG}"
    leftover.mkdir()
    (leftover / "bundle").mkdir()
    (leftover / "bundle" / "widgets.gpkg").write_text("widgets\tstale\tTrue\n")

    run_release(store)
    assert (tmp_path / "widgets.gpkg").read_text() == "project output"
    members = archive_members(
        store, f"releases/{RELEASE_TAG}/widgets_{RELEASE_TAG}.zip"
    )
    assert b"stale" not in members["widgets.gpkg"]


# --- publish_draft ---


@pytest.fixture
def run_files(tmp_path):
    """a run's local outputs and totals, as a project writes them"""
    for name in ("widgets.gpkg.zip", "parts.csv"):
        (tmp_path / name).write_text(name)
    pandas.DataFrame(
        {"widget_id": [1], "widget_name": ["sprocket"], "count": [120.0]}
    ).to_csv(tmp_path / "widgets_current.csv", index=False)
    pandas.DataFrame({"colour": ["red"], "count": [7.0]}).to_csv(
        tmp_path / "colours_current.csv", index=False
    )
    files = {name: str(tmp_path / name) for name in ("widgets.gpkg.zip", "parts.csv")}
    current = {
        "widgets": str(tmp_path / "widgets_current.csv"),
        "colours": str(tmp_path / "colours_current.csv"),
    }
    return files, current


def test_publish_draft(store, run_files, tmp_path):
    files, current = run_files
    run_id = dr.publish_draft(
        CONFIG, store, files, current, work_dir=str(tmp_path), run_id="20261006T000000Z"
    )
    assert run_id == "20261006T000000Z"

    # every draft file, then the manifest last, marking the run complete
    manifest_key = dr.draft_key(run_id, dr.MANIFEST)
    assert store.uploads == [
        dr.draft_key(run_id, name) for name in dr.draft_files(CONFIG)
    ] + [manifest_key]
    manifest = json.loads(store.objects[manifest_key]["body"])
    assert manifest["commit"] == COMMIT
    assert manifest["previous_release"] == PREVIOUS_RELEASE
    assert manifest["files"] == dr.draft_files(CONFIG)

    # summaries comparing the run's totals to the previous release
    summary = store.read_csv(dr.draft_key(run_id, "widget_summary.csv"))
    assert list(summary.columns) == [
        "widget_id",
        "widget_name",
        "current",
        PREVIOUS_RELEASE,
        "diff",
        "pct_diff",
    ]
    assert summary.iloc[0]["diff"] == 20
    colours = store.read_csv(dr.draft_key(run_id, "colour_summary.csv"))
    assert colours.iloc[0]["current"] == 7


def test_publish_draft_then_release(store, run_files, tmp_path):
    # what publish_draft writes is what release reads
    files, current = run_files
    run_id = dr.publish_draft(CONFIG, store, files, current, work_dir=str(tmp_path))

    run_release(store, run_id=run_id)
    rows = store.read_csv(dr.release_log_key(RELEASE_TAG, "widgets"))
    assert list(rows["count"]) == [120]


def test_publish_draft_wrong_files(store, run_files):
    files, current = run_files
    del files["parts.csv"]
    with pytest.raises(ValueError, match="configured outputs"):
        dr.publish_draft(CONFIG, store, files, current)
    assert store.uploads == []


@pytest.mark.parametrize(
    "files, current, match",
    [
        (["widgets.gpkg.zip"], ["widgets", "colours"], "configured outputs"),
        (
            ["widgets.gpkg.zip", "parts.csv", "extra.csv"],
            ["widgets", "colours"],
            "configured outputs",
        ),
        # summaries are written by publish_draft, not provided
        (
            ["widgets.gpkg.zip", "parts.csv", "widget_summary.csv"],
            ["widgets", "colours"],
            "configured outputs",
        ),
        (["widgets.gpkg.zip", "parts.csv"], ["widgets"], "configured logs"),
    ],
    ids=["missing file", "extra file", "summary provided", "missing log"],
)
def test_check_inputs_mismatch(files, current, match):
    with pytest.raises(ValueError, match=match):
        dr.check_inputs(CONFIG, files, current)


def test_check_inputs():
    dr.check_inputs(CONFIG, ["parts.csv", "widgets.gpkg.zip"], ["colours", "widgets"])


def test_publish_draft_no_release_yet(store, run_files):
    files, current = run_files
    for key in store.list("releases/"):
        del store.objects[key]
    with pytest.raises(ValueError, match="release at least one version"):
        dr.publish_draft(CONFIG, store, files, current)
    assert store.uploads == []


# --- prune_drafts ---


def run_prune(store, older_than=None, dry_run=False):
    dr.prune_drafts(CONFIG, store, older_than, dry_run)


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
    store.delete_prefix(dr.run_prefix(RUN_ID))
    recent = [
        (datetime.now(timezone.utc) - timedelta(days=d)).strftime(dr.RUN_ID_FORMAT)
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
    store.delete_prefix(dr.run_prefix(RUN_ID))

    run_prune(store, older_than=90)
    assert runs(store) == ["20200201T000000Z"]


def test_prune_keeps_partially_released_run(store):
    # a release that failed between publishing its change log files - its retry still
    # needs the draft
    put_release_log(
        store, "v2026-05", "2026-05-01T00:00:00+00:00", names=["widgets"], run_id=RUN_ID
    )

    run_prune(store)
    assert runs(store) == [RUN_ID]


def test_prune_dry_run(store):
    put_run(store, run_id=RELEASED_RUN)

    run_prune(store, older_than=0, dry_run=True)
    assert runs(store) == [RELEASED_RUN, RUN_ID]


def test_prune_ignores_other_keys(store):
    # files outside any run prefix, and a prefix that isn't a run's
    store.put("drafts/widgets.gpkg.zip", "old layout")
    store.put("drafts/notarun/file.csv", "?")
    # nor does a non-run prefix (which sorts after every run_id) count as the newest
    # unreleased run - the actual newest unreleased run is still kept
    put_run(store, run_id="20200101T000000Z")

    run_prune(store, older_than=0)
    assert "drafts/widgets.gpkg.zip" in store.objects
    assert "drafts/notarun/file.csv" in store.objects
    assert runs(store) == [RUN_ID, "notarun"]
