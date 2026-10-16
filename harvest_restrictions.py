import csv
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

# the cache of sources transformed to the standard column layout, written by cache and read by
# load-db - not to be confused with harvest_restrictions/sources/, which holds the sources not
# available for download elsewhere, in their native format
CACHE_PATH = (
    f"s3://{os.environ['BUCKET']}/harvest_restrictions/cache"
    if os.environ.get("BUCKET")
    else None
)

# everything generated locally is written under here, laid out as in the bucket - each release
# candidate's archive in prereleases/, each release built in releases/<release_tag>/
OUTPUTS = "outputs"

# everything published is under this prefix in the bucket
PREFIX = "harvest_restrictions"

# the overlay's geopackage, holding the harvest_restrictions and designations layers - and
# the latest/ geopackage of the designations layer alone
OVERLAY_GPKG = "harvest_restrictions.gpkg"
SOURCES_GPKG = "harvest_restrictions_sources.gpkg"
SOURCES_CSV = "sources.csv"

# the area totals compared to the previous release and logged with each release, by log name -
# each log's columns, the first being its key. overlay builds each log's totals with
# sql/<log>.sql, and compares them to the previous release's in <log>_summary.csv
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
AREA = "area_ha"


def summary(log):
    return f"{log}_summary.csv"


# each release candidate's and release's archive: OVERLAY_GPKG plus these csvs
ARCHIVE = "harvest_restrictions_{tag}.zip"
ARCHIVED_CSVS = [SOURCES_CSV] + [summary(log) for log in LOGS]

SOURCES_CSV_COLUMNS = [
    "harvest_restriction",
    "alias",
    "description",
    "source",
    "source_type",
    "layer",
    "query",
    "name_field",
]

# note that the all_* lists are exported as JSON arrays to retain nulls and keep the arrays aligned
DUMP_SQL = """select
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
all_harv_restrict_class_ranks != ARRAY[6]"""


verbose_opt = click.option("--verbose", "-v", count=True, help="Increase verbosity.")
quiet_opt = click.option("--quiet", "-q", count=True, help="Decrease verbosity.")


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
        writer.writerow(["index"] + SOURCES_CSV_COLUMNS)
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


# --- release candidates and releases ---
#
# Each version is published first as one or more release candidates, then released:
#
# 1. tag the commit to run as a release candidate, <version>-rc.<n> (eg v2026-10-rc.1)
# 2. overlay, run on that tag, publishes prereleases/harvest_restrictions_<version>-rc.<n>.zip,
#    with summaries comparing its area totals to the latest release - for review
# 3. once a candidate is approved, tag its commit <version> (eg v2026-10). release, run on that
#    tag (by the release workflow), promotes the commit's latest candidate of that version:
#    - releases/harvest_restrictions_<version>.zip, a copy of the candidate's archive
#    - fixed-name latest files under latest/, overwritten by each release
#    - the release's change log rows under logs/<version>/, never rewritten
#    - combined change logs under logs/, rebuilt from every release's rows
# 4. prune-prereleases deletes the candidates of released versions
#
# A release is exactly the candidate that was reviewed - nothing is re-run or rebuilt. Nothing a
# later step depends on is ever overwritten, so the bucket needs no versioning.

# a release candidate's tag: <version>-rc.<n>
RC_TAG = re.compile(r"^(?P<version>.+)-rc\.(?P<n>[0-9]+)$")


def prerelease_key(rc_tag):
    """a release candidate's archive - written once, never overwritten"""
    return f"prereleases/{ARCHIVE.format(tag=rc_tag)}"


def release_key(release_tag):
    """a release's archive - written once, never overwritten"""
    return f"releases/{ARCHIVE.format(tag=release_tag)}"


def release_log_key(release_tag, log):
    """one release's change log rows - written once, never rewritten"""
    return f"logs/{release_tag}/{log}.csv"


def combined_log_key(log):
    """a combined change log, rebuilt from every release's rows on each release - next to (not
    in) the per-release logs/<release_tag>/ folders, so it's never read as one"""
    return f"logs/{log}.csv"


