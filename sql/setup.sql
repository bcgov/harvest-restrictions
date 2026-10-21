CREATE EXTENSION postgis;

-- restriction type lookup

CREATE TABLE harvest_restriction_class_rank_name_xref (
  harvest_restriction_class_rank integer,
  harvest_restriction_class_name text
);

INSERT INTO harvest_restriction_class_rank_name_xref 
(harvest_restriction_class_rank, harvest_restriction_class_name) 
VALUES 
(1, 'Protected'), 
(2, 'Prohibited'), 
(3, 'High Restricted'), 
(4, 'Medium Restricted'), 
(5, 'Low Restricted'), 
(6, 'No Special Restriction');


--
-- output tables
--

-- designations (all input designation sources loaded to a single table)
CREATE TABLE designations (
 index                integer,
 description          text,
 alias                text,
 primary_key          text,
 name                 text,
 harvest_restriction  integer,
 geom                 geometry(MultiPolygon,3005)
);
CREATE INDEX on designations USING gist (geom);

-- overlay/overlap processing output table
CREATE TABLE harvest_restrictions (
  harvest_restrictions_id serial primary key,
  land_designation_name text,
  land_designation_type_rank integer,
  land_designation_type_code text,
  land_designation_type_name text,
  land_designation_primary_key text,
  harvest_restriction_class_rank integer,
  harvest_restriction_class_name text,
  all_land_desig_names text[],
  all_land_desig_type_ranks integer[],
  all_land_desig_type_codes text[],
  all_land_desig_type_names text[],
  all_land_desig_primary_keys text[],
  all_harv_restrict_class_ranks integer[],
  all_harv_restrict_class_names text[],
  map_tile_250k text,
  geom geometry(MULTIPOLYGON, 3005)
);
-- for comparing to the previous release, tile by tile
CREATE INDEX ON harvest_restrictions (map_tile_250k);
CREATE INDEX ON harvest_restrictions USING gist (geom);

-- the overlay output that is land (within the class 6 land base) and has a designation -
-- the area that is published, and totalled in the change logs
CREATE VIEW harvest_restrictions_land AS
SELECT *
FROM harvest_restrictions
WHERE
  all_harv_restrict_class_ranks @> ARRAY[6] AND
  all_harv_restrict_class_ranks != ARRAY[6];

-- the previous release's output (its published harvest_restrictions layer), loaded by
-- overlay to compare this overlay against - columns as published, the all_* lists as json
CREATE TABLE harvest_restrictions_previous (
  harvest_restrictions_id integer,
  land_designation_name text,
  land_designation_type_rank integer,
  land_designation_type_code text,
  land_designation_type_name text,
  land_designation_primary_key text,
  harvest_restriction_class_rank integer,
  harvest_restriction_class_name text,
  all_land_desig_names jsonb,
  all_land_desig_type_ranks jsonb,
  all_land_desig_type_codes jsonb,
  all_land_desig_type_names jsonb,
  all_land_desig_primary_keys jsonb,
  all_harv_restrict_class_ranks jsonb,
  all_harv_restrict_class_names jsonb,
  map_tile_250k text,
  geom geometry(MULTIPOLYGON, 3005)
);
CREATE INDEX ON harvest_restrictions_previous (map_tile_250k);
CREATE INDEX ON harvest_restrictions_previous USING gist (geom);

-- land whose top designation differs between the previous release and this overlay, from
-- its designation in the previous release to its designation now. Land designated in only
-- one of the two has null from_* (newly designated) or to_* (no longer designated)
CREATE TABLE harvest_restrictions_transitions (
  map_tile_250k text,
  from_class_rank integer,
  from_class_name text,
  from_type_rank integer,
  from_type_code text,
  from_type_name text,
  from_name text,
  from_primary_key text,
  to_class_rank integer,
  to_class_name text,
  to_type_rank integer,
  to_type_code text,
  to_type_name text,
  to_name text,
  to_primary_key text,
  geom geometry(MULTIPOLYGON, 3005)
);
