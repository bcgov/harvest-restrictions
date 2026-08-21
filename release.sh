#!/bin/bash
set -euxo pipefail

# tag this commit's already-published overlay outputs as a release, and
# publish the geopackage deliverable (only built at release time)
python harvest_restrictions.py release -v

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

# publish sources.csv, tagged with this commit and release
COMMIT=$(git rev-parse HEAD)
RELEASE_TAG=$(git describe --tags --exact-match)
aws s3 cp sources.csv s3://$BUCKET/harvest_restrictions/sources.csv
aws s3api put-object-tagging \
  --bucket $BUCKET \
  --key harvest_restrictions/sources.csv \
  --tagging "TagSet=[{Key=commit,Value=$COMMIT},{Key=release,Value=$RELEASE_TAG}]"
