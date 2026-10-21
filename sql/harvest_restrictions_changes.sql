-- area of each transition from the previous release's top designation to this overlay's,
-- by designation - transitions rounding to 0 ha (slivers from noding) are left out
select
  from_class_rank,
  from_class_name,
  from_type_rank,
  from_type_code,
  from_type_name,
  from_name,
  from_primary_key,
  to_class_rank,
  to_class_name,
  to_type_rank,
  to_type_code,
  to_type_name,
  to_name,
  to_primary_key,
  round((sum(st_area(geom)) / 10000)::numeric) as area_ha
from harvest_restrictions_transitions
group by
  from_class_rank,
  from_class_name,
  from_type_rank,
  from_type_code,
  from_type_name,
  from_name,
  from_primary_key,
  to_class_rank,
  to_class_name,
  to_type_rank,
  to_type_code,
  to_type_name,
  to_name,
  to_primary_key
having round((sum(st_area(geom)) / 10000)::numeric) > 0
order by
  area_ha desc;
