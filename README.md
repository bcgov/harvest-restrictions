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


## Usage

1. Edit `sources.json` as required

2. Validate `sources.json`:
	
		python download.py --dry-run -v

3. Download data to file:

		python download.py download -v

4. Load downloaded files to database:

        python download.py cache2pg -v --out_table designations

4. Run overlays, dump results to file, log result summaries to csv:

		./harvest_restrictions.sh

Output files are:

- `harvest_restrictions.gdb.zip`        
- `log_land_designations.csv`
- `log_harvest_restrictions.csv`

## Versioning

Note that logging of results over time is based on the output of `git describe` - for this to function effectively, tag releases with `v<year>_<month>`.
When a new release has been tagged, update the list of releases to track in `log.py`.

## designatedlands

This tool is a stripped down version of the [designated lands script](https://github.com/bcgov/designatedlands) and could be used for that analysis by adding mine and oil and gas restriction levels to each source in `sources.json`. Note however that several components of `designatedlands` are not currently supported by this tool:

- raster based analysis
- config based pre-processing of input sources
- adjustment of tiled processing to include the sliver of BC's official boundary not covered by 250k tiles
- overlay of results with arbitrary admin or eco layer