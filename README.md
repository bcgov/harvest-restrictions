# harvest-restrictions

[![Lifecycle:Stable](https://img.shields.io/badge/Lifecycle-Stable-97ca00)](https://github.com/bcgov/repomountie/blob/master/doc/lifecycle-badges.md)

Scripts to create [Generalized Forest Harvesting Restrictions](https://catalogue.data.gov.bc.ca/dataset/generalized-forest-harvesting-restrictions) - a "*spatial representation of various land designations that stipulate, to varying degrees, limits on forest harvesting activity*."


## Method

Each source land designation / restriction is defined as a 'layer' in [`sources.json`](sources.json).  Sources are defined in descending order of importance - if different sources overlap, data from the uppermost source takes priority. 

| key          | description |
|--------------|-------------|
| `alias`      | Key to uniquely identify the data source when processing (lower case, _ separated)      |
| `description`| Description of the data source |
| `source_type`| Type of data - types of `BCGW` and `FILE` are supported |
| `source`     | For sources of type `BCGW`, the table name. For sources of type `FILE`, the url or path |
| `layer`      | For sources of type `FILE`, the layer to use within the file (optional) |
| `query`      | Query to subset data in source/layer - ECQL for BCGW sources, OGR SQL for files (optional) |
| `primary_key`| The column holding primary key for the given source          |
| `field map`  | Mapping of source column names to new column names   |
| `data`       | Additional data to add to all records of the source, as key(column):value pair |

The `harvest_restriction` key in the `data` field defines the level of restriction a the given source, where restrictions are:

| `harvest_restriction` | description |
|-----------------------|-------------|
| `1`                   | Protected             |
| `2`                   | Prohibited            |
| `3`                   | High Restricted       |
| `4`                   | Medium Restricted     |
| `5`                   | Low Restricted        |
| `6`                   | No Special Restriction|

For example, this defines National Parks - data come from the BCGW, all parks are included (no query), the name of interest is held in the column `ENGLISH_NAME`, and these features have a `harvest_restriction` of 1 (Protected):

```
    {
        "alias": "park_national",
        "description": "National Park",
        "source_type": "BCGW",
        "source": "WHSE_ADMIN_BOUNDARIES.CLAB_NATIONAL_PARKS",
        "primary_key": "NATIONAL_PARK_ID",
        "query": null,
        "field_mapper": {"name": "ENGLISH_NAME"},
        "data": {"harvest_restriction": 1}
    },
```

See `source.schema.json` for a full description.

## Output spatial file

Output `harvest_restrictions.gdb` has the following columns:

| column                            | type    | description                                                      |
|-----------------------------------|---------|------------------------------------------------------------------|
| `harvest_restrictions_id`         | Integer | Polygon unique identifier                                        |
| `land_designation_name`           | String  | The highest ranking designation for given polygon                |
| `land_designation_type_rank`      | Integer | The rank of the designation type for given polygon               |    
| `land_designation_type_code`      | String  | The code of the designation type for given polygon               |
| `land_designation_type_name`      | String  | The name of the designation type for given polygon               |
| `land_designation_primary_key`    | String  | The primary key value for the highest ranking designation for the given polygon |   
| `harvest_restriction_class_rank`  | Integer | The rank of the harvest restriction class for given polygon      |    
| `harvest_restriction_class_name`  | String  | The name of the harvest restriction class for given polygon      |
| `all_land_desig_names`            | String  | All designations that apply to the given polygon                 |   
| `all_land_desig_type_ranks`       | String  | All designations ranks that apply to the given polygon           |   
| `all_land_desig_type_codes`       | String  | All designations types that apply to the given polygon           |   
| `all_land_desig_type_names`       | String  | All designations type names that apply to the given polygon      |   
| `all_land_desig_primary_keys`     | String  | Primary key values for all designations that apply to the given polyon |   
| `all_harv_restrict_class_ranks`   | String  | All harvest restriction class ranks that apply to the given polygon |   
| `all_harv_restrict_class_names`   | String  | All harvest restriction class names that apply to the given polygon |
| `map_tile_250k`                   | String  | NTS 1:250,000 tile name                                          |   

Note that the `all_*` columns are JSON arrays listing every designation applying to the polygon, in rank order.
The nth entry of each describes the same designation, and a missing value is `null` (eg a designation with no name or primary key).


## Development and testing setup

    git clone git@github.com:bcgov/harvest-restrictions.git
    cd harvest_restrictions
    docker compose build
    docker compose up -d

Committing changes requires `pre-commit` - install via your package manager of choice.

The `harvest_restrictions` object storage bucket does not require versioning - every `overlay` run publishes to its own `drafts/<run_id>/` prefix, and every release writes its own uniquely-named files, so nothing `release` depends on is ever overwritten. Versioning remains useful as insurance against accidental deletes. See "Object storage layout" below.

## Usage

1. Identify any file based sources for which download cannot be scripted, manually upload file to object storage.

2. If making changes to `harvest_restrictions.py`, test the changes:

        docker compose run -it --rm runner python -m pytest -v -rxXs

3. Edit `sources.json` as required (note that sources will likely be provided as a csv file)

4. Validate `sources.json`:
    
        docker compose run -it --rm runner python harvest_restrictions.py cache --dry_run -v

5. Download all restriction sources listed in `sources.json`, saving to geoparquet in `s3://$BUCKET/harvest_restrictions/cache` (pass `--out_path` to write elsewhere):

        docker compose run -it --rm runner python harvest_restrictions.py cache -v

    Optionally, clear the cache first - `cache` overwrites the files for sources it downloads, but doesn't remove anything for sources since removed or renamed, so `clear-cache` is useful for tidying those up. `clear-cache` only ever removes the `hr_*.parquet` files `cache` itself writes, so it's safe to point at a shared prefix:

        docker compose run -it --rm runner python harvest_restrictions.py clear-cache -v

6. Load restrictions layers from the cached geoparquet to postgresql (pass `--in_path` to read from elsewhere). `--out_table` appends, so pass `--truncate` when re-running against a table already loaded (e.g. re-running this step after a partial failure) to avoid duplicating rows:

        docker compose run -it --rm runner python harvest_restrictions.py load-db -v --out_table designations --truncate

7. Run overlays, dump resulting layer and summaries to geopackage/csv, and publish these outputs to object storage under `drafts/<run_id>/`. This also compares the new summaries to the most recently released version:

        docker compose run -it --rm runner python harvest_restrictions.py overlay -v

8. Review the change report (and `harvest_restrictions.gpkg.zip`, e.g. for external/client review - both are already published to object storage under the run's `drafts/<run_id>/` prefix):

    - `land_designations_summary.csv`
    - `harvest_restrictions_summary.csv`

    If results are not correct, address the issue, commit the fix, and re-run from step 3 (presuming the issue is with sources/data).

9. Once results are confirmed to be reasonable/correct, tag the commit as a release:

        git tag -a vYYYY-MM -m vYYYY-MM

10. Push the tag - this triggers the [Release workflow](https://github.com/bcgov/harvest-restrictions/actions/workflows/release.yaml), which publishes a zip archive of that commit's already-published, already-reviewed output, and adds the release to the change logs:

        git push origin vYYYY-MM

    Alternatively, run the release step locally instead of pushing the tag:

        docker compose run -it --rm runner python harvest_restrictions.py release -v

    The workflow then deletes the drafts of released runs (`prune-drafts`, below).

11. Optionally, prune drafts - by default only runs that have been released (everything they published was copied into the release), and with `--older_than` also unreleased runs older than that many days, apart from the newest unreleased run. Use `--dry_run` to list what would be deleted:

        docker compose run -it --rm runner python harvest_restrictions.py prune-drafts -v --older_than 90

12. Optionally, re-run the entire download/process pipeline by manually calling the [harvest-restrictions workflow](https://github.com/bcgov/harvest-restrictions/actions/workflows/harvest-restrictions.yaml).


## Object storage layout


```
s3://$BUCKET/harvest_restrictions/
├── cache/                                    # per-source geoparquet cache, written by `cache`
│   ├── hr_01_park_national.parquet           #   (sources transformed to the standard columns)
│   ├── hr_02_park_er.parquet
│   └── ...
├── sources/                                  # sources not available for download elsewhere,
│   ├── CRD.gdb.zip                           #   in their native format - referenced by sources.json
│   └── ...
├── drafts/                                   # unreviewed overlay output, written by `overlay`
│   └── <run_id>/                             # one prefix per run, never rewritten
│       ├── harvest_restrictions.gpkg.zip
│       ├── harvest_restrictions_sources.gpkg.zip
│       ├── land_designations_summary.csv
│       ├── harvest_restrictions_summary.csv
│       ├── sources.csv
│       └── manifest.json                     # written last - marks the run complete
├── latest/                                   # the latest release's files, written by `release`
│   ├── harvest_restrictions.gpkg
│   ├── harvest_restrictions_sources.gpkg
│   ├── land_designations_summary.csv
│   ├── harvest_restrictions_summary.csv
│   └── sources.csv
├── logs/                                     # change logs, written by `release`
│   ├── land_designations.csv                 # combined, rebuilt on every release
│   ├── harvest_restrictions.csv
│   └── <release_tag>/                        # each release's rows
│       ├── land_designations.csv
│       └── harvest_restrictions.csv
└── releases/                                 # permanent per-release archives, written by `release`
    └── harvest_restrictions_<release_tag>.zip
        ├── harvest_restrictions.gpkg
        │   ├── harvest_restrictions       (spatial layer)
        │   └── designations               (spatial layer)
        ├── land_designations_summary.csv
        ├── harvest_restrictions_summary.csv
        └── sources.csv
```

**`cache/`** - written by `cache`, untagged, one geoparquet per source (`hr_<NN>_<alias>.parquet`), overwritten on the next `cache` run for that source. `clear-cache` removes these directly by filename pattern, safe to point at a shared prefix since it only ever touches its own `hr_*.parquet` files.

**`drafts/<run_id>/`** - written by `overlay`, one prefix per run, never rewritten. `manifest.json` (the run's `commit`, `run_id`, the `previous_release` its summaries compare against, and its file list) is written last, so a run that failed partway through publishing has no manifest and is never released. Transient by design - `prune-drafts` (run by the release workflow) deletes released runs' prefixes, and with `--older_than` old unreleased ones. Pruning by age can also be left to a lifecycle expiration rule on `drafts/` (released runs are fully copied into the release, so pruning them loses nothing; pruning an unreleased run just means re-running `overlay` before releasing it):

- `harvest_restrictions.gpkg.zip`, `harvest_restrictions_sources.gpkg.zip` - the raw overlay result and its source designations, each a zipped geopackage
- `land_designations_summary.csv`, `harvest_restrictions_summary.csv` - a disposable rollup, rebuilt from scratch on every `overlay` run (by `log`), comparing the *most recent release* against the *current* run with `current`/`diff`/`pct_diff` columns. Retains every category present in either side - a category new to this run or dropped since the previous release still gets its labels, with `diff`/`pct_diff` left as `NaN` rather than misleadingly implying zero area. This is what you review in step 8 above, and what `release` reads (via the `current` column) to append this run's totals to the durable change log - there's no separate current-only file, since this already carries the same totals plus the diff.
- `sources.csv` - a flattened `sources.json` as it stood for this run, for review alongside the summary csvs

**`logs/<release_tag>/land_designations.csv`, `logs/<release_tag>/harvest_restrictions.csv`** - written only by `release`, that release's change log rows, never rewritten. One row per category (`release_tag`, `release_date`, `commit`, `run_id`, `released_at`, category columns, `area_ha`). These are the change log's source of truth: `overlay` reads them to find the previous release to compare against, and `release` refuses to release a tag already logged here. `released_at` (when the release was made) orders the releases - `release_date` is the released commit's author date, which needn't follow release order.

**`logs/land_designations.csv` / `logs/harvest_restrictions.csv`** - the combined change logs, every release's rows from `logs/<release_tag>/` in `released_at` order. Rebuilt by `release` each time, so if damaged they're fixed by the next release.

**`latest/`** - `harvest_restrictions.gpkg`, `harvest_restrictions_sources.gpkg`, `land_designations_summary.csv`, `harvest_restrictions_summary.csv` and `sources.csv`: the latest release's files, written only by `release`, at fixed plain-named keys (no suffix), overwritten on every release. The same five deliverables as separate files rather than one geopackage, for scripts/mapping applications that just want the current release without tracking release tags - point at these instead of the `releases/` archive. Fully redundant with the latest release's archive.

**`releases/harvest_restrictions_<release_tag>.zip`** - written only by `release`, never overwritten, so every past release stays retrievable by tag. A single download holding the release - only the archive's name carries the release tag: one geopackage (`harvest_restrictions.gpkg`) with both spatial layers - unlike `latest/harvest_restrictions.gpkg`, which holds just the `harvest_restrictions` layer - the reviewed summaries comparing it to the previous release, and `overlay`'s reviewed `sources.csv`. The zip is seek-optimized (`sozip`), so the geopackage can be read in place without downloading or unzipping, e.g. `ogrinfo /vsizip//vsis3/$BUCKET/harvest_restrictions/releases/harvest_restrictions_<release_tag>.zip/harvest_restrictions.gpkg`.

### commit vs run_id

Every `overlay` run records both `commit` (the git commit that produced it) and `run_id` (a UTC timestamp identifying that specific invocation of `overlay`, and the name of its `drafts/` prefix) in its manifest. These are usually interchangeable - `release` defaults to the most recent run of a given commit - but they diverge if `overlay` is run more than once against the same commit (the underlying source data can change even with no code change). If a second run happens after you've reviewed the first, pass `--run_id` to `release` to pin the exact run that was actually reviewed, rather than picking up whatever ran most recently.

The logs were backfilled from pre-existing wide-format records, with `released_at` copied from `release_date`. `v2024-08`, `v2025-04`, `v2025-08`, and `v2026-02` carry real `commit`/`release_date` values from their matching git tags (the latter two were originally recorded under the old `-DRAFT` tag naming convention, since renamed). `v2023-07` and `v2024-04` have no corresponding git tag at all - for those two, `release_date` is approximated as the first of the tag's named month and `commit` is left blank.


## Release module (`datarelease.py`)

The draft/release/prune workflow above is implemented in `datarelease.py`, which knows nothing of harvest restrictions - it's driven entirely by `release.json`, and could be reused by any similar analysis (it depends only on `pandas`, `jsonschema`, the aws cli, and GDAL's `ogr2ogr` and `sozip` for geopackages and archives). `harvest_restrictions.py` supplies the analysis, and its `release`/`prune-drafts` commands are thin wrappers.

`release.json` describes what each run publishes (see `CONFIG_SCHEMA` in `datarelease.py`):

- `prefix` - everything is published under `s3://$BUCKET/<prefix>/`
- `archive` - name of the zip archive of each release, published under `releases/` (`{release_tag}` is replaced with the tag - required, so a downloaded archive says which release it is)
- `bundle` - name of a geopackage in the archive combining the outputs' layers (it may share a name with a `latest` file)
- `outputs` - each `file` a run publishes, optionally added to the bundle as `layer` (a non-spatial table for `.csv`), added to the `archive` as is, and/or published under `latest/` as a fixed-name `latest` file - as is if the same name, otherwise converted to a geopackage of that name
- `logs` - totals compared against the previous release and logged per release: the `summary` file to write, the category `key` and `columns`, the `value` column, and optionally `decimals` to round to (default 0)

To use it from another project:

```python
import datarelease

config = datarelease.load_config("release.json")
store = datarelease.S3Store(bucket, config["prefix"])

# after the analysis - files maps each configured output to a local path, current maps each
# log's name to a local csv of this run's totals (the log's columns plus its value column)
run_id = datarelease.publish_draft(config, store, files, current)

# on the tagged commit, after review
datarelease.release(config, store)

# delete drafts of released runs (and, with older_than, old unreleased runs)
datarelease.prune_drafts(config, store, older_than=90)
```

Its tests (`test_datarelease.py`) use an unrelated example config, and need no network access or bucket.


## designatedlands

This tool is adapted from the [designated lands script](https://github.com/bcgov/designatedlands). This tool could be used for that analysis by adding mine and oil and gas restriction levels to each source in `sources.json`. Note however that several components of `designatedlands` are not currently supported by this tool:

- raster based analysis
- config based pre-processing of input sources
- adjustment of tiled processing to include the sliver of BC's official boundary not covered by 250k tiles
- overlay of results with arbitrary admin or eco layer