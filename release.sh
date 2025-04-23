#!/bin/bash
set -euxo pipefail


# compare current output to previous release
python log.py

# post output files to object storage
aws s3 cp harvest_restrictions.gdb.zip \
  s3://$BUCKET/dss_projects_2024/harvest_restrictions/harvest_restrictions.gdb.zip

aws s3 cp log_land_designations.csv \
  s3://$BUCKET/dss_projects_2024/harvest_restrictions/log_land_designations.csv

aws s3 cp log_harvest_restrictions.csv \
  s3://$BUCKET/dss_projects_2024/harvest_restrictions/log_harvest_restrictions.csv