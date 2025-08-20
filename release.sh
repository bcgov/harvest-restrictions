#!/bin/bash
set -euxo pipefail

# compare current output to previous release
python log.py

# create csv listing data sources from the json
echo "index,harvest_restriction,alias,description,source,source_type,layer,query,name_field" > sources.csv
jq -r '
  (.[] | [
    .data.harvest_restriction,
    .alias,
    .description,
    .source,
    .source_type,
    .layer,
    .query,
    .field_mapper.name
  ])
  | @csv
' sources.json | nl -n ln -s "," -w1 >> sources.csv

# post output files to object storage
aws s3 cp harvest_restrictions.gpkg.zip \
  s3://$BUCKET/dss_projects_2025/GeoBC/harvest_restrictions/harvest_restrictions.gpkg.zip

aws s3 cp log_land_designations.csv \
  s3://$BUCKET/dss_projects_2025/GeoBC/harvest_restrictions/log_land_designations.csv

aws s3 cp log_harvest_restrictions.csv \
  s3://$BUCKET/dss_projects_2025/GeoBC/harvest_restrictions/log_harvest_restrictions.csv

aws s3 cp sources.csv \
  s3://$BUCKET/dss_projects_2025/GeoBC/harvest_restrictions/sources.csv
