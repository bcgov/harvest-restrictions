--------------------
-- ## clean/subdivide geoms
--------------------
create temporary table cleaned as

with tile as (
  select
    t.map_tile,
    s.index,
    s.alias,
    s.description,
    s.primary_key,
    s.name,
    s.harvest_restriction,
    CASE
      WHEN ST_CoveredBy(s.geom, t.geom) THEN st_makevalid(s.geom)
      ELSE st_makevalid((ST_Intersection(s.geom, t.geom, .1)))
    END as geom
  from designations s
  inner join whse_basemapping.nts_250k_grid t
  on st_intersects(s.geom, t.geom)
  where map_tile = :'tile'
)

-- dump and subdivide
select * from (
select
  map_tile,
  index,
  alias,
  description,
  primary_key,
  name,
  harvest_restriction,
  st_makevalid(st_subdivide((st_dump(geom)).geom)) as geom
from tile
) as a where st_dimension(geom) = 2 ;  -- do not include any line/point artifacts created by intersect

create index on cleaned using gist (geom);


--------------------
-- ## run the overlay
--------------------

-- dump poly rings and convert to lines
with rings as
(
  SELECT
    map_tile,
    ST_Exteriorring((ST_DumpRings(geom)).geom) AS geom
  FROM cleaned
),

-- node the lines with st_union and dump to singlepart lines
lines as
(
  SELECT
    map_tile,
    (st_dump(st_union(geom, .1))).geom as geom
  FROM rings
  GROUP BY map_tile
),

-- polygonize the resulting noded lines
flattened AS
(
  SELECT
    map_tile,
    (ST_Dump(ST_Polygonize(geom))).geom AS geom
  FROM lines
  GROUP BY map_tile
),

-- get the attributes and sort by index
sorted AS
(
  SELECT
    p.index,
    p.alias,
    p.description,
    p.primary_key,
    p.name,
    COALESCE(p.harvest_restriction, 0) as harvest_restriction,
    hrn.harvest_restriction_class_name,
    f.map_tile,
    f.geom
  FROM flattened f
  LEFT OUTER JOIN cleaned p
  ON ST_Contains(p.geom, ST_PointOnSurface(f.geom))
  INNER JOIN harvest_restriction_class_rank_name_xref hrn on p.harvest_restriction = hrn.harvest_restriction_class_rank
  WHERE p.index is not null
  ORDER BY p.index, p.primary_key
),

aggregated as (
  SELECT
    map_tile,
    array_agg(index ORDER BY index) as indexes_all,
    array_agg(alias ORDER BY index) as aliases_all,
    array_agg(description ORDER BY index) as descriptions_all,
    array_agg(primary_key ORDER BY index) as primary_keys_all,
    array_agg(name ORDER BY index) as names_all,
    array_agg(harvest_restriction ORDER BY index) as harvest_restrictions_all,
    array_agg(harvest_restriction_class_name ORDER BY index) as harvest_restriction_class_names_all,
    geom
  FROM sorted
  GROUP BY map_tile, geom
)

INSERT INTO harvest_restrictions (
    land_designation_name,
    land_designation_type_rank,
    land_designation_type_code,
    land_designation_type_name,
    land_designation_primary_key,
    harvest_restriction_class_rank,
    harvest_restriction_class_name,
    all_land_desig_names,
    all_land_desig_type_ranks,
    all_land_desig_type_codes,
    all_land_desig_type_names,
    all_land_desig_primary_keys,
    all_harv_restrict_class_ranks,
    all_harv_restrict_class_names,
    map_tile_250k,
    geom
)
SELECT
  names_all[1] as land_designation_name,
  indexes_all[1] as land_designation_type_rank,
  aliases_all[1] as land_designation_type_code,
  descriptions_all[1] as land_designation_type_name,
  primary_keys_all[1] as land_designation_primary_key,
  harvest_restrictions_all[1] as harvest_restriction_class_rank,
  harvest_restriction_class_names_all[1] as harvest_restriction_class_name,
  names_all as all_land_desig_names,
  indexes_all as all_land_desig_type_ranks,
  aliases_all as all_land_desig_type_codes,
  descriptions_all as all_land_desig_type_names,
  primary_keys_all as all_land_desig_primary_keys,
  harvest_restrictions_all as all_harv_restrict_class_ranks,
  harvest_restriction_class_names_all as all_harv_restrict_class_names,
  map_tile as map_tile_250k,
  st_union(geom, .1) as geom
from aggregated
group by
  names_all,
  indexes_all,
  aliases_all,
  descriptions_all,
  primary_keys_all,
  harvest_restrictions_all,
  harvest_restriction_class_names_all,
  map_tile;