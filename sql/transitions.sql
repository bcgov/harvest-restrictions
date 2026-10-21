-- ------------------
-- ## transitions from the previous release, for one tile
-- ------------------
-- every piece of the tile whose top designation differs between the previous release
-- (harvest_restrictions_previous) and this overlay (harvest_restrictions_land, the published
-- area): previous x current intersections where the designation changed, plus land
-- designated in only one of the two. Overlay outputs are cut by 250k tile, so polygons only
-- overlap others of the same tile

insert into harvest_restrictions_transitions (
  map_tile_250k,
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
  geom
)

with previous as (
  select * from harvest_restrictions_previous where map_tile_250k = :'tile'
),

draft as (
  select * from harvest_restrictions_land where map_tile_250k = :'tile'
),

-- land designated in both, with a different top designation
changed as (
  select
    p.harvest_restriction_class_rank as from_class_rank,
    p.harvest_restriction_class_name as from_class_name,
    p.land_designation_type_rank as from_type_rank,
    p.land_designation_type_code as from_type_code,
    p.land_designation_type_name as from_type_name,
    p.land_designation_name as from_name,
    p.land_designation_primary_key as from_primary_key,
    c.harvest_restriction_class_rank as to_class_rank,
    c.harvest_restriction_class_name as to_class_name,
    c.land_designation_type_rank as to_type_rank,
    c.land_designation_type_code as to_type_code,
    c.land_designation_type_name as to_type_name,
    c.land_designation_name as to_name,
    c.land_designation_primary_key as to_primary_key,
    st_intersection(p.geom, c.geom, .1) as geom
  from previous p
  inner join draft c
  on st_intersects(p.geom, c.geom)
  where (p.land_designation_type_code, p.land_designation_primary_key)
    is distinct from (c.land_designation_type_code, c.land_designation_primary_key)
),

-- land designated in the previous release, but not now
undesignated as (
  select
    p.harvest_restriction_class_rank,
    p.harvest_restriction_class_name,
    p.land_designation_type_rank,
    p.land_designation_type_code,
    p.land_designation_type_name,
    p.land_designation_name,
    p.land_designation_primary_key,
    null::integer,
    null::text,
    null::integer,
    null::text,
    null::text,
    null::text,
    null::text,
    -- all of it, where nothing overlaps (the union of nothing is null)
    coalesce(st_difference(p.geom, c.geom, .1), p.geom)
  from previous p
  left join lateral (
    select st_union(geom) as geom
    from draft c
    where st_intersects(c.geom, p.geom)
  ) c on true
),

-- land designated now, but not in the previous release
designated as (
  select
    null::integer,
    null::text,
    null::integer,
    null::text,
    null::text,
    null::text,
    null::text,
    c.harvest_restriction_class_rank,
    c.harvest_restriction_class_name,
    c.land_designation_type_rank,
    c.land_designation_type_code,
    c.land_designation_type_name,
    c.land_designation_name,
    c.land_designation_primary_key,
    -- all of it, where nothing overlaps (the union of nothing is null)
    coalesce(st_difference(c.geom, p.geom, .1), c.geom)
  from draft c
  left join lateral (
    select st_union(geom) as geom
    from previous p
    where st_intersects(p.geom, c.geom)
  ) p on true
),

-- polygons only - intersections and differences also return the lines and points where
-- polygons touch
pieces as (
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
    st_multi(st_collectionextract(geom, 3)) as geom
  from (
    select * from changed
    union all
    select * from undesignated
    union all
    select * from designated
  ) as all_pieces
)

select
  :'tile' as map_tile_250k,
  *
from pieces
where not st_isempty(geom);
