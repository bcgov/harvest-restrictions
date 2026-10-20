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

Output `harvest_restrictions.gpkg` (layer `harvest_restrictions`) has the following columns:

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

Each release also includes `harvest_restrictions_sources.gpkg` (layer `designations`) - every source designation as loaded for the overlay.


## Development and testing setup

    git clone git@github.com:bcgov/harvest-restrictions.git
    cd harvest_restrictions
    docker compose build
    docker compose up -d

Committing changes requires `pre-commit` - install via your package manager of choice.

The database is set up by `sql/setup.sql` when its container is first created. Any changes to `setup.sql` require rebuilding the db image and recreating the database.

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

7. Run overlays, writing the results to the database and the local draft to `drafts/`. `overlay` runs at any commit (eg to test changes), recording the commit and any uncommitted changes in the draft's `overlay_run.json`:

        docker compose run -it --rm runner python harvest_restrictions.py overlay -v

    Each run replaces the local draft, which can be reviewed before publishing it:

    - `harvest_restrictions.gpkg`, `harvest_restrictions_sources.gpkg`, `sources.csv` - as archived in a release, along with `overlay_run.json`
    - `land_designations.csv`, `harvest_restrictions.csv` - the area totals, which `publish-draft` compares to the most recent release
    - `overlay_run.json` - what the draft was run on, written when `overlay` finishes (a draft without it is unfinished):

            {
              "commit": "f439b88c2d1e7a4b9f0c3e5a6b8d2f1e4c7a9b03",
              "uncommitted_changes": [" M sql/overlay.sql"],
              "finished_at": "2026-10-19T21:02:11+00:00"
            }

      `uncommitted_changes` lists the `git status` lines of changed tracked files other than documentation - empty for a clean checkout, and starting with `the checkout changed during the run` if it did

