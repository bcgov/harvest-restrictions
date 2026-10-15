"""Draft, review and release the outputs of a data analysis, via object storage

A project runs its analysis, then calls publish_draft() with the files it produced. Each run
is published under its own drafts/<run_id>/ prefix, with summaries comparing the run's totals
to the most recent release, and a manifest written last to mark the run complete. Once a
run has been reviewed, its commit is tagged and release() promotes the run:

- a zip archive of the release's files (optionally including a bundle geopackage combining
  its layers) under releases/, with the release tag in its name, never overwritten
- the release's change log rows under logs/<release_tag>/, never rewritten
- fixed-name "latest" files at the root, for consumers that don't track releases
- combined change logs at the root, rebuilt from every release's rows

prune_drafts() deletes drafts that are no longer needed. Nothing is ever overwritten that a
later step depends on, so the bucket needs no versioning.

What a project publishes is described by a config file - see CONFIG_SCHEMA and load_config().
Requires the aws cli, sozip (bundled with GDAL) if an archive is configured, and ogr2ogr if a
bundle or converted "latest" files are configured.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

import jsonschema
import pandas

LOG = logging.getLogger(__name__)

MANIFEST = "manifest.json"
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"

CONFIG_SCHEMA = {
    "type": "object",
    "required": ["prefix", "outputs", "logs"],
    "additionalProperties": False,
    "properties": {
        "prefix": {
            "type": "string",
            "description": "prefix in the bucket that everything is published under",
        },
        "archive": {
            "type": "string",
            "description": "name of the zip archive of each release's files, published under "
            "releases/, with a {release_tag} placeholder",
        },
        "bundle": {
            "type": "string",
            "description": "name of a geopackage in the archive combining the outputs' layers",
        },
        "outputs": {
            "type": "array",
            "description": "files the project publishes with each run",
            "items": {
                "type": "object",
                "required": ["file"],
                "additionalProperties": False,
                "properties": {
                    "file": {"type": "string"},
                    "layer": {
                        "type": "string",
                        "description": "add to the bundle as this layer (non-spatial table for .csv)",
                    },
                    "archive": {
                        "type": "boolean",
                        "description": "add to the archive, as is",
                    },
                    "latest": {
                        "type": "string",
                        "description": "publish as this fixed-name latest file - as is if it's the "
                        "same name as file, otherwise converted to a .gpkg of that name",
                    },
                },
            },
        },
        "logs": {
            "type": "array",
            "description": "totals to compare each run against the previous release, and log per release",
            "items": {
                "type": "object",
                "required": ["name", "summary", "key", "columns", "value"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "summary": {
                        "type": "string",
                        "description": "name of the summary comparing a run to the previous release",
                    },
                    "key": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "value": {"type": "string"},
                    "decimals": {"type": "integer", "default": 0},
                },
            },
        },
    },
}


def load_config(path):
    """read and validate a release config file"""
    with open(path) as f:
        config = json.load(f)
    validate_config(config)
    return config


def validate_config(config):
    jsonschema.validate(config, CONFIG_SCHEMA)
    archive = config.get("archive")
    # the archive's name says which release it is, once it's downloaded
    if archive and "{release_tag}" not in archive:
        raise ValueError(f"archive {archive} has no {{release_tag}} placeholder")
    bundle = config.get("bundle")
    if bundle and not archive:
        raise ValueError("a bundle is configured, but no archive to hold it")
    archived = [o["file"] for o in config["outputs"] if o.get("archive")]
    if bundle in archived:
        raise ValueError(f"bundle {bundle} has the same name as an archived output")
    for output in config["outputs"]:
        if output.get("archive") and not archive:
            raise ValueError(
                f"{output['file']}: is to be archived, but no archive is configured"
            )
        latest = output.get("latest")
        if latest and latest != output["file"] and not latest.endswith(".gpkg"):
            raise ValueError(
                f"{output['file']}: a latest file converted from it must be a .gpkg, not {latest}"
            )
        if output.get("layer") and not config.get("bundle"):
            raise ValueError(
                f"{output['file']}: has a bundle layer, but no bundle is configured"
            )
    for log in config["logs"]:
        if log["key"] not in log["columns"]:
            raise ValueError(f"{log['name']}: key {log['key']} is not in columns")


def draft_files(config):
    """names of every file a run publishes under drafts/<run_id>/, besides the manifest"""
    names = [o["file"] for o in config["outputs"]]
    names += [log["summary"] for log in config["logs"] if log["summary"] not in names]
    return names


def check_inputs(config, files, current):
    """check a project provides exactly what publish_draft() needs for config

    files are the names of the output files the project produces - every configured output
    other than the summaries, which publish_draft() writes. current are the names of the logs
    the project produces totals for - every configured log. Call this before a long-running
    analysis, to fail fast rather than once it's done.
    """
    summaries = {log["summary"] for log in config["logs"]}
    expected = set(draft_files(config)) - summaries
    if set(files) != expected:
        raise ValueError(
            f"files must be exactly the configured outputs {sorted(expected)}, "
            f"got {sorted(files)}"
        )
    log_names = {log["name"] for log in config["logs"]}
    if set(current) != log_names:
        raise ValueError(
            f"current must be totals for exactly the configured logs {sorted(log_names)}, "
            f"got {sorted(current)}"
        )


# --- object storage ---


def run_prefix(run_id):
    """the drafts/ prefix holding everything one run published

    Each run gets its own prefix and never writes to it again, so runs can't overwrite each
    other, and pruning a run is just deleting its prefix. run_id is a UTC timestamp, so run
    prefixes sort oldest to newest.
    """
    return f"drafts/{run_id}"


def draft_key(run_id, name):
    return f"{run_prefix(run_id)}/{name}"


def log_prefix(release_tag):
    """the logs/ prefix holding one release's change log rows - written once, never rewritten"""
    return f"logs/{release_tag}"


