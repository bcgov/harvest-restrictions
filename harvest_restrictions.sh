#!/bin/bash
set -euxo pipefail

# stop script when sql fails
PSQL="psql $DATABASE_URL -v ON_ERROR_STOP=1"

# load 250k grid
bcdata bc2pg WHSE_BASEMAPPING.NTS_250K_GRID

# run overlays in parallel per tile
$PSQL -tXA \
-c "SELECT DISTINCT map_tile
    FROM whse_basemapping.nts_250k_grid
    ORDER BY map_tile" \
    | parallel --tag $PSQL -f sql/overlay.sql -v tile={1}

# dump result to file
ogr2ogr   \
  -f GPKG \
  harvest_restrictions.gpkg.zip \
  "PG:$DATABASE_URL" \
  -nlt MULTIPOLYGON \
  -nln harvest_restrictions \
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

# summarize results
$PSQL -f sql/land_designations.sql --csv > current_land_designations.csv
$PSQL -f sql/harvest_restrictions.sql --csv > current_harvest_restrictions.csv