class S3Store:
    """the objects under PREFIX in an s3 bucket, via the aws cli

    Keys passed to and returned by every method are relative to PREFIX.
    """

    def __init__(self, bucket):
        self.bucket = bucket

    def url(self, key):
        return f"s3://{self.bucket}/{PREFIX}/{key}"

    def s3api(self, *args):
        return subprocess.run(
            ["aws", "s3api", *args, "--bucket", self.bucket, "--output", "json"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def upload(self, local_file, key, tags):
        subprocess.run(["aws", "s3", "cp", local_file, self.url(key)], check=True)
        tagging = {"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]}
        self.s3api(
            "put-object-tagging",
            "--key",
            f"{PREFIX}/{key}",
            "--tagging",
            json.dumps(tagging),
        )

    def tags(self, key):
        """the tags of the object at key"""
        tagging = json.loads(
            self.s3api("get-object-tagging", "--key", f"{PREFIX}/{key}")
        )
        return {t["Key"]: t["Value"] for t in tagging["TagSet"]}

    def download(self, key, local_file):
        """download to local_file, creating its directory if needed"""
        os.makedirs(os.path.dirname(local_file) or ".", exist_ok=True)
        subprocess.run(["aws", "s3", "cp", self.url(key), local_file], check=True)

    def list(self, prefix):
        """sorted keys under prefix"""
        out = self.s3api("list-objects-v2", "--prefix", f"{PREFIX}/{prefix}")
        # the cli prints nothing at all when no objects match
        contents = json.loads(out).get("Contents", []) if out.strip() else []
        return sorted(c["Key"][len(PREFIX) + 1 :] for c in contents)

    def delete(self, key):
        subprocess.run(["aws", "s3", "rm", self.url(key)], check=True)


def git(*args):
    return subprocess.check_output(["git", *args]).decode("ascii").strip()


def committed_head():
    """the checked-out commit, refusing if any tracked file has uncommitted changes

    A candidate is published as the commit's output, so it must be exactly what the commit
    produces. Untracked files are ignored: the analysis can only use one if a tracked file
    refers to it, and that change would itself be uncommitted.
    """
    changes = git("status", "--porcelain", "--untracked-files=no")
    if changes:
        raise ValueError(
            "Tracked files have uncommitted changes - commit or stash them first, so the "
            f"run is the commit's output:\\n{changes}"
        )
    return git("rev-parse", "HEAD")


def head_tags():
    """(release tags, release candidate tags) of the checked-out commit - candidates sorted
    oldest (lowest rc number) to newest"""
    tags = git("tag", "--points-at", "HEAD").split()
    rc_tags = sorted(
        (t for t in tags if RC_TAG.match(t)),
        key=lambda t: int(RC_TAG.match(t)["n"]),
    )
    return [t for t in tags if not RC_TAG.match(t)], rc_tags


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
    """read every release's change log rows (logs/<release_tag>/<log>.csv), downloading them
    to work_dir

    returns {log: df} with each df the rows of all releases combined and ordered by
    released_at, or None if no release has written that log yet
    """
    names = {f"{log}.csv": log for log in LOGS}
    frames = {log: [] for log in LOGS}
    for key in store.list("logs/"):
        parts = key.split("/")
        log = names.get(parts[2]) if len(parts) == 3 else None
        if log:
            local_file = os.path.join(work_dir, key)
            store.download(key, local_file)
            frames[log].append(pandas.read_csv(local_file))

    histories = {}
    for log, release_frames in frames.items():
        if not release_frames:
            histories[log] = None
            continue
        history = pandas.concat(release_frames, ignore_index=True)
        # released_at orders releases - tag names and commit dates needn't follow release order
        released_at = pandas.to_datetime(
            history["released_at"], utc=True, format="ISO8601"
        )
        histories[log] = history.iloc[released_at.argsort(kind="stable")].reset_index(
            drop=True
        )
    return histories


def latest_release(history):
    """tag of the most recent release in a history from read_release_logs(), or None"""
    if history is None:
        return None
    return history["release_tag"].iloc[-1]


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
        columns + [AREA]
    ].rename(columns={AREA: previous_release})
    current = pandas.read_csv(current_file)[columns + [AREA]].rename(
        columns={AREA: "current"}
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


def release_candidate(store):
    """(rc tag, commit) to run as the checked-out commit's newest release candidate - refusing
    if it has uncommitted changes, no candidate tag, or its newest is already published"""
    commit = committed_head()
    _, rc_tags = head_tags()
    if not rc_tags:
        raise ValueError(
            "The checked-out commit has no release candidate tag - tag it "
            "<version>-rc.<n> (eg v2026-10-rc.1) to run it"
        )
    rc_tag = rc_tags[-1]
    if prerelease_key(rc_tag) in store.list("prereleases/"):
        raise ValueError(
            f"{rc_tag} is already published at {store.url(prerelease_key(rc_tag))} - tag "
            "the next release candidate to run again"
        )
    return rc_tag, commit


def publish_prerelease(store, rc_tag, commit, run_dir, out_dir=OUTPUTS):
    """publish a release candidate's archive to prereleases/

    run_dir holds the run's outputs: OVERLAY_GPKG (with the harvest_restrictions and
    designations layers), SOURCES_CSV, and the area totals (<log>.csv). The summaries comparing
    those totals to the most recent release are written there by this, and archived with the
    rest. The archive is built in out_dir/prereleases/, kept locally.

    commit is the commit the run started on - refuses to publish if the checkout has changed
    since, as the run then isn't commit's output.
    """
    head = committed_head()
    if head != commit:
        raise ValueError(
            f"The checkout moved from {commit} to {head} during the run - re-run against "
            "the commit to publish"
        )

    with tempfile.TemporaryDirectory() as logs_dir:
        histories = read_release_logs(store, logs_dir)
    previous_release = None
    for log, columns in LOGS.items():
        history = histories[log]
        if history is None:
            raise ValueError(
                f"No {log} change log found under {store.url('logs/')} "
                "- release at least one version before comparing to it"
            )
        previous_release = latest_release(history)
        build_change(
            os.path.join(run_dir, f"{log}.csv"), history, previous_release, columns
        ).to_csv(os.path.join(run_dir, summary(log)))
        LOG.info(f"{summary(log)} written, comparing to {previous_release}")

    key = prerelease_key(rc_tag)
    archive = os.path.join(out_dir, key)
    os.makedirs(os.path.dirname(archive), exist_ok=True)
    zip_files(
        archive,
        [os.path.join(run_dir, name) for name in [OVERLAY_GPKG] + ARCHIVED_CSVS],
    )
    # the tags record what the candidate is, for release to check - previous_release is the
    # release its summaries compare against
    tags = {
        "commit": commit,
        "prerelease": rc_tag,
        "previous_release": previous_release,
    }
    store.upload(archive, key, tags)
    LOG.info(f"{rc_tag} published to {store.url(key)}")


def publish_release(store, out_dir=OUTPUTS):
    """release the checked-out commit's newest candidate of the version it's tagged with

    Refuses (before publishing anything) if the version is already released, or if the
    candidate's summaries compare against an older release than the latest.

    Everything is downloaded and built in out_dir/releases/<release_tag>/ before anything is
    published, each file at the path of its key in the bucket. The release's change log rows
    are published last: they mark the release as done, so a failure partway through
    publishing can be retried, and the retry overwrites whatever had been published so far.

    The candidates are left in place - prune-prereleases deletes them.
    """
    commit = git("rev-parse", "HEAD")
    release_date = git("log", "-1", "--format=%aI", commit)
    release_tags, rc_tags = head_tags()
    if len(release_tags) != 1:
        raise ValueError(
            f"The checked-out commit must have exactly one release tag, it has "
            f"{release_tags or 'none'}"
        )
    release_tag = release_tags[0]
    rc_tags = [t for t in rc_tags if RC_TAG.match(t)["version"] == release_tag]
    if not rc_tags:
        raise ValueError(
            f"No release candidate of {release_tag} on this commit - release the commit of "
            f"an approved candidate ({release_tag}-rc.<n>)"
        )
    rc_tag = rc_tags[-1]

    # cleared at the start of every run, so leftovers from a failed attempt are never reused
    work_dir = os.path.join(out_dir, "releases", release_tag)
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir)

    def local(key):
        return os.path.join(work_dir, key)

    # refuse to release a tag that's already logged. Each log's rows are published as a
    # separate file - if only some made it (a failure between the uploads), the release didn't
    # finish, so its partial rows are dropped here and the retry publishes them again
    histories = read_release_logs(store, work_dir)
    logged_in = [
        log
        for log, history in histories.items()
        if history is not None and release_tag in history["release_tag"].values
    ]
    if len(logged_in) == len(LOGS):
        raise ValueError(
            f"Release {release_tag} is already in {store.url(f'logs/{release_tag}')}/ "
            "- it has already been released"
        )
    for log in logged_in:
        LOG.warning(
            f"Release {release_tag} is only partially logged (in {log}) - re-publishing it"
        )
        history = histories[log]
        histories[log] = history[history["release_tag"] != release_tag]

    rc_key = prerelease_key(rc_tag)
    if rc_key not in store.list("prereleases/"):
        raise ValueError(
            f"{rc_tag} isn't published at {store.url(rc_key)} - run overlay on it first"
        )
    LOG.info(f"Releasing {release_tag}: {rc_tag}, commit={commit}")

    # the candidate's summaries compare against whichever release was latest when it ran. If
    # another release has happened since, their diffs are against an older release
    previous_release = latest_release(next(iter(histories.values())))
    rc_previous = store.tags(rc_key).get("previous_release")
    if rc_previous != previous_release:
        raise ValueError(
            f"{rc_tag}'s summaries compare against {rc_previous}, but the latest release is "
            f"now {previous_release} - their diffs are against an older release. Tag and run "
            f"a new release candidate to compare against {previous_release}"
        )

    # the release archive is the candidate's, as is - only its name differs
    store.download(rc_key, local(rc_key))
    archive_key = release_key(release_tag)
    os.makedirs(os.path.dirname(local(archive_key)))
    shutil.copy(local(rc_key), local(archive_key))

    # the latest files, from the archive - each layer of the geopackage as its own geopackage
    bundle_dir = local("bundle")
    with zipfile.ZipFile(local(rc_key)) as z:
        z.extractall(bundle_dir)
    os.makedirs(local("latest"))
    bundle = os.path.join(bundle_dir, OVERLAY_GPKG)
    add_gpkg_layer(
        local(f"latest/{OVERLAY_GPKG}"),
        bundle,
        "harvest_restrictions",
        "harvest_restrictions",
    )
    add_gpkg_layer(
        local(f"latest/{SOURCES_GPKG}"), bundle, "designations", "designations"
    )
    for name in ARCHIVED_CSVS:
        shutil.copy(os.path.join(bundle_dir, name), local(f"latest/{name}"))
    latest_keys = [
        f"latest/{name}" for name in [OVERLAY_GPKG, SOURCES_GPKG] + ARCHIVED_CSVS
    ]

    # this release's change log rows, from each summary's "current" column - a category with no
    # current value existed in the previous release but not this run, and is excluded rather
    # than logged as zero. released_at (when this release was made) orders the releases when
    # the logs are combined
    released_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    release_logs = []
    combined_logs = []
    for log, columns in LOGS.items():
        rows = (
            pandas.read_csv(os.path.join(bundle_dir, summary(log)))[
                columns + ["current"]
            ]
            .rename(columns={"current": AREA})
            .dropna(subset=[AREA])
        )
        rows["release_tag"] = release_tag
        rows["release_date"] = release_date
        rows["commit"] = commit[:7]
        rows["prerelease"] = rc_tag
        rows["released_at"] = released_at

        release_log = release_log_key(release_tag, log)
        os.makedirs(os.path.dirname(local(release_log)), exist_ok=True)
        rows.to_csv(local(release_log), index=False)
        release_logs.append(release_log)

        # the combined log, rebuilt from every release's rows (fetched above) plus this one's
        if histories[log] is not None:
            rows = pandas.concat([histories[log], rows], ignore_index=True)
        rows.to_csv(local(combined_log_key(log)), index=False)
        combined_logs.append(combined_log_key(log))

    # everything is built - publish. First the archive, never overwritten once released, then
    # the latest files and combined change logs, and finally this release's change log rows,
    # marking the release as done
    tags = {"commit": commit, "release": release_tag, "prerelease": rc_tag}
    for key in [archive_key] + latest_keys + combined_logs + release_logs:
        store.upload(local(key), key, tags)
        LOG.info(f"{local(key)} published to {store.url(key)}")
    LOG.info(f"Released {release_tag}")


def delete_prereleases(store, dry_run=False, out_dir=OUTPUTS):
    """delete the release candidates of released versions, and their local copies"""
    # a version is released once it's in every change log - one in only some is a release
    # that failed partway through publishing, whose retry still needs its candidate
    with tempfile.TemporaryDirectory() as logs_dir:
        histories = read_release_logs(store, logs_dir)
    released = set.intersection(
        *(
            set() if history is None else set(history["release_tag"])
            for history in histories.values()
        )
    )

    prefix, suffix = ARCHIVE.split("{tag}")
    for key in store.list("prereleases/"):
        # anything under prereleases/ that isn't a candidate's archive is left alone
        name = os.path.basename(key)
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        rc = RC_TAG.match(name[len(prefix) : -len(suffix)])
        if not rc or rc["version"] not in released:
            continue
        local_file = os.path.join(out_dir, key)
        if not os.path.exists(local_file):
            local_file = None
        if dry_run:
            LOG.info(f"Would delete {store.url(key)} ({rc['version']} is released)")
            if local_file:
                LOG.info(f"Would delete {local_file}")
        else:
            store.delete(key)
            LOG.info(f"Deleted {store.url(key)} ({rc['version']} is released)")
            if local_file:
                os.remove(local_file)
                LOG.info(f"Deleted {local_file}")


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
@click.option(
    "--bucket",
    "-b",
    default=os.environ.get("BUCKET"),
    help="Object storage bucket to write outputs, defaults to $BUCKET environment variable if set",
)
@verbose_opt
@quiet_opt
def overlay(db_url, designations_table, bucket, verbose, quiet):
    """Run per-tile overlay, publishing the outputs and summaries as a release candidate

    The checked-out commit must be tagged as a release candidate, <version>-rc.<n> - the
    newest such tag is published to prereleases/, and kept locally in outputs/prereleases/
    """
    configure_logging((verbose - quiet))

    if not db_url:
        raise ValueError(
            "Target database url not provided, set --db_url or $DATABASE_URL"
        )
    if not bucket:
        raise ValueError(
            "Target object storage bucket not provided, set --bucket or $BUCKET"
        )

    # the release candidate this run publishes - checked before the long-running overlay,
    # rather than once it's done
    store = S3Store(bucket)
    rc_tag, commit = release_candidate(store)
    LOG.info(f"Running release candidate {rc_tag}, commit={commit}")

    # the run's outputs are written to a temporary directory, removed when overlay exits -
    # just the release candidate's archive is kept, in outputs/prereleases/
    tmp_dir = tempfile.TemporaryDirectory()

    def tmp(name):
        return os.path.join(tmp_dir.name, name)

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

    # dump the result, and the source designations, to one geopackage
    add_gpkg_layer(
        tmp(OVERLAY_GPKG),
        f"PG:{db_url}",
        "harvest_restrictions",
        "-nlt",
        "MULTIPOLYGON",
        "-sql",
        DUMP_SQL,
    )
    add_gpkg_layer(
        tmp(OVERLAY_GPKG), f"PG:{db_url}", "designations", designations_table
    )
    LOG.info(f"Overlay results and {designations_table} written to {tmp(OVERLAY_GPKG)}")

    # area totals, compared to the previous release in the summaries
    for log in LOGS:
        run(f"{psql} -f sql/{log}.sql --csv > {tmp(log + '.csv')}")

    # flatten sources.json to a csv, for review alongside the rest of the outputs
    write_sources_csv("sources.json", tmp(SOURCES_CSV))

    publish_prerelease(store, rc_tag, commit, tmp_dir.name)
    tmp_dir.cleanup()


@cli.command()
@click.option(
    "--bucket",
    "-b",
    default=os.environ.get("BUCKET"),
    help="Object storage bucket to publish outputs to, defaults to $BUCKET environment variable if set",
)
@verbose_opt
@quiet_opt
def release(bucket, verbose, quiet):
    """Release the checked-out commit, which must be tagged with the release tag and a release
    candidate of it

    Promotes the commit's newest release candidate of the version: publishes its archive under
    releases/, the fixed-name latest files under latest/, and the release's change log rows
    under logs/<release_tag>/ - see publish_release(). The release is built locally first, in
    outputs/releases/<release_tag>/
    """
    configure_logging((verbose - quiet))

    if not bucket:
        raise ValueError(
            "Target object storage bucket not provided, set --bucket or $BUCKET"
        )
    publish_release(S3Store(bucket))


@cli.command()
@click.option(
    "--bucket",
    "-b",
    default=os.environ.get("BUCKET"),
    help="Object storage bucket to prune release candidates from, defaults to $BUCKET environment variable if set",
)
@click.option(
    "--dry_run",
    "-t",
    is_flag=True,
    help="List release candidates that would be deleted, do not delete",
)
@verbose_opt
@quiet_opt
def prune_prereleases(bucket, dry_run, verbose, quiet):
    """Delete the release candidates of released versions, and their local copies in
    outputs/prereleases/ - each release is a copy of its candidate's archive"""
    configure_logging((verbose - quiet))

    if not bucket:
        raise ValueError(
            "Target object storage bucket not provided, set --bucket or $BUCKET"
        )
    delete_prereleases(S3Store(bucket), dry_run)


if __name__ == "__main__":
    cli()
