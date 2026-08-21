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


## Development and testing setup

    git clone git@github.com:bcgov/harvest-restrictions.git
    cd harvest_restrictions
    docker compose build
    docker compose up -d

Committing changes requires `pre-commit` - install via your package manager of choice.

The `harvest_restrictions` object storage bucket must have [versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html) enabled - `overlay` and `release` identify a given run's outputs by tagging the relevant object *version* with the commit hash / release tag, rather than by object key.

## Usage

1. Identify any file based sources for which download cannot be scripted, manually upload file to object storage.

2. If making changes to `harvest_restrictions.py`, test the changes:

        docker compose run -it --rm runner python -m pytest -v -rxXs

3. Edit `sources.json` as required (note that sources will likely be provided as a csv file)

4. Validate `sources.json`:
    
        docker compose run -it --rm runner python harvest_restrictions.py cache --dry_run -v

5. Download all restriction sources listed in `sources.json`, saving to geoparquet (specifying output path):

        docker compose run -it --rm runner python harvest_restrictions.py cache -v -o s3://$BUCKET/harvest_restrictions/restrictions

6. Load restrictions layers from cached geoparquet to postgresql (specifying input path):

        docker compose run -it --rm runner python harvest_restrictions.py load-db -v --out_table designations -p s3://$BUCKET/harvest_restrictions/restrictions

7. Run overlays, dump resulting layer and summaries to geoparquet/csv, and publish these outputs to object storage tagged with the current commit hash. This also compares the new summaries to the most recently released version and writes updated change logs:

        docker compose run -it --rm runner python harvest_restrictions.py overlay -v

8. Review the change logs (and geoparquet spatial output if required):

    - `land_designations_summary.csv`
    - `harvest_restrictions_summary.csv`

    If results are not correct, address the issue, commit the fix, and re-run from step 7.

9. If needed, build a geopackage for external (e.g. client) review before committing to a release. This reads the same commit's already-published output, and has no lasting effect - no release tag, no change to the change log - so it's safe to re-run against draft work as many times as needed:

        docker compose run -it --rm runner python harvest_restrictions.py preview -v

10. Once results are confirmed to be reasonable/correct, tag the commit as a release:

        git tag -a vYYYY-MM -m vYYYY-MM

11. Push the tag - this triggers the [Release workflow](https://github.com/bcgov/harvest-restrictions/actions/workflows/release.yaml), which tags the current commit's already-published outputs (from step 7) with the release, builds/publishes the geopackage deliverables (`harvest_restrictions.gpkg.zip` and `harvest_restrictions_sources.gpkg.zip`), and appends the release to the change log:

        git push origin vYYYY-MM

    Alternatively, run the release step locally instead of pushing the tag:

        docker compose run -it --rm app ./release.sh

12. Optionally, re-run the entire download/process pipeline by manually calling the [harvest-restrictions workflow](https://github.com/bcgov/harvest-restrictions/actions/workflows/harvest-restrictions.yaml).


## Change history and review reports

Two kinds of csv track area totals by category over time, both held in object storage at `s3://$BUCKET/harvest_restrictions/`:

- `land_designations_log.csv` / `harvest_restrictions_log.csv` - the durable log, long/tidy format (one row per category per release: `release_tag`, `release_date`, `commit`, `run_id`, category columns, `area_ha`). Only ever appended to, and only at release time (by `release`) - this is the source of truth for area over time, suited to plotting/analysis across all past releases.
- `land_designations_summary.csv` / `harvest_restrictions_summary.csv` - a small, disposable rollup, rebuilt from scratch on every `overlay` run (by `log`). Summarizes the *most recent release* from the log against the *current* run, with `diff`/`pct_diff` columns - this is what you review in step 8 above before deciding whether to release.

### commit vs run_id

Every object `overlay` publishes is tagged with both `commit` (the git commit that produced it) and `run_id` (a UTC timestamp identifying that specific invocation of `overlay`). These are usually interchangeable - `release`/`preview` default to the most recent run of a given commit - but they diverge if `overlay` is run more than once against the same commit (the underlying source data can change even with no code change). If a second run happens after you've reviewed the first, pass `--run_id` to `release`/`preview` to pin the exact run that was actually reviewed, rather than picking up whatever ran most recently.

The logs were backfilled from pre-existing wide-format records. `v2024-08`, `v2025-04`, and `v2026-02-DRAFT` carry real `commit`/`release_date` values from their matching git tags. `v2023-07`, `v2024-04`, and `v2025-08` have no corresponding git tag (either never tagged, or - for `v2025-08` - the tag on record is named `v2025-08-DRAFT`, not `v2025-08`) - for those three, `release_date` is approximated as the first of the tag's named month and `commit` is left blank.


## designatedlands

This tool is a stripped down version of the [designated lands script](https://github.com/bcgov/designatedlands) and could be used for that analysis by adding mine and oil and gas restriction levels to each source in `sources.json`. Note however that several components of `designatedlands` are not currently supported by this tool:

- raster based analysis
- config based pre-processing of input sources
- adjustment of tiled processing to include the sliver of BC's official boundary not covered by 250k tiles
- overlay of results with arbitrary admin or eco layer