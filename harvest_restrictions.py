import csv
import fnmatch
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

import bcdata
import click
import geopandas
import jsonschema
import pandas
from pyproj import CRS
from shapely.geometry.linestring import LineString
from shapely.geometry.multilinestring import MultiLineString
from shapely.geometry.multipoint import MultiPoint
from shapely.geometry.multipolygon import MultiPolygon
from shapely.geometry.point import Point
from shapely.geometry.polygon import Polygon
from slugify import slugify
from sqlalchemy import create_engine, inspect, text

LOG_FORMAT = "%(asctime)s:%(levelname)s:%(name)s: %(message)s"
LOG = logging.getLogger(__name__)

# everything published is under this prefix in the bucket
PREFIX = "harvest_restrictions"

# $BUCKET is not required for download/cache/overlay
# But if $BUCKET set, cache source data to s3
CACHE_PATH = (
    f"s3://{os.environ['BUCKET']}/{PREFIX}/cache" if os.environ.get("BUCKET") else None
)

# the change logs, by log name - each log is the area_ha of each category, per release.
# overlay writes each log's totals to <log>.csv with sql/<log>.sql, publish-draft compares them
# to the previous release's in <log>_summary.csv, and release appends them to logs/<log>.csv.
# The columns describe a category - the first is its key, matching the totals across
# releases, the rest are labels
LOGS = {
    "land_designations": [
        "land_designation_type_rank",
        "harvest_restriction_class_rank",
        "harvest_restriction_class_name",
        "land_designation_type_code",
        "land_designation_type_name",
    ],
    "harvest_restrictions": [
        "harvest_restriction_class_rank",
        "harvest_restriction_class_name",
    ],
}

# each published draft's and release's archive, holding these files - which are also each
# release's latest/ files. overlay_run.json records the commit the draft was run on
ARCHIVE = "harvest_restrictions_{tag}.zip"
ARCHIVED_FILES = [
    "harvest_restrictions.gpkg",
    "harvest_restrictions_sources.gpkg",
    "sources.csv",
    "overlay_run.json",
] + [f"{log}_summary.csv" for log in LOGS]

# a version, and its release's git tag: vYYYY-MM, or vYYYY-MM-DD for a hotfix - either sorts
# by date
VERSION = re.compile(r"v\d{4}-\d{2}(-\d{2})?")

# a published draft's name: <version>-draft<n>, numbered from 1 by publish-draft
DRAFT = re.compile(rf"(?P<version>{VERSION.pattern})-draft(?P<n>[1-9][0-9]*)")

verbose_opt = click.option("--verbose", "-v", count=True, help="Increase verbosity.")
quiet_opt = click.option("--quiet", "-q", count=True, help="Decrease verbosity.")
bucket_opt = click.option(
    "--bucket",
    "-b",
    envvar="BUCKET",
    required=True,
    help="Object storage bucket to publish to, defaults to $BUCKET environment variable if set",
)


def configure_logging(verbosity):
    log_level = max(10, 30 - 10 * verbosity)
    logging.basicConfig(stream=sys.stderr, level=log_level, format=LOG_FORMAT)


def run(cmd):
    """run a shell command, logging it first"""
    LOG.info(cmd)
    subprocess.run(cmd, shell=True, check=True)


def zip_files(out_zip, files):
    """zip files into out_zip, each under its basename

    Uses sozip (bundled with GDAL) rather than plain zip, so the result is seek-optimized -
    GDAL's /vsizip/ reader can read a layer from it, even remotely, fetching just the bytes it
    needs instead of decompressing the whole archive.
    """
    subprocess.run(
        ["sozip", "--overwrite", "--junk-paths", out_zip, *files], check=True
    )


def parse_sources(sources):
    """validate and parse sources data structure"""
    # validate sources against schema doc
    with open("source.schema.json", "r") as f:
        schema = json.load(f)
    jsonschema.validate(instance=sources, schema=schema)

    # sources are presumed to be ordered by importance/hierarchy,
    # sources occuring earlier in the list override sources lower in the list.
    # add an index (base 1) indicating the hierchy level of a given source
    sources = [dict(d, index=index + 1) for (index, d) in enumerate(sources)]

    parsed = sources
    for i, source in enumerate(sources):
        # replace string {CURRENT_DATE} with todays date
        if source["query"] and "{CURRENT_DATE}" in source["query"]:
            parsed[i]["query"] = parsed[i]["query"].replace(
                "{CURRENT_DATE}", datetime.today().strftime("%Y-%m-%d")
            )
        # slugify the alias
        alias = source["alias"]
        slug = slugify(alias, separator="_", lowercase=True)
        if slug != alias:
            parsed[i]["alias"] = slug
            LOG.warning(
                "{alias} - alias adjusted to {slug}, consider editing alias in config file"
            )

    LOG.info("Source json is valid")
    return parsed


