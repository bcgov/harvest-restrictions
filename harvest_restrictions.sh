#!/bin/bash
set -euxo pipefail

# stop script when sql fails
PSQL="psql $DATABASE_URL -v ON_ERROR_STOP=1"

# load 250k grid
bcdata bc2pg WHSE_BASEMAPPING.NTS_250K_GRID

# create harvest restriction rank-name lookup table
$PSQL -c "DROP TABLE IF EXISTS harvest_restriction_class_rank_name_xref; \
          CREATE TABLE harvest_restriction_class_rank_name_xref (harvest_restriction_class_rank integer, harvest_restriction_class_name text); \
          INSERT INTO harvest_restriction_class_rank_name_xref \
          (harvest_restriction_class_rank, harvest_restriction_class_name) \
          VALUES \
          (1, 'Protected'), \
          (2, 'Prohibited'), \
          (3, 'High Restricted'), \
          (4, 'Medium Restricted'), \
          (5, 'Low Restricted'), \
          (6, 'No Special Restriction')"

# create output table
$PSQL -c "DROP TABLE IF EXISTS harvest_restrictions;
  CREATE TABLE harvest_restrictions (
    harvest_restrictions_id serial primary key,
    land_designation_name text,
    land_designation_type_rank integer,
    land_designation_type_code text,
    land_designation_type_name text,
    land_designation_primary_key text,
    harvest_restriction_class_rank integer,
    harvest_restriction_class_name text,
    all_land_desig_names text[],
    all_land_desig_type_ranks text[],
    all_land_desig_type_codes text[],
    all_land_desig_type_names text[],
    all_land_desig_primary_keys text[],
    all_harv_restrict_class_ranks integer[],
    all_harv_restrict_class_names text[],
    map_tile_250k text,
    geom geometry(MULTIPOLYGON, 3005)
  );"

# run overlay
$PSQL -tXA \
-c "SELECT DISTINCT map_tile
    FROM whse_basemapping.nts_250k_grid
    ORDER BY map_tile" \
    | parallel --tag $PSQL -f sql/overlay.sql -v tile={1}

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
  -sql "select
  harvest_restrictions_id,
  land_designation_name,
  land_designation_type_rank,
  land_designation_type_code,
  land_designation_type_name,
  land_designation_primary_key,
  harvest_restriction_class_rank,
  harvest_restriction_class_name,
  array_to_string(trim_array(all_land_desig_names, 1), ';') as all_land_desig_names,
  array_to_string(trim_array(all_land_desig_type_ranks, 1), ';') as all_land_desig_type_ranks,
  array_to_string(trim_array(all_land_desig_type_codes, 1), ';') as all_land_desig_type_codes,
  array_to_string(trim_array(all_land_desig_type_names, 1), ';') as all_land_desig_type_names,
  array_to_string(trim_array(all_land_desig_primary_keys, 1), ';') as all_land_desig_primary_keys,
  array_to_string(trim_array(all_harv_restrict_class_ranks, 1), ';') as all_harv_restrict_class_ranks,
  array_to_string(trim_array(all_harv_restrict_class_names, 1), ';') as all_harv_restrict_class_names,
  map_tile_250k,
  geom
from harvest_restrictions
where
all_harv_restrict_class_ranks @> ARRAY[6] and
all_harv_restrict_class_ranks != ARRAY[6]"

# zip output
zip -r harvest_restrictions.gdb.zip harvest_restrictions.gdb

# summarize results
$PSQL -f sql/land_designations.sql --csv > current_land_designations.csv
$PSQL -f sql/harvest_restrictions.sql --csv > current_harvest_restrictions.csv

# compare to previous outputs
python log.py -tag $(git describe --tags --abbrev=0)

# post to s3
aws s3 cp harvest_restrictions.gdb.zip s3://$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/harvest_restrictions.gdb.zip
aws s3 cp log_land_designations.csv s3://$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/log_land_designations.csv
aws s3 cp log_harvest_restrictions.csv s3://$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/log_harvest_restrictions.csv