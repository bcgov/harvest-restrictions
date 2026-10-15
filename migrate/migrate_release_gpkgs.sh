#!/usr/bin/env bash
# One-off: repackage releases/harvest_restrictions_<tag>.gpkg (the old layout) as
# releases/harvest_restrictions_<tag>.zip (the new layout).
#
# usage: BUCKET=... migrate/migrate_release_gpkgs.sh
#
# Prints what it would do; set RUN=1 to actually do it. The old geopackages are left in place -
# delete them separately once the new archives are checked, so only the archives remain in
# releases/.
set -euo pipefail
: "${BUCKET:?set BUCKET}"
ROOT="s3://$BUCKET/harvest_restrictions"
RUN=${RUN:-0}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

for gpkg in $(aws s3 ls "$ROOT/releases/" | awk '{print $4}' | grep -E '^harvest_restrictions_.+\.gpkg$' || true); do
    tag=${gpkg#harvest_restrictions_}
    tag=${tag%.gpkg}
    archive="harvest_restrictions_$tag.zip"
    echo "$ROOT/releases/$gpkg -> $ROOT/releases/$archive"
    if [ "$RUN" = 1 ]; then
        aws s3 cp "$ROOT/releases/$gpkg" "$work/$gpkg"
        sozip --overwrite --junk-paths "$work/$archive" "$work/$gpkg"
        aws s3 cp "$work/$archive" "$ROOT/releases/$archive"
        rm "$work/$gpkg" "$work/$archive"
    fi
done
