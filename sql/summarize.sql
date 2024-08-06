with rr as (
  SELECT designations_id as harvest_restrictions_id,
    lpad(index::text, 2, '0')||'_'||alias as harvest_restriction,
    description,
    primary_key,
    name,
    harvest_restriction as harvest_restriction_class,
    case
      when harvest_restriction = 1 then 'Protected'
      when harvest_restriction = 2 then 'Prohibited'
      when harvest_restriction = 3 then 'High Restricted'
      when harvest_restriction = 4 then 'Medium Restricted'
      when harvest_restriction = 5 then 'Low Restricted'
      when harvest_restriction = 6 then 'No Special Restriction'
    end as harvest_restriction_class_desc,
    array_to_string(aliases_all, ';') as harvest_restrictions_all,
    array_to_string(descriptions_all, ';') as descriptions_all,
    array_to_string(primary_keys_all, ';') as primary_keys_all,
    array_to_string(names_all, ';') as names_all,
    array_to_string(harvest_restrictions_all, ';') as harvest_restriction_classes_all,
    map_tile text,
    geom
  from designations
  where harvest_restrictions_all @> ARRAY[6]
)

select
  harvest_restriction_class,
  harvest_restriction_class_desc,
  harvest_restriction,
  description as harvest_restriction_desc,
  round((sum(st_area(geom)) / 10000)::numeric) as area_ha
from rr
group by harvest_restriction, harvest_restriction_class, harvest_restriction_class_desc, harvest_restriction, description
order by harvest_restriction, harvest_restriction_class, harvest_restriction;