def write_sources_csv(sources_file, out_file):
    """flatten sources_file to a csv, for publishing alongside release outputs"""
    with open(sources_file, "r") as f:
        sources = json.load(f)
    with open(out_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "index",
                "harvest_restriction",
                "alias",
                "description",
                "source",
                "source_type",
                "layer",
                "query",
                "name_field",
            ]
        )
        for index, source in enumerate(sources, start=1):
            writer.writerow(
                [
                    index,
                    source["data"].get("harvest_restriction"),
                    source["alias"],
                    source["description"],
                    source["source"],
                    source["source_type"],
                    source.get("layer"),
                    source["query"],
                    source["field_mapper"].get("name"),
                ]
            )


def validate_file(source):
    """simple validation of file based sources
    - file exists
    - schema is as expected
    """
    # load file
    df = geopandas.read_file(
        os.path.expandvars(source["source"]),
        layer=source["layer"],
        where=source["query"],
    )

    # are expected columns present?
    columns = [x.lower() for x in df.columns]
    # is primary key present and not null?
    if "primary_key" in source and source["primary_key"]:
        if source["primary_key"].lower() not in columns:
            raise ValueError(
                f"Validation error: {source['alias']} - primary key is not present - {source['primary_key']}"
            )
    for column in source["field_mapper"].values():
        if column and column.lower() not in columns:
            raise ValueError(
                f"Validation error: {source['alias']} - column {column} is not present, modify config 'field_mapper'"
            )

    # is there data?
    count = len(df.index)
    if count == 0:
        raise ValueError(
            f"Validation error: {source['alias']} - no data returned, check source and query"
        )

    # presume layer is defined correctly if no errors are raised
    LOG.info(f"Validation successful: {source['alias']} - record count: {str(count)}")


def validate_bcgw(source):
    """validate bcdata sources against bcdc api and wfs"""
    # does source exist as written?
    table = source["source"].upper()
    if table not in bcdata.list_tables():
        raise ValueError(
            f"Validation error: {source['alias']} - {table} does not exist in BCGW or is not available via WFS"
        )

    # get columns present in source from data catalogue
    table_def = bcdata.get_table_definition(table)
    columns = [c["column_name"] for c in table_def["schema"]]

    # is primary key present and not null?
    if "primary_key" in source and source["primary_key"]:
        if source["primary_key"] not in columns:
            raise ValueError(
                f"Validation error: {source['alias']} - Primary key - {source['primary_key']} is not present in {table}"
            )

    # required columns in field mapping present?
    for column in source["field_mapper"].values():
        if column:  # allow null source columns (adds the new column, but with no values from source)
            if column.upper() not in columns:
                raise ValueError(
                    f"Validation error: {source['alias']} - column {column} is not present in {table}, modify config 'field_mapper'"
                )

    # is there data?
    count = bcdata.get_count(table, query=source["query"])
    if count == 0:
        raise ValueError(
            f"Validation error: {source['alias']} - no data returned, check query against {table}"
        )

    # presume source is defined correctly if no errors are raised
    LOG.info(f"Validation successful: {source['alias']} - record count: {str(count)}")


def to_multipart(df):
    """
    geopandas has no built-in func for dumping singlepart to multipart
    https://gis.stackexchange.com/questions/311320/casting-geometry-to-multi-using-geopandas
    """
    df["geom"] = [
        MultiPoint([feature]) if isinstance(feature, Point) else feature
        for feature in df["geom"]
    ]
    df["geom"] = [
        MultiLineString([feature]) if isinstance(feature, LineString) else feature
        for feature in df["geom"]
    ]
    df["geom"] = [
        MultiPolygon([feature]) if isinstance(feature, Polygon) else feature
        for feature in df["geom"]
    ]
    return df


def validate_sources(sources, validate_data=True, alias=None):
    """
    Validate json, whether data sources exist, and assign hierarchy index
    based on position in list
    """
    for source in sources:
        if source["source_type"] == "BCGW":
            validate_bcgw(source)
        elif source["source_type"] == "FILE":
            validate_file(source)

    LOG.info("Validation successful: all layers appear valid")

    # return validated (and indexed/dated) sources as ordered dictionary
    return sources


