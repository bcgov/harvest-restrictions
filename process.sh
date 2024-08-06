#!/bin/bash
set -euxo pipefail

# create output table
psql $DATABASE_URL -c "DROP TABLE IF EXISTS designations;
  CREATE TABLE designations (
    designations_id serial primary key,
    alias text,
    description text,
    primary_key text,
    name text,
    harvest_restriction integer,
    mine_restriction integer,
    og_restriction integer,
    all_aliases text[],
    all_descriptions text[],
    all_primary_keys text[],
    all_names text[],
    all_harvest_restrictions integer[],
    all_mine_restrictions integer[],
    all_og_restrictions integer[],
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
    alias as harvest_restriction,
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
    end as harvest_restriction_level,
    array_to_string(all_aliases, ';') as all_harvest_restrictions,
    array_to_string(all_descriptions, ';') as all_descriptions,
    array_to_string(all_primary_keys, ';') as all_primary_keys,
    array_to_string(all_names, ';') as all_names,
    array_to_string(all_harvest_restrictions, ';') as all_harvest_restriction_classes,
    map_tile text,
    geom
  from designations
  where all_harvest_restrictions @> ARRAY[6]" # land only

# report on result
