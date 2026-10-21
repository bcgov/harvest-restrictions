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
| `field_mapper`| Mapping of source column names to new column names   |
| `data`       | Additional data to add to all records of the source, as key(column):value pair |

The `harvest_restriction` key in the `data` field defines the level of restriction of the given source, where restrictions are:

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
| `all_land_desig_primary_keys`     | String  | Primary key values for all designations that apply to the given polygon |   
| `all_harv_restrict_class_ranks`   | String  | All harvest restriction class ranks that apply to the given polygon |   
| `all_harv_restrict_class_names`   | String  | All harvest restriction class names that apply to the given polygon |
| `map_tile_250k`                   | String  | NTS 1:250,000 tile name                                          |   

Note that the `all_*` columns are JSON arrays listing every designation applying to the polygon, in rank order.
The nth entry of each describes the same designation, and a missing value is `null` (eg a designation with no name or primary key).

Each release also includes `harvest_restrictions_sources.gpkg` (layer `designations`) - every source designation as loaded for the overlay.

And `harvest_restrictions_changes.csv` - where the top designation differs from the previous release's, the area (ha) of each change, from the previous release's designation (`from_*` columns) to the release's (`to_*`). Land designated in only one of the two has empty `from_*` (newly designated) or `to_*` (no longer designated) columns. Changes rounding to 0 ha are left out.


## Development and testing setup

    git clone git@github.com:bcgov/harvest-restrictions.git
    cd harvest-restrictions
    docker compose build
    docker compose up -d

Committing changes requires `pre-commit` - install via your package manager of choice.

The database is set up by `sql/setup.sql` when its container is first created. Any changes to `setup.sql` require rebuilding the db image and recreating the database.

## Usage

1. Identify any file based sources for which download cannot be scripted, manually upload file to object storage with the `/harvest_restrictions/sources` prefix.

2. If making changes to `harvest_restrictions.py`, test the changes:

        docker compose run -it --rm runner python -m pytest -v -rxXs

3. Edit `sources.json` as required (note that sources will likely be provided as a csv file)

4. Validate `sources.json`:
    
        docker compose run -it --rm runner python harvest_restrictions.py cache --dry_run -v

5. Download all restriction sources listed in `sources.json`, saving to standard schema in geoparquet format. Defaults to writing to object storage `/harvest_restrictions/cache` if `$BUCKET` is set, pass `--out_path` to write elsewhere:

        docker compose run -it --rm runner python harvest_restrictions.py cache -v

    Optionally, clear the cache first. While `cache` overwrites the files for sources it downloads, it does not remove anything for sources since removed or renamed. These should generally be cleaned up, `clear-cache` makes it simple, removing just the `hr_*.parquet` files written by `cache` itself:

        docker compose run -it --rm runner python harvest_restrictions.py clear-cache -v

6. Load restrictions layers from the cached geoparquet to postgresql. `--out_table` appends, so pass `--truncate` when re-running against a table already loaded (e.g. re-running this step after a partial failure) to avoid duplicating rows:

        docker compose run -it --rm runner python harvest_restrictions.py load-db -v --out_table designations --truncate

7. Run overlays in postgres and dump results to `drafts/`:

        docker compose run -it --rm runner python harvest_restrictions.py overlay -v

    With `$BUCKET` set, the results are compared to the latest release (if it has an archive in `releases/` - releases before `v2026-02` don't). Without `$BUCKET`, or for a release without an archive, a warning is logged and the comparison skipped - and the draft can't be published.

    Each run overwrites the contents of `drafts/`:

    - `harvest_restrictions.gpkg` - output harvest restrictions layer
    - `harvest_restrictions_sources.gpkg` - source data combined into a single layer
    - `sources.csv` - `sources.json` translated to csv
    - `land_designations.csv` - area totals for source designations
    - `harvest_restrictions.csv` - area totals for output harvest restrictions after overlaps removed
    - `harvest_restrictions_changes.csv` - area of each change in top designation from the latest release
    - `harvest_restrictions_transitions.gpkg` - the geometries of those changes (layer `transitions`), for review - not published
    - `overlay_run.json` - logs what code the draft was run on, and the release it was compared to, written when `overlay` finishes:

            {
              "commit": "f439b88c2d1e7a4b9f0c3e5a6b8d2f1e4c7a9b03",
              "uncommitted_changes": [" M sql/overlay.sql"],
              "previous_release": "v2026-02",
              "finished_at": "2026-10-19T21:02:11+00:00"
            }

      `uncommitted_changes` lists the `git status` lines of changed tracked files (other than documentation). If there are uncommitted changes noted, the next step `publish-draft` will fail - commit changes and re-run the overlay until results are satisfactory. `publish-draft` also fails if `previous_release` isn't the latest release (eg a release was made since the overlay ran, or it wasn't compared) - re-run the overlay.

