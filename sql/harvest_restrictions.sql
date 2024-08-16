select
  harvest_restriction_class_rank,
  harvest_restriction_class_name,
  round((sum(st_area(geom)) / 10000)::numeric) as area_ha
from harvest_restrictions
-- extract only land area, and do not include areas with no designation
where
  all_harv_restrict_class_ranks @> ARRAY[6] and
  all_harv_restrict_class_ranks != ARRAY[6]
group by
  harvest_restriction_class_rank,
  harvest_restriction_class_name
order by
  harvest_restriction_class_rank,
  harvest_restriction_class_name;
