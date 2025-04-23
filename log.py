import os
import subprocess

import pandas


S3 = "s3://" + os.environ.get("BUCKET") + "/dss_projects_2024/harvest_restrictions"

# current release column header comes from git tag
tag = subprocess.check_output(["git", "describe", "--tags"]).decode("ascii").strip()

# read data
d_log = pandas.read_csv(os.path.join(S3, "log_land_designations.csv"))
d_summary = pandas.read_csv("current_land_designations.csv")
h_log = pandas.read_csv(os.path.join(S3, "log_harvest_restrictions.csv"))
h_summary = pandas.read_csv("current_harvest_restrictions.csv")

# log columns - retain only the categories and area_ha of previous releases
d_columns = [
    "land_designation_type_rank",
    "harvest_restriction_class_rank",
    "harvest_restriction_class_name",
    "land_designation_type_code",
    "land_designation_type_name",
]
h_columns = [
    "harvest_restriction_class_rank",
    "harvest_restriction_class_name",
]

# extract release tags from columns
releases = list(set(d_log.columns).difference(set(d_columns + ["diff", "pct_diff"])))

# strip existing diff columns
d_log = d_log[d_columns + releases]
h_log = h_log[h_columns + releases]

# summary columns - drop everything but keys and current area totals
d_summary = d_summary[["land_designation_type_rank", "area_ha"]]
h_summary = h_summary[["harvest_restriction_class_rank", "area_ha"]]

# join the log to the latest summary
d = d_log.merge(d_summary, how="outer", on="land_designation_type_rank").fillna(0)
h = h_log.merge(h_summary, how="outer", on="harvest_restriction_class_rank").fillna(0)

# use current tag as new column name
d = d.rename(columns={"area_ha": tag})
h = h.rename(columns={"area_ha": tag})

# calculate diff and pct diff
previous_tag = sorted(releases)[-1]
d["diff"] = d[tag] - d[previous_tag]
h["diff"] = h[tag] - h[previous_tag]
d["pct_diff"] = (d["diff"] / d[previous_tag]) * 100
h["pct_diff"] = (h["diff"] / h[previous_tag]) * 100

# clean up
d = d.round({tag: 0, "diff": 0, "pct_diff": 2}).set_index("land_designation_type_rank")

h = h.round({tag: 0, "diff": 0, "pct_diff": 2}).set_index(
    "harvest_restriction_class_rank"
)

# dump results to csv
d.to_csv("log_land_designations.csv")
h.to_csv("log_harvest_restrictions.csv")
