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
    s.og_restriction,
    s.mine_restriction,
    CASE
      WHEN ST_CoveredBy(s.geom, t.geom) THEN st_makevalid(s.geom)
      ELSE st_makevalid((ST_Intersection(s.geom, t.geom, .1)))
    END as geom
  from designations_source s
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
  og_restriction,
  mine_restriction,
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
    COALESCE(p.mine_restriction, 0) as mine_restriction,
    COALESCE(p.og_restriction, 0) as og_restriction,
    f.map_tile,
    f.geom
  FROM flattened f
  LEFT OUTER JOIN cleaned p
  ON ST_Contains(p.geom, ST_PointOnSurface(f.geom))
  WHERE p.index is not null
  ORDER BY p.index, p.primary_key
),

aggregated as (
  SELECT
    map_tile,
    array_agg(index ORDER BY index) as all_indexes,
    array_agg(alias ORDER BY index) as all_aliases,
    array_agg(description ORDER BY index) as all_descriptions,
    array_agg(primary_key ORDER BY index) as all_primary_keys,
    array_agg(name ORDER BY index) as all_names,
    array_agg(harvest_restriction ORDER BY index) as all_harvest_restrictions,
    array_agg(mine_restriction ORDER BY index) as all_mine_restrictions,
    array_agg(og_restriction ORDER BY index) as all_og_restrictions,
    geom
  FROM sorted
  GROUP BY map_tile, geom
)

INSERT INTO designations (
  alias,
  description,
  primary_key,
  name,
  harvest_restriction,
  mine_restriction,
  og_restriction,
  all_indexes,
  all_aliases,
  all_descriptions,
  all_primary_keys,
  all_names,
  all_harvest_restrictions,
  all_mine_restrictions,
  all_og_restrictions,
  map_tile,
  geom
)
SELECT
  all_aliases[1] as alias,
  all_descriptions[1] as description,
  all_primary_keys[1] as primary_key,
  all_names[1] as name,
  all_harvest_restrictions[1] as harvest_restriction,
  all_mine_restrictions[1] as mine_restriction,
  all_og_restrictions[1] as og_restriction,
  all_indexes,
  all_aliases,
  all_descriptions,
  all_primary_keys,
  all_names,
  all_harvest_restrictions,
  all_mine_restrictions,
  all_og_restrictions,
  map_tile,
  st_union(geom, .1) as geom
from aggregated
group by
  all_indexes,
  all_aliases,
  all_descriptions,
  all_primary_keys,
  all_names,
  all_harvest_restrictions,
  all_mine_restrictions,
  all_og_restrictions,
  map_tile;