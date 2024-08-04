-- Create a tiling layer.
-- Primary tiles is the 20k bcgs grid, but they don't cover everything, add:
--    - 250k tiles in marine areas
--    - 250m buffer at northern border (60deg)

DROP TABLE IF EXISTS tiles;
CREATE TABLE tiles (tile_id serial primary key, map_tile text, geom geometry);

CREATE INDEX ON tiles (map_tile);
CREATE INDEX tiles_gidx ON tiles USING GIST (geom) ;

-- add 20k tiles
INSERT INTO tiles (map_tile, geom)
SELECT map_tile, geom FROM whse_basemapping.bcgs_20k_grid;

-- Add 250k tiles for marine areas
-- (except for SSOG to avoid confusion with USA border)

INSERT INTO tiles (map_tile, geom)
SELECT a.map_tile||'000',
ST_Multi(ST_CollectionExtract(ST_Difference(a.geom, ST_Union(b.geom)), 3))
FROM whse_basemapping.nts_250k_grid a
INNER JOIN whse_basemapping.bcgs_20k_grid b
ON ST_Intersects(a.geom, b.geom)
WHERE a.map_tile IN
  ('092E','092C','102I','102O','102P','103A','103B',
   '103F','103G', '103H', '103C','103J','103K')
GROUP BY a.map_tile, a.geom;

-- Add 250m buffer along 60deg north to include all of the official bc boundary
-- first - grab 20k tiles along border by id
WITH north_tiles AS
(SELECT ST_Union(geom) as geom
FROM whse_basemapping.bcgs_20k_grid
WHERE (map_tile LIKE '114O%%'
   OR map_tile LIKE '114P%%'
   OR map_tile LIKE '104M%%'
   OR map_tile LIKE '104N%%'
   OR map_tile LIKE '104O%%'
   OR map_tile LIKE '104P%%'
   OR map_tile LIKE '094M%%'
   OR map_tile LIKE '094N%%'
   OR map_tile LIKE '094O%%'
   OR map_tile LIKE '094P%%')
  AND (substring(map_tile from 5 for 2) = '09' OR
       substring(map_tile from 5 for 3) = '100')
  AND (substring(map_tile from 5 for 3) != '090')
  ),

-- then buffer the aggregated tiles by 250m
buff AS
(SELECT
  ST_Buffer(geom, 250) as geom
FROM north_tiles),

-- finally, insert the difference of the buffer and exising tiles (use 250k
-- for speed) into our tile layer
diff as (
SELECT st_union(geom) as geom FROM tiles_250k
WHERE (map_tile LIKE '114O%%'
   OR map_tile LIKE '114P%%'
   OR map_tile LIKE '104M%%'
   OR map_tile LIKE '104N%%'
   OR map_tile LIKE '104O%%'
   OR map_tile LIKE '104P%%'
   OR map_tile LIKE '094M%%'
   OR map_tile LIKE '094N%%'
   OR map_tile LIKE '094O%%'
   OR map_tile LIKE '094P%%'))

INSERT INTO tiles (map_tile, geom)
SELECT
  '0000000' as map_tile,
  ST_Difference(a.geom, b.geom) as geom
FROM buff a, diff b;