8. Once outputs in `drafts/` are satisfactory, combine the local draft files noted above into a single archive and publish to object storage `harvest_restrictions/drafts` for review, assigning the draft a tag in format `vYYYY-MM` or, if necessary `vYYYY-MM-DD`:

        docker compose run -it --rm runner python harvest_restrictions.py publish-draft v2026-10 -v

    `publish-draft` numbers the version's drafts from 1 - the first is published as `drafts/harvest_restrictions_v2026-10-draft1.zip` in the bucket, with summaries comparing its area totals to the most recent release.

9. Provide the published draft's archive to reviewers - it's exactly what the release will be:

    - `harvest_restrictions.gpkg`
    - `harvest_restrictions_sources.gpkg`
    - `land_designations_summary.csv`
    - `harvest_restrictions_summary.csv`
    - `harvest_restrictions_changes.csv`
    - `sources.csv`
    - `overlay_run.json` - the commit the draft was run on, and the release it was compared to

    If results are not correct, address the issue, commit the fix, re-run from step 3 (presuming the issue is with the sources), and publish a new draft with the same command and tag (the script auto increments the draft numbers).

10. Once a draft is approved, release it:

        docker compose run -it --rm runner python harvest_restrictions.py release v2026-10-draft2 -v

    This can also be run on GitHub by triggering the [Release workflow](https://github.com/bcgov/harvest-restrictions/actions/workflows/release.yaml) and entering the draft tag to release when prompted. The release writes the archive to `harvest_restrictions/releases/harvest_restrictions_<release_tag>.zip` and also writes the various files to `/latest` and logs the area totals in `/logs/*.csv`

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
│   ├── harvest_restrictions_changes.csv
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
        ├── harvest_restrictions_changes.csv
        ├── sources.csv
        └── overlay_run.json
```

## Change log history

Note that the logs were backfilled from pre-existing summaries. `v2024-08`, `v2025-04`, `v2025-08`, and `v2026-02` have git tags but older releases `v2023-07` and `v2024-04` have no corresponding tag in the git repository.

## Re-running a past release

As of `v2026-08` all releases are retained and a release can be recreated using exactly the same data and tools. Starting from scratch with a hypothetical `v2026-10` release:

    git clone git@github.com:bcgov/harvest-restrictions.git
    cd harvest-restrictions
    # check out the release code
    git checkout v2026-10
    # use the release's images, built by the release workflow - tagged with the names docker
    # compose gives the images it builds, so it uses these rather than building its own
    for image in db runner; do
      docker pull ghcr.io/bcgov/harvest-restrictions-$image:v2026-10
      docker tag ghcr.io/bcgov/harvest-restrictions-$image:v2026-10 harvest-restrictions-$image
    done
    docker compose up -d
    # load data from the release archive
    docker compose run -it --rm runner sh -c 'ogr2ogr -f PostgreSQL "PG:$DATABASE_URL" -append -nln designations /vsizip//vsis3/$BUCKET/harvest_restrictions/releases/harvest_restrictions_v2026-10.zip/harvest_restrictions_sources.gpkg designations'
    # run the overlays, writing output to the local draft folder, drafts/
    docker compose run -it --rm runner python harvest_restrictions.py overlay -v

## designatedlands

This tool is adapted from the [designated lands script](https://github.com/bcgov/designatedlands). This tool could be used for that analysis by adding mine and oil and gas restriction levels to each source in `sources.json`. Note however that several components of `designatedlands` are not currently supported by this tool:

- raster based analysis
- config based pre-processing of input sources
- adjustment of tiled processing to include the sliver of BC's official boundary not covered by 250k tiles
- overlay of results with arbitrary admin or eco layer