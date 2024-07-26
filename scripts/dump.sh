#!/bin/bash
set -euxo pipefail

# manually compile non-bcgw sources and dump to s3

docker-compose run --rm -v /Users:/Users app \
  ogr2ogr \
  /vsis3/$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/sources/crd_water_supply_area.parquet \
  $PWD/CRD.gdb.zip \
  WSA_Boundary

docker-compose run --rm -v /Users:/Users app \
  ogr2ogr \
  /vsis3/$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/sources/gvrd_water_supply_area.parquet \
  $PWD/GVRD_watershed.gdb.zip \
  GVRD_watershed

# for CPCAD, don't write unnecessary columns due to this error writing to parquet
# ERROR 1: WriteColumnChunk() failed for field PA_OECM_DF: Writing DictionaryArray with null encoded in dictionary type not yet supported
docker-compose run --rm -v /Users:/Users app \
  ogr2ogr \
  /vsis3/$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/sources/migratory_bird_sanctuaries.parquet \
  $PWD/CPCAD-BDCAPC_Nov2023.gdb.zip \
  CPCAD_Nov2023_Albers \
  -where "LOC = 2 And BIOME = 'T' And TYPE_E = 'Migratory Bird Sanctuary'" \
  -select PARENT_ID

docker-compose run --rm -v /Users:/Users app \
  ogr2ogr \
  /vsis3/$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/sources/national_wildlife_area.parquet \
  $PWD/CPCAD-BDCAPC_Nov2023.gdb.zip \
  CPCAD_Nov2023_Albers \
  -where "LOC = 2 AND BIOME = 'T' AND TYPE_E = 'National Wildlife Area'" \
  -select PARENT_ID

docker-compose run --rm -v /Users:/Users app \
  ogr2ogr \
  /vsis3/$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/sources/great_bear_fisheries_watersheds.parquet \
  $PWD/GBRO_Schedules_20160120.gdb.zip \
  GBRSchE_IFW_20160104

docker-compose run --rm -v /Users:/Users app \
  ogr2ogr \
  /vsis3/$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/sources/great_bear_grizzly_class2.parquet \
  $PWD/GBRO_Schedules_20160120.gdb.zip \
  GBRSchD_GB_20151105 \
  -where "GB_CLASS = 2"

docker-compose run --rm -v /Users:/Users app \
  ogr2ogr \
  /vsis3/$OBJECTSTORE_BUCKET/dss_projects_2024/harvest_restrictions/sources/no_special_restriction.parquet \
  $PWD/BC_Boundary_Terrestrial.gpkg.zip \
  BC_Boundary_Terrestrial_Singlepart
