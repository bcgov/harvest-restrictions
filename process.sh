#!/bin/bash
set -euxo pipefail

# download 250k grid
bcdata bc2pg WHSE_BASEMAPPING.NTS_250K_GRID

# create output table
psql $DATABASE_URL -c "DROP TABLE IF EXISTS designations;
  CREATE TABLE designations (
    designations_id serial primary key,
    index integer,
    alias text,
    description text,
    primary_key text,
    name text,
    harvest_restriction integer,
    mine_restriction integer,
    og_restriction integer,
    indexes_all text[],
    aliases_all text[],
    descriptions_all text[],
    primary_keys_all text[],
    names_all text[],
    harvest_restrictions_all integer[],
    mine_restrictions_all integer[],
    og_restrictions_all integer[],
    map_tile text,
    geom geometry(MULTIPOLYGON, 3005)
  );"

# run overlay
psql $DATABASE_URL -tXA \
-c "SELECT DISTINCT map_tile
    FROM whse_basemapping.nts_250k_grid
    ORDER BY map_tile" \
    | parallel --tag psql $DATABASE_URL -f sql/overlay.sql -v tile={1}

# dump result to file
ogr2ogr   \
  -f OpenFileGDB \
  harvest_restrictions.gdb \
  "PG:$DATABASE_URL" \
  --config OPENFILEGDB_DEFAULT_STRING_WIDTH 255 \
  -nlt MULTIPOLYGON \
  -nln harvest_restrictions \
  -lco CREATE_SHAPE_AREA_AND_LENGTH_FIELDS=YES \
  -mapfieldtype Integer64=Integer \
  -sql "SELECT designations_id as harvest_restrictions_id,
    lpad(index::text, 2, '0')||alias as harvest_restriction,
    description,
    primary_key,
    name,
    harvest_restriction as harvest_restriction_class,
    case
      when harvest_restriction = 1 then 'Protected'
      when harvest_restriction = 2 then 'Prohibited'
      when harvest_restriction = 3 then 'High Restricted'
      when harvest_restriction = 4 then 'Medium Restricted'
      when harvest_restriction = 5 then 'Low Restricted'
      when harvest_restriction = 6 then 'No Special Restriction'
    end as harvest_restriction_class_desc,
    array_to_string(aliases_all, ';') as harvest_restrictions_all,
    array_to_string(descriptions_all, ';') as descriptions_all,
    array_to_string(primary_keys_all, ';') as primary_keys_all,
    array_to_string(names_all, ';') as names_all,
    array_to_string(harvest_restrictions_all, ';') as harvest_restriction_classes_all,
    map_tile text,
    geom
  from designations
  where harvest_restrictions_all @> ARRAY[6]" # land only

# zip output
zip -r harvest_restrictions.gdb.zip harvest_restrictions.gdb

# summarize results
psql $DATABASE_URL -f sql/summarize.sql --csv > harvest_restrictions_summary.csv

# post to s3
aws s3 cp harvest_restrictions.gdb.zip s3://$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/harvest_restrictions.gdb.zip
aws s3 cp harvest_restrictions_summary.csv s3://$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/harvest_restrictions_summary.csv