select
  harvest_restriction_class_rank,
  harvest_restriction_class_name,
  round((sum(st_area(geom)) / 10000)::numeric) as area_ha
from harvest_restrictions_land
group by
  harvest_restriction_class_rank,
  harvest_restriction_class_name
order by
  harvest_restriction_class_rank,
  harvest_restriction_class_name;
