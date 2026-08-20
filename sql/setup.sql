-- create class lookup
CREATE EXTENSION postgis;

CREATE TABLE harvest_restriction_class_rank_name_xref (harvest_restriction_class_rank integer, harvest_restriction_class_name text); 

INSERT INTO harvest_restriction_class_rank_name_xref 
(harvest_restriction_class_rank, harvest_restriction_class_name) 
VALUES 
(1, 'Protected'), 
(2, 'Prohibited'), 
(3, 'High Restricted'), 
(4, 'Medium Restricted'), 
(5, 'Low Restricted'), 
(6, 'No Special Restriction');

-- create output table
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
  all_land_desig_type_ranks text[],
  all_land_desig_type_codes text[],
  all_land_desig_type_names text[],
  all_land_desig_primary_keys text[],
  all_harv_restrict_class_ranks integer[],
  all_harv_restrict_class_names text[],
  map_tile_250k text,
  geom geometry(MULTIPOLYGON, 3005)
);