def download_source(source):
    """download data from source to a standardized geodataframe"""

    # download WFS
    if source["source_type"] == "BCGW":
        df = bcdata.get_data(
            source["source"],
            query=source["query"],
            as_gdf=True,
            lowercase=True,
        )
        # if primary key is not provided in config, default to the pk noted in bcdata
        if ("primary_key" not in source.keys() or not source["primary_key"]) and source[
            "source"
        ].lower() in bcdata.primary_keys:
            source["primary_key"] = bcdata.primary_keys[source["source"].lower()]
        else:
            source["primary_key"] = None

    # download file
    elif source["source_type"] == "FILE":
        df = geopandas.read_file(
            os.path.expandvars(source["source"]),
            layer=source["layer"],
            where=source["query"],
        )
        if not df.crs:
            raise ValueError(
                "Source does not have a defined projection/coordinate reference system"
            )
        # reproject to BC Albers if necessary
        if df.crs != CRS.from_user_input(3005):
            df = df.to_crs("EPSG:3005")
        # lowercasify column names
        df.columns = [x.lower() for x in df.columns]

    # standardize/tidy the data
    df = df.rename_geometry("geom")
    df = to_multipart(df)  # sources can have mixed types, just make everything multi

    # standardize columns, adding data as required
    df["__index__"] = source["index"]
    df["__description__"] = source["description"]
    df["__alias__"] = source["alias"].lower()
    df["__primary_key__"] = ""
    if "primary_key" in source and source["primary_key"]:
        df["__primary_key__"] = df[source["primary_key"].lower()].astype(
            "str"
        )  # handle pks as strings

    # rename columns that we want to retain
    for key, value in source["field_mapper"].items():
        if value:
            df["__" + key + "__"] = df[
                value.lower()
            ]  # all incoming data is already lowercasified
        else:
            df["__" + key + "__"] = None

    # add additional constant data
    if source["data"]:
        for key, value in source["data"].items():
            df["__" + key + "__"] = value

    # retain only columns that have just been added
    columns = (
        ["index", "description", "alias", "primary_key"]
        + list(source["field_mapper"])
        + list(source["data"])
    )
    df = df[["__" + c + "__" for c in columns] + ["geom"]]

    # strip the __ prefix/suffix
    df = df.rename(columns={"__" + c + "__": c for c in columns})

    return df


# --- drafts and releases ---
#
# Each version is published first as one or more drafts, for review, then released:
#
# 1. overlay, run on a commit, writes its outputs to the local draft, drafts/
# 2. publish-draft <version> (eg v2026-10) publishes the local draft as the version's next
#    draft, drafts/harvest_restrictions_<version>-draft<n>.zip, with summaries comparing its
#    area totals to the latest release - for review
# 3. once a draft is approved, release <draft> (eg v2026-10-draft2) - run by the release
#    workflow - tags the draft's commit <version>, pushes the tag, and publishes:
#    - fixed-name latest files under latest/, overwritten by each release
#    - the release's area totals, appended to the change logs, logs/<log>.csv
#    - releases/harvest_restrictions_<version>.zip, a copy of the draft's archive -
#      published last, marking the version released
#    then deletes the version's drafts
#
# A release is exactly the draft that was reviewed - nothing is re-run or rebuilt.


def draft_key(draft):
    """a published draft's archive - written once, never overwritten. Under drafts/, the
    local draft folder's name"""
    return f"drafts/{ARCHIVE.format(tag=draft)}"


def published_drafts(store, version):
    """the names of version's published drafts, <version>-draft<n>, from their archives
    (drafts/harvest_restrictions_<version>-draft<n>.zip)"""
    archive = re.compile(rf"drafts/harvest_restrictions_(?P<name>{DRAFT.pattern})\.zip")
    return [
        m["name"]
        for key in store.list(f"drafts/harvest_restrictions_{version}-draft")
        if (m := archive.fullmatch(key))
    ]


def release_key(release_tag):
    """a release's archive - written once, never overwritten"""
    return f"releases/{ARCHIVE.format(tag=release_tag)}"