8. To publish the results for review, commit any changes (re-running `overlay` if the local draft was run with uncommitted changes), push the commit, and publish the local draft as the next draft of the version - a version is `vYYYY-MM`, or `vYYYY-MM-DD` for a hotfix, and must sort after the most recent release:

        docker compose run -it --rm runner python harvest_restrictions.py publish-draft v2026-10 -v

    `publish-draft` numbers the version's drafts from 1 - the first is published as `drafts/harvest_restrictions_v2026-10-draft1.zip` in the bucket, with summaries comparing its area totals to the most recent release. It refuses unless the local draft is of an `overlay` run on the checked-out commit with no uncommitted changes - a published draft is exactly that commit's output. Uncommitted changes to documentation (`*.md`, `CHANGES.txt`, `LICENSE`) are allowed in the local draft, changes to any other tracked file are not.

    To run the [harvest-restrictions workflow](https://github.com/bcgov/harvest-restrictions/actions/workflows/harvest-restrictions.yaml) instead, give it the version - it publishes a draft when given one.

9. Review the published draft's archive - it's exactly what the release will be:

    - `harvest_restrictions.gpkg`
    - `harvest_restrictions_sources.gpkg`
    - `land_designations_summary.csv`
    - `harvest_restrictions_summary.csv`
    - `sources.csv`
    - `overlay_run.json` - the commit the draft was run on

    If results are not correct, address the issue, commit the fix, re-run from step 3 (presuming the issue is with sources/data), and publish the next draft (`publish-draft v2026-10` again publishes `v2026-10-draft2`). Re-publishing the same commit (eg after source data changes) is just another `publish-draft`.

10. Once a draft is approved, run the [Release workflow](https://github.com/bcgov/harvest-restrictions/actions/workflows/release.yaml) with the draft (eg `v2026-10-draft2`). Once approved, it tags the draft's commit as the release (`v2026-10`), pushes the tag, and publishes the draft: its files under `latest/`, its area totals added to the change logs, and its archive copied to `releases/`. Nothing is re-run or rebuilt. Once the release is published, `release` deletes the version's drafts, and the workflow builds the release's Docker images.

    `release` can also be run locally - it needs permission to push tags to the repository, and has none of the workflow's approval, or its guard against two releases at once:

        docker compose run -it --rm runner python harvest_restrictions.py release v2026-10-draft2 -v


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
├── drafts/                                   # published drafts, written by `publish-draft`
│   └── harvest_restrictions_<version>-draft<n>.zip   # as releases/, never overwritten
├── latest/                                   # the latest release's files, written by `release`
│   ├── harvest_restrictions.gpkg
│   ├── harvest_restrictions_sources.gpkg
│   ├── land_designations_summary.csv
│   ├── harvest_restrictions_summary.csv
│   ├── sources.csv
│   └── overlay_run.json
├── logs/                                     # change logs, appended to by `release`
│   ├── land_designations.csv
│   └── harvest_restrictions.csv
└── releases/                                 # permanent per-release archives, written by `release`
    └── harvest_restrictions_<release_tag>.zip
        ├── harvest_restrictions.gpkg         (harvest_restrictions layer)
        ├── harvest_restrictions_sources.gpkg (designations layer)
        ├── land_designations_summary.csv
        ├── harvest_restrictions_summary.csv
        ├── sources.csv
        └── overlay_run.json
```

**`cache/`** - written by `cache`, untagged, one geoparquet per source (`hr_<NN>_<alias>.parquet`), overwritten on the next `cache` run for that source. `clear-cache` removes these directly by filename pattern, safe to point at a shared prefix since it only ever touches its own `hr_*.parquet` files.

**`drafts/harvest_restrictions_<version>-draft<n>.zip`** - written by `publish-draft`, one archive per published draft, never overwritten, laid out as a release archive (see `releases/` below). Transient by design - `release` deletes a version's drafts once it's released. The archive holds:

- `harvest_restrictions.gpkg`, `harvest_restrictions_sources.gpkg` - the overlay result (`harvest_restrictions` layer) and its source designations (`designations` layer)
- `land_designations_summary.csv`, `harvest_restrictions_summary.csv` - comparing the *most recent release* against the draft with `current`/`diff`/`pct_diff` columns. Retains every category present in either side - a category new to this run or dropped since the previous release still gets its labels, with `diff`/`pct_diff` left as `NaN` rather than misleadingly implying zero area. This is what you review in step 9 above, and what `release` reads (via the `current` column) to append the release's totals to the change log - and, from the previous release's column name, to refuse a draft compared against an older release than the latest
- `sources.csv` - a flattened `sources.json` as it stood for this run
- `overlay_run.json` - the draft's record of the commit it was run on (see step 7 above) - `release` tags this commit as the release

**`logs/land_designations.csv` / `logs/harvest_restrictions.csv`** - the change logs, written only by `release`, which appends each release's rows. One row per category per release (`release_tag`, category columns, `area_ha`), in release order - each release appends its rows. The released commit is the release tag's, in git. `publish-draft` reads the logs to find the previous release to compare against. If a release fails partway through publishing, its retry replaces any of its rows already logged.

**`latest/`** - `harvest_restrictions.gpkg`, `harvest_restrictions_sources.gpkg`, `land_designations_summary.csv`, `harvest_restrictions_summary.csv`, `sources.csv` and `overlay_run.json`: the latest release's files, written only by `release`, at fixed plain-named keys (no suffix), overwritten on every release. The latest release archive's files, unzipped, for scripts/mapping applications that just want the current release without tracking release tags - point at these instead of the `releases/` archive. Fully redundant with the latest release's archive.

**`releases/harvest_restrictions_<release_tag>.zip`** - written only by `release`, never overwritten, so every past release stays retrievable by tag. Published last, so a version is released once its archive is here - `release` refuses to release it again. A copy of the promoted draft's archive. A single download holding the release - only the archive's name carries the release tag: the same files as `latest/`, ie the two geopackages, the summaries comparing it to the previous release, the run's `sources.csv`, and its `overlay_run.json`. The zip is seek-optimized (`sozip`), so the geopackages can be read in place without downloading or unzipping, e.g. `ogrinfo /vsizip//vsis3/$BUCKET/harvest_restrictions/releases/harvest_restrictions_<release_tag>.zip/harvest_restrictions.gpkg`.

### Local outputs

The only generated files kept locally are the draft, in `drafts/` (git-ignored) - the latest `overlay` run's outputs and `overlay_run.json` (see step 7 above), replaced by each run.

Every published draft is a copy of a local draft, and every release a copy of a published draft, so no other local copies are kept. `publish-draft` builds the archive from the local draft, and `release` downloads the published draft and builds the release, in a temporary directory (`$TMPDIR`) removed once published - set `TMPDIR` if the default temp location is too small for the archive.

### Change log history

The logs were backfilled from pre-existing wide-format records. `v2024-08`, `v2025-04`, `v2025-08`, and `v2026-02` have matching git tags (the latter two were originally recorded under the old `-DRAFT` tag naming convention, since renamed). `v2023-07` and `v2024-04` have no corresponding git tag at all.


## designatedlands

This tool is adapted from the [designated lands script](https://github.com/bcgov/designatedlands). This tool could be used for that analysis by adding mine and oil and gas restriction levels to each source in `sources.json`. Note however that several components of `designatedlands` are not currently supported by this tool:

- raster based analysis
- config based pre-processing of input sources
- adjustment of tiled processing to include the sliver of BC's official boundary not covered by 250k tiles
- overlay of results with arbitrary admin or eco layer