def release_log_key(release_tag, log_name):
    """key of one release's change log rows"""
    return f"{log_prefix(release_tag)}/{log_name}.csv"


def combined_log_key(log_name):
    """key of a combined change log, rebuilt from every release's rows on each release"""
    return f"LOG_{log_name}.csv"


class S3Store:
    """the objects under a prefix in an s3 bucket, via the aws cli

    Keys passed to and returned by every method are relative to the prefix.
    """

    def __init__(self, bucket, prefix):
        self.bucket = bucket
        self.prefix = prefix

    def url(self, key):
        return f"s3://{self.bucket}/{self.prefix}/{key}"

    def upload(self, local_file, key, tags=None):
        subprocess.run(["aws", "s3", "cp", local_file, self.url(key)], check=True)
        if tags:
            tagging = {"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]}
            subprocess.run(
                [
                    "aws",
                    "s3api",
                    "put-object-tagging",
                    "--bucket",
                    self.bucket,
                    "--key",
                    f"{self.prefix}/{key}",
                    "--tagging",
                    json.dumps(tagging),
                ],
                check=True,
            )

    def download(self, key, local_file):
        """download to local_file, creating its directory if needed"""
        os.makedirs(os.path.dirname(local_file) or ".", exist_ok=True)
        subprocess.run(["aws", "s3", "cp", self.url(key), local_file], check=True)

    def list(self, prefix):
        """sorted keys under prefix"""
        out = subprocess.run(
            [
                "aws",
                "s3api",
                "list-objects-v2",
                "--bucket",
                self.bucket,
                "--prefix",
                f"{self.prefix}/{prefix}",
                "--output",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        # the cli prints nothing at all when no objects match
        contents = json.loads(out).get("Contents", []) if out.strip() else []
        return sorted(c["Key"][len(self.prefix) + 1 :] for c in contents)

    def delete_prefix(self, prefix):
        """delete every object under prefix/"""
        subprocess.run(
            ["aws", "s3", "rm", "--recursive", self.url(prefix) + "/"], check=True
        )


# --- geopackages ---


def add_gpkg_layer(out_file, source_file, layer_name, spatial=True):
    """add source_file as a new layer (or non-spatial table) in out_file, creating it if needed"""
    cmd = ["ogr2ogr", "-f", "GPKG"]
    if os.path.exists(out_file):
        cmd.append("-update")
    cmd += [out_file, source_file, "-nln", layer_name]
    if not spatial:
        cmd += ["-nlt", "NONE", "-oo", "AUTODETECT_TYPE=YES"]
    subprocess.run(cmd, check=True)


def zip_files(out_zip, files):
    """zip files into out_zip, each under its basename

    Uses sozip (bundled with GDAL) rather than plain zip, so the result is seek-optimized -
    GDAL's /vsizip/ reader can read a layer from it, even remotely, fetching just the bytes it
    needs instead of decompressing the whole archive.
    """
    subprocess.run(
        ["sozip", "--overwrite", "--junk-paths", out_zip, *files], check=True
    )


def ogr_path(local_file):
    """path GDAL reads local_file from - zipped files via /vsizip/, which opens a zip holding
    a single file without naming it, so the file inside needn't match the zip's name"""
    if local_file.endswith(".zip"):
        return f"/vsizip/{local_file}"
    return local_file


# --- change logs ---


def read_release_logs(config, store, work_dir):
    """read every release's change log rows (logs/<release_tag>/<log name>.csv),
    downloading them to work_dir

    returns {log name: df} with each df the rows of all releases combined and ordered by
    released_at, or None if no release has written that log yet
    """
    names = {f"{log['name']}.csv": log["name"] for log in config["logs"]}
    frames = {name: [] for name in names.values()}
    for key in store.list("logs/"):
        parts = key.split("/")
        name = names.get(parts[-1]) if len(parts) == 3 else None
        if name:
            local_file = os.path.join(work_dir, key)
            store.download(key, local_file)
            frames[name].append(pandas.read_csv(local_file))

    histories = {}
    for name, release_frames in frames.items():
        if not release_frames:
            histories[name] = None
            continue
        history = pandas.concat(release_frames, ignore_index=True)
        # released_at orders releases - tag names and commit dates needn't follow release order
        released_at = pandas.to_datetime(
            history["released_at"], utc=True, format="ISO8601"
        )
        histories[name] = history.iloc[released_at.argsort(kind="stable")].reset_index(
            drop=True
        )
    return histories


def latest_release(history):
    """tag of the most recent release in a history from read_release_logs(), or None"""
    if history is None:
        return None
    return history["release_tag"].iloc[-1]


def build_change(
    current_file, history, previous_release, key, columns, value="area_ha", decimals=0
):
    """merge this run's totals with the previous release's, keyed on the given column

    Retains every category present in either side (outer join) rather than just those in
    the previous release. Descriptive columns are filled from whichever side has them, so a
    category new to this run (or one dropped since the previous release) still gets its
    labels. Totals/diff/pct_diff are left as genuine NaN (not 0) for a category missing from
    one side, since 0 would misleadingly imply the category existed with a zero total.
    """
    descriptive = [c for c in columns if c != key]

    previous = history[history["release_tag"] == previous_release][
        columns + [value]
    ].rename(columns={value: previous_release})
    current = pandas.read_csv(current_file)[columns + [value]].rename(
        columns={value: "current"}
    )

    merged = previous.merge(current, how="outer", on=key, suffixes=("_previous", ""))
    for col in descriptive:
        previous_col = merged.pop(f"{col}_previous")
        merged[col] = merged[col].combine_first(previous_col)

    merged["diff"] = merged["current"] - merged[previous_release]
    merged["pct_diff"] = (merged["diff"] / merged[previous_release]) * 100

    # key, descriptive columns, current, previous, diff, pct_diff - explicit rather than
    # relying on whatever order the merge happens to produce
    column_order = [key, *descriptive, "current", previous_release, "diff", "pct_diff"]
    return (
        merged[column_order]
        .round(
            {
                previous_release: decimals,
                "current": decimals,
                "diff": decimals,
                "pct_diff": 2,
            }
        )
        .set_index(key)
    )


# --- git ---


def git(*args):
    return subprocess.check_output(["git", *args]).decode("ascii").strip()


# --- draft ---


def publish_draft(config, store, files, current, work_dir=".", run_id=None):
    """publish a run's outputs under its own drafts/<run_id>/ prefix

    files maps every configured output file (other than the summaries, which this writes) to
    the local path of that file. current maps each configured log's name to a local csv of
    this run's totals (the log's columns, plus its value column). Each log's summary, comparing
    those totals to the most recent release, is written to work_dir and published with the
    rest.

    The manifest is published last - its presence marks the run as complete, so release()
    never picks up a run that failed partway through publishing.

    returns the run_id
    """
    check_inputs(config, files, current)

    commit = git("rev-parse", "HEAD")
    run_id = run_id or datetime.now(timezone.utc).strftime(RUN_ID_FORMAT)

    # compare this run's totals to the most recent release
    with tempfile.TemporaryDirectory() as logs_dir:
        histories = read_release_logs(config, store, logs_dir)
    previous_release = None
    local_files = dict(files)
    for log in config["logs"]:
        history = histories[log["name"]]
        if history is None:
            raise ValueError(
                f"No {log['name']} change log found under {store.url('logs/')} "
                "- release at least one version before comparing to it"
            )
        previous_release = latest_release(history)
        summary_file = os.path.join(work_dir, log["summary"])
        build_change(
            current[log["name"]],
            history,
            previous_release,
            log["key"],
            log["columns"],
            log["value"],
            log.get("decimals", 0),
        ).to_csv(summary_file)
        LOG.info(f"{summary_file} written, comparing to {previous_release}")
        local_files[log["summary"]] = summary_file

    for name in draft_files(config):
        store.upload(local_files[name], draft_key(run_id, name))
        LOG.info(
            f"{local_files[name]} published to {store.url(draft_key(run_id, name))}"
        )

    manifest = {
        "run_id": run_id,
        "commit": commit,
        # the release this run's summaries are compared against
        "previous_release": previous_release,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": draft_files(config),
    }
    manifest_file = os.path.join(work_dir, MANIFEST)
    with open(manifest_file, "w") as f:
        json.dump(manifest, f, indent=2)
    store.upload(manifest_file, draft_key(run_id, MANIFEST))
    LOG.info(f"Run {run_id} published to {store.url(run_prefix(run_id))}/")
    return run_id


# --- release ---


def find_run(store, commit, run_id, work_dir):
    """return the manifest of the run of commit to release

    run_id pins a specific run; otherwise the most recent complete run of commit is used.
    Only runs with a manifest (i.e. that finished publishing) are considered.
    """
    manifest_keys = [
        k for k in store.list("drafts/") if os.path.basename(k) == MANIFEST
    ]
    if run_id:
        candidates = [k for k in manifest_keys if k == draft_key(run_id, MANIFEST)]
    else:
        # run_ids are UTC timestamps, so this is newest first
        candidates = sorted(manifest_keys, reverse=True)

    for key in candidates:
        local_file = os.path.join(work_dir, key)
        store.download(key, local_file)
        with open(local_file) as f:
            manifest = json.load(f)
        if manifest["commit"] == commit:
            return manifest
        if run_id:
            raise ValueError(
                f"Run {run_id} is of commit {manifest['commit']}, not {commit} - "
                "check out that commit to release it"
            )

    run_desc = f"run {run_id}" if run_id else "run"
    raise ValueError(
        f"No complete {run_desc} of commit={commit} found under {store.url('drafts/')} "
        "- run the analysis against this commit before releasing it"
    )


def release(config, store, run_id=None):
    """release a run of the checked-out commit, which must be tagged with the release tag

    Defaults to the commit's most recent complete run; run_id pins a specific run. Refuses to
    release (before publishing anything) a tag that's already released, a run whose summaries
    compare against an older release than the latest, or a run missing any of its files.

    Everything is downloaded and built in a working directory, release_<release_tag>/, before
    anything is published. The release's change log rows are published last: they mark the
    release as done, so a failure partway through publishing can be retried, and the retry
    overwrites whatever had been published so far.

    The released run's drafts/<run_id>/ prefix is left in place - prune_drafts() deletes it.
    """
    commit = git("rev-parse", "HEAD")
    short_commit = git("rev-parse", "--short", "HEAD")
    release_tag = git("describe", "--tags", "--exact-match")
    release_date = git("log", "-1", "--format=%aI", commit)
    log_names = [log["name"] for log in config["logs"]]

    # cleared at the start of every run - so leftovers from a failed attempt (or the project's
    # own local outputs, which may share names) never get appended to
    work_dir = f"release_{release_tag}"
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir)

    def local(name):
        return os.path.join(work_dir, name)

    # refuse to release a tag that's already logged. Each log's rows are published as a
    # separate file - if only some made it (a failure between the uploads), the release didn't
    # finish, so its partial rows are dropped here and the retry publishes them again
    histories = read_release_logs(config, store, work_dir)
    logged_in = [
        name
        for name in log_names
        if histories[name] is not None
        and release_tag in histories[name]["release_tag"].values
    ]
    if log_names and len(logged_in) == len(log_names):
        raise ValueError(
            f"Release {release_tag} is already in {store.url(log_prefix(release_tag))}/ "
            "- it has already been released"
        )
    for name in logged_in:
        LOG.warning(
            f"Release {release_tag} is only partially logged (in {name}) - re-publishing it"
        )
        history = histories[name]
        histories[name] = history[history["release_tag"] != release_tag]

    # resolve which run of this commit to release - once resolved it's pinned for everything
    # below, so every file in the release comes from the same run
    manifest = find_run(store, commit, run_id, work_dir)
    run_id = manifest["run_id"]
    LOG.info(f"Releasing {release_tag}: commit={commit}, run_id={run_id}")

    # the run's summaries compare against whichever release was latest when it ran. If
    # another release has happened since, their diffs are against an older release
    previous_release = latest_release(histories[log_names[0]]) if log_names else None
    if manifest["previous_release"] != previous_release:
        raise ValueError(
            f"Summaries for run_id={run_id} compare against {manifest['previous_release']}, "
            f"but the latest release is now {previous_release} - their diffs are against an "
            f"older release. Re-run the analysis to compare against {previous_release}, "
            "then release that run"
        )

    published = store.list(run_prefix(run_id) + "/")
    missing = [f for f in draft_files(config) if draft_key(run_id, f) not in published]
    if missing:
        raise ValueError(
            f"Run {run_id} is missing {', '.join(missing)} under "
            f"{store.url(run_prefix(run_id))}/ - re-run the analysis"
        )

    tags = {"commit": commit, "run_id": run_id, "release": release_tag}

    # fetch the run's files, building the archive (and the bundle in it) and any converted
    # latest files from them. The bundle is built in a directory of its own - it may share a
    # name with a latest file (e.g. a latest geopackage of one of its layers)
    archive = (
        config["archive"].format(release_tag=release_tag)
        if config.get("archive")
        else None
    )
    bundle = config.get("bundle")
    bundle_file = os.path.join(work_dir, "bundle", bundle) if bundle else None
    if bundle:
        os.makedirs(os.path.dirname(bundle_file))
    archive_files = [bundle_file] if bundle else []
    latest_files = []
    for name in draft_files(config):
        store.download(draft_key(run_id, name), local(name))
    for output in config["outputs"]:
        name = output["file"]
        if output.get("archive"):
            archive_files.append(local(name))
        if output.get("layer"):
            add_gpkg_layer(
                bundle_file,
                ogr_path(local(name)),
                output["layer"],
                spatial=not name.endswith(".csv"),
            )
            LOG.info(f"{output['layer']} added to {bundle}, from {name}")
        latest = output.get("latest")
        if latest:
            if latest != name:
                layer = output.get("layer") or os.path.splitext(latest)[0]
                add_gpkg_layer(local(latest), ogr_path(local(name)), layer)
            latest_files.append(latest)
    if archive:
        zip_files(local(archive), archive_files)
        LOG.info(
            f"{archive} built, holding "
            f"{', '.join(os.path.basename(f) for f in archive_files)}"
        )

    # this release's change log rows, from each summary's "current" column - a category with no
    # current value existed in the previous release but not this run, and is excluded rather
    # than logged as zero. released_at (when this release was made) orders the releases when
    # the logs are combined
    released_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    release_logs = []
    for log in config["logs"]:
        rows = (
            pandas.read_csv(local(log["summary"]))[log["columns"] + ["current"]]
            .rename(columns={"current": log["value"]})
            .dropna(subset=[log["value"]])
        )
        rows["release_tag"] = release_tag
        rows["release_date"] = release_date
        rows["commit"] = short_commit
        rows["run_id"] = run_id
        rows["released_at"] = released_at

        release_log = release_log_key(release_tag, log["name"])
        os.makedirs(os.path.dirname(local(release_log)), exist_ok=True)
        rows.to_csv(local(release_log), index=False)
        release_logs.append(release_log)

        # the combined log, rebuilt from every release's rows (fetched above) plus this one's
        history = histories[log["name"]]
        if history is not None:
            rows = pandas.concat([history, rows], ignore_index=True)
        combined = combined_log_key(log["name"])
        rows.to_csv(local(combined), index=False)
        latest_files.append(combined)

    # everything is built - publish. First the archive, never overwritten once released, then
    # the latest files (including the combined change logs), and finally this release's change
    # log rows, marking the release as done
    to_publish = [(archive, f"releases/{archive}")] if archive else []
    to_publish += [(f, f) for f in latest_files]
    to_publish += [(f, f) for f in release_logs]
    for name, key in to_publish:
        store.upload(local(name), key, tags)
        LOG.info(f"{name} published to {store.url(key)}")
    LOG.info(f"Released {release_tag}")


# --- prune ---


def prune_drafts(config, store, older_than=None, dry_run=False):
    """delete runs' drafts/<run_id>/ prefixes that are no longer needed

    By default only released runs are deleted - everything they published was copied into
    the release. With older_than (days), unreleased runs older than that are deleted too,
    apart from the newest unreleased run, which may still be under review.
    """
    # a run is released once its rows are in every change log - a run in only some is a
    # release that failed partway through publishing, whose retry still needs the draft
    with tempfile.TemporaryDirectory() as logs_dir:
        histories = read_release_logs(config, store, logs_dir)
    released = None
    for history in histories.values():
        run_ids = (
            set() if history is None else set(history["run_id"].dropna().astype(str))
        )
        released = run_ids if released is None else released & run_ids
    released = released or set()

    # runs are drafts/<run_id>/<file>, with run_id a UTC timestamp - anything else under drafts/
    # isn't a run's, and is left alone
    run_times = {}
    for key in store.list("drafts/"):
        parts = key.split("/")
        if len(parts) != 3 or parts[1] in run_times:
            continue
        try:
            run_times[parts[1]] = datetime.strptime(parts[1], RUN_ID_FORMAT).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
    run_ids = sorted(run_times)
    unreleased = [r for r in run_ids if r not in released]
    now = datetime.now(timezone.utc)

    for run_id in run_ids:
        if run_id in released:
            reason = "released"
        elif (
            older_than is not None
            and run_id != unreleased[-1]
            and (now - run_times[run_id]).days > older_than
        ):
            reason = f"unreleased, older than {older_than} days"
        else:
            continue

        prefix = run_prefix(run_id)
        if dry_run:
            LOG.info(f"Would delete {store.url(prefix)}/ ({reason})")
        else:
            store.delete_prefix(prefix)
            LOG.info(f"Deleted {store.url(prefix)}/ ({reason})")