class S3Store:
    """the objects under PREFIX in an s3 bucket, via the aws cli

    Keys passed to and returned by every method are relative to PREFIX.
    """

    def __init__(self, bucket):
        self.bucket = bucket

    def url(self, key):
        return f"s3://{self.bucket}/{PREFIX}/{key}"

    def upload(self, local_file, key):
        subprocess.run(["aws", "s3", "cp", local_file, self.url(key)], check=True)

    def copy(self, key, new_key):
        """copy within the bucket - nothing is downloaded"""
        subprocess.run(
            ["aws", "s3", "cp", self.url(key), self.url(new_key)], check=True
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
                f"{PREFIX}/{prefix}",
                "--output",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        # the cli prints nothing at all when no objects match
        contents = json.loads(out).get("Contents", []) if out.strip() else []
        return sorted(c["Key"][len(PREFIX) + 1 :] for c in contents)

    def delete(self, key):
        subprocess.run(["aws", "s3", "rm", self.url(key)], check=True)


def git(*args):
    return subprocess.check_output(["git", *args]).decode("ascii").strip()


def uncommitted_changes():
    """git status lines of the uncommitted changes to tracked files, other than to docs

    Untracked files are ignored: the analysis can only use one if a tracked file refers to
    it, and that change would itself be uncommitted.
    """
    # files whose changes can't affect the outputs - every other tracked file must be
    # committed for a run to be the commit's output
    docs = ["*.md", "CHANGES.txt", "LICENSE"]
    # -z: entries NUL-separated, paths unquoted - a rename or copy is followed by its source
    entries = iter(
        subprocess.check_output(
            ["git", "status", "--porcelain", "-z", "--untracked-files=no"]
        )
        .decode()
        .split("\0")
    )
    changes = []
    for entry in entries:
        if not entry:
            continue
        status, paths = entry[:2], [entry[3:]]
        if "R" in status or "C" in status:
            paths.append(next(entries))
        if not all(any(fnmatch.fnmatch(p, doc) for doc in docs) for p in paths):
            changes.append(f"{status} {' <- '.join(paths)}")
    return changes


def checkout_state():
    """(HEAD, uncommitted changes other than to docs) of the checkout"""
    return git("rev-parse", "HEAD"), uncommitted_changes()


def write_overlay_run(draft_dir, commit, changes):
    """record what the draft in draft_dir was run on - written last, marking it finished"""
    draft = {
        "commit": commit,
        "uncommitted_changes": changes,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(os.path.join(draft_dir, "overlay_run.json"), "w") as f:
        json.dump(draft, f, indent=2)


def check_draft(draft_dir, commit):
    """refuse unless draft_dir holds a finished draft of commit, run without uncommitted
    changes - from its overlay_run.json (the draft is removed when an overlay starts, and
    its record written when it finishes)"""
    path = os.path.join(draft_dir, "overlay_run.json")
    if not os.path.exists(path):
        raise ValueError("No finished overlay draft - run overlay first")
    with open(path) as f:
        draft = json.load(f)
    if draft["commit"] != commit:
        raise ValueError(
            f"The draft is of commit {draft['commit']}, not {commit} - run overlay on this "
            "commit first"
        )
    if draft["uncommitted_changes"]:
        raise ValueError(
            "The draft was run with uncommitted changes - run overlay on a clean checkout "
            "first:\n" + "\n".join(draft["uncommitted_changes"])
        )


def add_gpkg_layer(out_file, source, layer_name, *args):
    """add a layer from source (a file, or a PG: connection with a table or -sql in args) to
    geopackage out_file as layer_name, creating out_file if needed"""
    cmd = ["ogr2ogr", "-f", "GPKG"]
    if os.path.exists(out_file):
        cmd.append("-update")
    subprocess.run(
        cmd + [out_file, source, *args, "-nln", layer_name],
        check=True,
    )


def read_release_logs(store, work_dir):
    """read the change logs (logs/<log>.csv), downloading them to work_dir

    returns {log: df} with each df every release's rows, in release order (each release
    appends its own)
    """
    histories = {}
    for log in LOGS:
        local_file = os.path.join(work_dir, "logs", f"{log}.csv")
        store.download(f"logs/{log}.csv", local_file)
        histories[log] = pandas.read_csv(local_file)
    return histories


def build_change(current_file, history, previous_release, columns):
    """merge this run's totals with the previous release's, keyed on the first column

    Retains every category present in either side (outer join) rather than just those in
    the previous release. Descriptive columns are filled from whichever side has them, so a
    category new to this run (or one dropped since the previous release) still gets its
    labels. Totals/diff/pct_diff are left as genuine NaN (not 0) for a category missing from
    one side, since 0 would misleadingly imply the category existed with a zero total.
    """
    key, *descriptive = columns

    previous = history[history["release_tag"] == previous_release][
        columns + ["area_ha"]
    ].rename(columns={"area_ha": previous_release})
    current = pandas.read_csv(current_file)[columns + ["area_ha"]].rename(
        columns={"area_ha": "current"}
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
        .round({previous_release: 0, "current": 0, "diff": 0, "pct_diff": 2})
        .set_index(key)
    )


def check_version_order(version, histories):
    """refuse unless version sorts after the latest release - versions sort by date, and the
    change logs are in release order, so a version can't be released after a later one.
    Returns the latest release, the last logged"""
    latest_release = next(iter(histories.values()))["release_tag"].iloc[-1]
    if version <= latest_release:
        raise ValueError(
            f"{version} doesn't sort after the latest release, {latest_release} - use a "
            "later version (vYYYY-MM, or vYYYY-MM-DD for a hotfix)"
        )
    return latest_release


def tag_release(version, commit):
    """tag commit as version's release, and push the tag - a retried release finds its tag
    already there, but a tag of the version on another commit is refused"""
    if git("tag", "--list", version):
        tagged = git("rev-parse", f"{version}^{{commit}}")
        if tagged != commit:
            raise ValueError(
                f"Tag {version} is already on commit {tagged}, not the draft's {commit}"
            )
    else:
        git("tag", "-a", version, "-m", version, commit)
    git("push", "origin", f"refs/tags/{version}")
    LOG.info(f"Tagged {commit} {version}")


def publish_draft(store, version, draft_dir="drafts"):
    """publish the local draft as version's next draft, in drafts/ in the bucket

    draft_dir holds overlay's outputs: harvest_restrictions.gpkg,
    harvest_restrictions_sources.gpkg, sources.csv, the area totals (<log>.csv), and
    overlay_run.json. Refuses unless the draft is of the checked-out commit, with no
    uncommitted changes, and the version sorts after the latest release. The summaries
    comparing the totals to the most recent release are built here, and archived with the
    rest - the draft itself is left as is. The archive is built in a temporary directory,
    removed once it's published - the draft is the local copy.

    Drafts are numbered from 1, after the version's already published - returns the
    published draft's name, <version>-draft<n>
    """
    if not VERSION.fullmatch(version):
        raise ValueError(
            f"{version} isn't a version - vYYYY-MM, or vYYYY-MM-DD for a hotfix"
        )
    commit = git("rev-parse", "HEAD")
    check_draft(draft_dir, commit)

    summaries = [f"{log}_summary.csv" for log in LOGS]
    with tempfile.TemporaryDirectory() as tmp_dir:
        histories = read_release_logs(store, tmp_dir)
        previous_release = check_version_order(version, histories)

        n = max(
            (int(DRAFT.fullmatch(d)["n"]) for d in published_drafts(store, version)),
            default=0,
        )
        name = f"{version}-draft{n + 1}"
        key = draft_key(name)

        for log, columns in LOGS.items():
            build_change(
                os.path.join(draft_dir, f"{log}.csv"),
                histories[log],
                previous_release,
                columns,
            ).to_csv(os.path.join(tmp_dir, f"{log}_summary.csv"))
            LOG.info(f"{log}_summary.csv written, comparing to {previous_release}")

        archive = os.path.join(tmp_dir, os.path.basename(key))
        zip_files(
            archive,
            [
                os.path.join(tmp_dir if name in summaries else draft_dir, name)
                for name in ARCHIVED_FILES
            ],
        )
        store.upload(archive, key)
    LOG.info(f"{name} (commit {commit}) published to {store.url(key)}")
    return name


def publish_release(store, draft):
    """release a published draft (<version>-draft<n>) - tagging its commit <version>, and
    pushing the tag, before publishing anything

    Refuses (before tagging or publishing anything) if the version is already released, or
    doesn't sort after the latest release, or if the draft's summaries compare against
    an older release than the latest.

    Everything is downloaded and built in a temporary directory before anything is
    published, each file at the path of its key in the bucket, and removed once published -
    nothing is kept locally. The release archive is copied from the draft's in the
    bucket. The release archive is published last: it marks the release as done, so a
    failure partway through publishing can be retried, and the retry overwrites whatever had
    been published so far.

    Once the archive is published, the version's drafts are deleted - the release is a copy
    of the one promoted.
    """
    match = DRAFT.fullmatch(draft)
    if not match:
        raise ValueError(
            f"{draft} isn't a draft - <version>-draft<n>, eg v2026-10-draft2"
        )
    version = match["version"]

    with tempfile.TemporaryDirectory() as work_dir:

        def local(key):
            return os.path.join(work_dir, key)

        archive_key = release_key(version)
        if archive_key in store.list(archive_key):
            raise ValueError(
                f"Release {version} is already published at {store.url(archive_key)} - it "
                "has already been released"
            )

        # the archive is published last, so any rows of this release in the logs are from an
        # attempt that failed before finishing - dropped here, and published again
        histories = read_release_logs(store, work_dir)
        for log, history in histories.items():
            if version in history["release_tag"].values:
                LOG.warning(
                    f"Release {version} is partially logged (in {log}) - re-publishing it"
                )
                histories[log] = history[history["release_tag"] != version]
        previous_release = check_version_order(version, histories)

        published = draft_key(draft)
        if published not in store.list(published):
            raise ValueError(f"{draft} isn't published at {store.url(published)}")

        # the draft's archive - its files are the latest files, as is, its summaries give
        # the release's change log rows, and its overlay_run.json the commit to tag
        store.download(published, local(published))
        with zipfile.ZipFile(local(published)) as z:
            z.extractall(local("latest"))
        latest_keys = [f"latest/{name}" for name in ARCHIVED_FILES]
        with open(local("latest/overlay_run.json")) as f:
            commit = json.load(f)["commit"]

        # the draft's summaries compare against whichever release was latest when it
        # ran - the summaries' column after "current", named for that release. If another
        # release has happened since, their diffs are against an older release
        columns = list(pandas.read_csv(local(f"latest/{next(iter(LOGS))}_summary.csv")))
        draft_previous = columns[columns.index("current") + 1]
        if draft_previous != previous_release:
            raise ValueError(
                f"{draft}'s summaries compare against {draft_previous}, but the latest "
                f"release is now {previous_release} - their diffs are against an older "
                f"release. Publish a new draft to compare against {previous_release}"
            )

        # this release's change log rows, from each summary's "current" column - a category
        # with no current value existed in the previous release but not this run, and is
        # excluded rather than logged as zero - appended to the change logs
        log_keys = []
        for log, columns in LOGS.items():
            rows = (
                pandas.read_csv(local(f"latest/{log}_summary.csv"))[
                    columns + ["current"]
                ]
                .rename(columns={"current": "area_ha"})
                .dropna(subset=["area_ha"])
            )
            # first, for sorting and filtering the logs by eye
            rows.insert(0, "release_tag", version)

            rows = pandas.concat([histories[log], rows], ignore_index=True)
            rows.to_csv(local(f"logs/{log}.csv"), index=False)
            log_keys.append(f"logs/{log}.csv")

        # everything is built - tag the release, then publish. The latest files and change
        # logs, then the archive - the draft's, as is, copied in the bucket - never
        # overwritten once released, marking the release as done
        LOG.info(f"Releasing {version}: {draft}, commit={commit}")
        tag_release(version, commit)
        for key in latest_keys + log_keys:
            store.upload(local(key), key)
            LOG.info(f"{local(key)} published to {store.url(key)}")
        store.copy(published, archive_key)
        LOG.info(f"{store.url(published)} copied to {store.url(archive_key)}")
    LOG.info(f"Released {version}")

    # the version's drafts - after the archive, so a failed release keeps them for its retry
    for name in published_drafts(store, version):
        store.delete(draft_key(name))
        LOG.info(f"Deleted {store.url(draft_key(name))}")


@click.group()
def cli():
    pass


@cli.command()
@click.argument("sources_file", type=click.Path(exists=True), default="sources.json")
@click.option(
    "--source_alias",
    "-s",
    default=None,
    help="Validate and download just the specified source",
)
@click.option(
    "--dry_run", "-t", is_flag=True, help="Validate sources_file only, do not download"
)
@click.option(
    "--out_path",
    "-o",
    type=click.Path(),
    default=CACHE_PATH,
    help="Output path to write data (local or s3://), defaults to s3://$BUCKET/harvest_restrictions/cache if $BUCKET is set",
)
@verbose_opt
@quiet_opt
def cache(sources_file, source_alias, dry_run, out_path, verbose, quiet):
    """Download sources defined in provided sources.json file"""
    configure_logging((verbose - quiet))

    if not out_path and not dry_run:
        raise ValueError("Output path not provided, set --out_path or $BUCKET")

    # load sources file
    with open(sources_file, "r") as f:
        sources = parse_sources(json.load(f))

    # if specified, use only one source
    if source_alias:
        if source_alias not in [s["alias"] for s in sources]:
            raise ValueError(f"Source {source_alias} is not present in {sources_file}")
        else:
            sources = [s for s in sources if s["alias"] == source_alias]

    sources = validate_sources(sources)

    # download each data source, dump to file
    if not dry_run:
        for source in sources:
            df = download_source(source)
            layer = (
                "hr_" + str(source["index"]).zfill(2) + "_" + source["alias"].lower()
            )
            # parquet is one file per layer and direct write to s3 is supported
            out_file = os.path.join(out_path, layer + ".parquet")
            df.to_parquet(out_file)

            LOG.info(f"{source['alias']} written to {out_file}")


@cli.command()
@click.option(
    "--path",
    "-p",
    type=click.Path(),
    default=CACHE_PATH,
    help="Path to clear cached source parquet files from (local or s3://), defaults to s3://$BUCKET/harvest_restrictions/cache if $BUCKET is set",
)
@click.option(
    "--dry_run",
    "-t",
    is_flag=True,
    help="List files that would be removed, do not delete",
)
@verbose_opt
@quiet_opt
def clear_cache(path, dry_run, verbose, quiet):
    """Delete cached parquet files written by cache (hr_*.parquet)"""
    configure_logging((verbose - quiet))

    if not path:
        raise ValueError("Path not provided, set --path or $BUCKET")

    if path.startswith("s3://"):
        cmd = [
            "aws",
            "s3",
            "rm",
            path,
            "--recursive",
            "--exclude",
            "*",
            "--include",
            "hr_*.parquet",
        ]
        if dry_run:
            cmd.append("--dryrun")
        subprocess.run(cmd, check=True)
    else:
        files = glob.glob(os.path.join(path, "hr_*.parquet"))
        for f in files:
            if dry_run:
                LOG.info(f"Would remove {f}")
            else:
                os.remove(f)
                LOG.info(f"Removed {f}")

    verb = "Would clear" if dry_run else "Cleared"
    LOG.info(f"{verb} cached source parquet files from {path}")


@cli.command()
@click.argument("sources_file", type=click.Path(exists=True), default="sources.json")
@click.option(
    "--in_path",
    "-p",
    type=click.Path(),
    default=CACHE_PATH,
    help="Path to read data (local or s3://), defaults to s3://$BUCKET/harvest_restrictions/cache if $BUCKET is set",
)
@click.option(
    "--db_url",
    "-db",
    help="Target database url, defaults to $DATABASE_URL environment variable if set",
    default=os.environ.get("DATABASE_URL"),
)
@click.option(
    "--out_table",
    "-o",
    help="Target output table. Appended to if the table already exists.",
)
@click.option(
    "--source_alias",
    "-s",
    default=None,
    help="Load just the specified source",
)
@click.option(
    "--truncate",
    is_flag=True,
    help="Truncate --out_table before loading, so re-running against it doesn't duplicate "
    "previously loaded rows. No effect without --out_table, since each per-source table is "
    "already replaced fresh on every load.",
)
@click.option(
    "--dry_run", "-t", is_flag=True, help="Validate sources_file only, do not load data"
)
@verbose_opt
@quiet_opt
def load_db(
    sources_file,
    in_path,
    db_url,
    out_table,
    source_alias,
    truncate,
    dry_run,
    verbose,
    quiet,
):
    """Load source layers from parquet cache to the postgresql db"""
    configure_logging((verbose - quiet))

    if not in_path and not dry_run:
        raise ValueError("Input path not provided, set --in_path or $BUCKET")

    # connect to db
    db = create_engine(db_url)

    # load sources file
    with open(sources_file, "r") as f:
        sources = parse_sources(json.load(f))

    # if specified, use only one source
    if source_alias:
        if source_alias not in [s["alias"] for s in sources]:
            raise ValueError(f"Source {source_alias} is not present in {sources_file}")
        else:
            sources = [s for s in sources if s["alias"] == source_alias]

    # only validate on dry-run
    if dry_run:
        sources = validate_sources(sources)

    else:
        if truncate:
            if out_table and inspect(db).has_table(out_table):
                with db.begin() as conn:
                    conn.execute(text(f"TRUNCATE TABLE {out_table}"))
                LOG.info(f"Truncated {out_table}")
            elif not out_table:
                LOG.warning("--truncate has no effect without --out_table")

        for source in sources:
            layer = (
                "hr_" + str(source["index"]).zfill(2) + "_" + source["alias"].lower()
            )
            in_file = os.path.join(in_path, layer + ".parquet")
            df = geopandas.read_parquet(in_file)
            # if out_table specified, write to that table, appending if it exists
            if out_table:
                df.to_postgis(out_table, db, if_exists="append")
                LOG.info(f"{source['alias']} written to {out_table}")
            # if out_table not provided, write to table with the layer name, overwriting if it exists
            else:
                df.to_postgis(layer, db, if_exists="replace")
                LOG.info(f"{source['alias']} written to {layer}")


@cli.command()
@click.option(
    "--db_url",
    "-db",
    help="Target database url, defaults to $DATABASE_URL environment variable if set",
    default=os.environ.get("DATABASE_URL"),
)
@click.option(
    "--designations_table",
    default="designations",
    help="Name of the source designations table (as loaded by load-db), dumped to geopackage alongside overlay outputs",
)
@verbose_opt
@quiet_opt
def overlay(db_url, designations_table, verbose, quiet, draft_dir="drafts"):
    """Run per-tile overlay, writing the results to the database and the draft in drafts/

    Runs at any commit. The commit, and any uncommitted changes, are recorded in the draft's
    overlay_run.json - publish-draft only publishes a draft of the checked-out commit, with no
    changes.
    """
    configure_logging((verbose - quiet))

    if not db_url:
        raise ValueError(
            "Target database url not provided, set --db_url or $DATABASE_URL"
        )

    # remove the previous draft - the results are about to be replaced
    commit, changes = checkout_state()
    shutil.rmtree(draft_dir, ignore_errors=True)

    psql = f"psql {db_url} -v ON_ERROR_STOP=1"

    # load 250k grid
    run("bcdata bc2pg WHSE_BASEMAPPING.NTS_250K_GRID")

    # clear any existing data from output table
    run(f'{psql} -c "TRUNCATE harvest_restrictions"')

    # run overlays in parallel per tile
    run(
        f'{psql} -tXA -c "SELECT DISTINCT map_tile '
        "FROM whse_basemapping.nts_250k_grid "
        'ORDER BY map_tile" '
        f"| parallel --tag {psql} -f sql/overlay.sql -v tile={{1}}"
    )

    os.makedirs(draft_dir)

    # the result (harvest_restrictions layer), and the source designations (designations layer)
    add_gpkg_layer(
        os.path.join(draft_dir, "harvest_restrictions.gpkg"),
        f"PG:{db_url}",
        "harvest_restrictions",
        "-nlt",
        "MULTIPOLYGON",
        "-sql",
        """select
          harvest_restrictions_id,
          land_designation_name,
          land_designation_type_rank,
          land_designation_type_code,
          land_designation_type_name,
          land_designation_primary_key,
          harvest_restriction_class_rank,
          harvest_restriction_class_name,
          array_to_json(trim_array(all_land_desig_names, 1)) as all_land_desig_names,
          array_to_json(trim_array(all_land_desig_type_ranks, 1)) as all_land_desig_type_ranks,
          array_to_json(trim_array(all_land_desig_type_codes, 1)) as all_land_desig_type_codes,
          array_to_json(trim_array(all_land_desig_type_names, 1)) as all_land_desig_type_names,
          array_to_json(trim_array(all_land_desig_primary_keys, 1)) as all_land_desig_primary_keys,
          array_to_json(trim_array(all_harv_restrict_class_ranks, 1)) as all_harv_restrict_class_ranks,
          array_to_json(trim_array(all_harv_restrict_class_names, 1)) as all_harv_restrict_class_names,
          map_tile_250k,
          geom
        from harvest_restrictions
        where
        all_harv_restrict_class_ranks @> ARRAY[6] and
        all_harv_restrict_class_ranks != ARRAY[6]""",
    )
    add_gpkg_layer(
        os.path.join(draft_dir, "harvest_restrictions_sources.gpkg"),
        f"PG:{db_url}",
        "designations",
        designations_table,
    )

    # area totals, compared to the previous release in the summaries publish-draft builds
    for log in LOGS:
        run(f"{psql} -f sql/{log}.sql --csv > {os.path.join(draft_dir, log + '.csv')}")

    # flatten sources.json to a csv, for review alongside the rest of the outputs
    write_sources_csv("sources.json", os.path.join(draft_dir, "sources.csv"))

    # a checkout changed during the run (a commit, another checkout, an edit) means the
    # results are no commit's in particular
    if checkout_state() != (commit, changes):
        changes = ["the checkout changed during the run"] + changes
    write_overlay_run(draft_dir, commit, changes)
    LOG.info(
        f"Overlay of commit {commit} written to {draft_dir}"
        + (", with uncommitted changes:\n" + "\n".join(changes) if changes else "")
    )


@cli.command("publish-draft")
@click.argument("version")
@bucket_opt
@verbose_opt
@quiet_opt
def publish_draft_command(version, bucket, verbose, quiet):
    """Publish the local draft in drafts/ as VERSION's next draft

    VERSION is vYYYY-MM, or vYYYY-MM-DD for a hotfix. The draft must be of an overlay run on
    the checked-out commit with no uncommitted changes. It's published to drafts/ in the
    bucket as <version>-draft<n>, numbered after the version's drafts already published
    """
    configure_logging((verbose - quiet))
    publish_draft(S3Store(bucket), version)


@cli.command()
@click.argument("draft")
@bucket_opt
@verbose_opt
@quiet_opt
def release(draft, bucket, verbose, quiet):
    """Release DRAFT, a published draft (eg v2026-10-draft2)

    Tags the draft's commit with its version (eg v2026-10) and pushes the tag, then
    publishes the fixed-name latest files under latest/, appends the release's rows to the
    change logs under logs/, and publishes its archive under releases/ - see
    publish_release(). The release is built in a temporary directory first
    """
    configure_logging((verbose - quiet))
    publish_release(S3Store(bucket), draft)


if __name__ == "__main__":
    cli()
