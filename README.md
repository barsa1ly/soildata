# soildata

A static copy of the USDA-NRCS **Soil Survey Geographic Database (SSURGO) for Texas**, fiscal year 2027 (published by
NRCS on 1 October 2026; survey areas dated 3 to 10 September 2026). SSURGO is public domain.

The SoilGuard and FieldGuard apps read it as a **backup**: they ask USDA's Soil Data Access (SDA) for the soil at a
point first, and read these files only when SDA does not answer. The copy holds what the apps ask SDA for (their
`js/soil.js`, `batch()` and `tablesAt()`): the map unit at the point, and that map unit's rows of eight tables, with the
same columns, in the same order, and every value a string, as SDA's JSON gives them.

## Layout

```
TX/meta.json              source, fiscal year, survey dates, counts, build date, tool
TX/mukeys.json            [null, 58362, 58384, ...]: raster index -> mukey (index 0 = no map unit)
TX/mu/<k>.json.gz         map unit records, gzipped, k = mukey % 1024 (1024 files, about 17 kB each, 13 map units)
TX/r30/index.json         the 30 m raster's tile grid
TX/r30/<row>/<col>.bin    raster tiles, 512 x 512 cells (3,068 tiles; a tile with no map unit is not written)
tools/build_ssurgo.py     builds a state's copy from NRCS's SQLite SSURGO download
tools/lookup_point.py     reads the copy at a point (a reference reader, used for the checks below)
```

### Map unit records (`TX/mu/<k>.json.gz`, gzip: inflate, then parse as JSON)

```
{"<mukey>": {"mukey": 391100, "mu": {...}, "comps": [...], "hz": [...], "areas": [...],
             "eco": [...], "ylds": [...], "plants": [...], "restr": [...]}, ...}
```

The shape `tablesAt()` returns: `mukey` a number, `mu` one row object, the rest arrays of row objects whose keys are
the columns of the app's SELECTs with the table prefix dropped (`mu.mukey` is `mukey`, `tg.texture` is `texture`).

| key | rows | order |
|---|---|---|
| `mu` | `mapunit` with its `legend`, the survey's date (`sacatalog.saverest`) and `muaggatt` | one row |
| `comps` | `component` | `comppct_r` descending, ties by `cokey` |
| `hz` | `chorizon` with its representative texture group (`chtexturegrp`, `rvindicator = 'Yes'`), every component's | `cokey`, then `hzdept_r` |
| `areas` | `muaoverlap` with `laoverlap` (counties, MLRA, USLE climate and rainfall factor areas, ...) | `areaovacres` descending |
| `eco`, `ylds`, `plants`, `restr` | `coecoclass`, `cocropyld`, `coeplants`, `corestrictions` | `cokey`, then the table's own key |

Values are strings the way SDA writes them: whole numbers without `.0` (`"9"`, `"85"`), other numbers as written
(`"0.3"`, `"617.3"`), text as it is (`".37"`, `"Loamy fine sand"`), the survey date as `"9/3/2026 1:37:01 PM"`;
`null` where there is no value. Two horizon columns the apps ask SDA for are not in NRCS's download and are `null`
for every horizon: `fragvoltot_r` (total rock fragment volume) and `esp_r` (exchangeable sodium percentage).

All 13,122 Texas map units have a record (41,268 components, 65,610 horizons).

### Map unit raster (`TX/r30/`)

`index.json`:

```
{"crs": "EPSG:5070", "res": 30, "tile": 512, "x0": -1013760, "y0": 1520640, "rows": 79, "cols": 82,
 "have": ["0/8", "0/9", ...], "format": "...", "resample": "mode"}
```

* The grid is NAD83 / Conus Albers (EPSG:5070, equal area, as NRCS's raster), 30 m cells, in tiles of 512 x 512 cells
  (15,360 m) whose origin (`x0`, `y0`: the left edge of tile column 0 and the top edge of tile row 0) is a multiple of
  15,360 m. `have` lists the tiles written, as `"row/col"`.
* Each `.bin` holds 512 x 512 unsigned 16-bit little-endian integers, row by row from the north-west corner,
  compressed with zlib (what `DecompressionStream("deflate")` reads). A value is an index into `mukeys.json`; 0 means
  no map unit.
* The 30 m cells come from NRCS's 10 m raster (`muraster_10m_TX.tif`): each cell takes the 9 pixels whose centres lie
  in it and keeps the map unit that covers most of them (pixels with no map unit do not count, so a cell is empty only
  when all 81 are; a tie goes to the centre pixel's map unit). At 3,000 random points in Texas the copy gives the 10 m
  raster's map unit at 89.5% of them (over all of Texas's 10 m pixels, 91.0%); 13,115 of the 13,122 map units are big
  enough to appear in it.

## Looking up a point

1. Project longitude and latitude to EPSG:5070 (GRS 1980; standard parallels 29.5° N and 45.5° N; origin 23° N,
   96° W; no false easting or northing). Coordinates in WGS 84 can be used as they are (NAD83 differs by about a metre).
2. The tile is column `floor((x - x0) / (res * tile))`, row `floor((y0 - y) / (res * tile))`; if `"row/col"` is not in
   `have`, there is no map unit at the point.
3. The cell is column `floor((x - x0) / res)` and row `floor((y0 - y) / res)`; inside the tile it is at
   `(row mod 512) * 512 + (col mod 512)`, two bytes each.
4. `mukeys.json[index]` is the mukey (`null` for index 0), and `mu/<mukey % 1024>.json.gz` (gzip) holds its record under
   `"<mukey>"`.

In JavaScript (`base` is the state folder, such as `https://barsa1ly.github.io/soildata/TX`):

```js
// longitude, latitude (degrees) -> EPSG:5070 x, y (m): Snyder (1987), Map Projections: A Working Manual, eqs. 14-1 to 14-6
function albers5070(lon, lat) {
  const a = 6378137, f = 1 / 298.257222101, e2 = f * (2 - f), e = Math.sqrt(e2), r = Math.PI / 180;
  const m = p => Math.cos(p) / Math.sqrt(1 - e2 * Math.sin(p) ** 2);
  const q = p => { const s = Math.sin(p); return (1 - e2) * (s / (1 - e2 * s * s) - Math.log((1 - e * s) / (1 + e * s)) / (2 * e)); };
  const p1 = 29.5 * r, p2 = 45.5 * r, n = (m(p1) ** 2 - m(p2) ** 2) / (q(p2) - q(p1)), C = m(p1) ** 2 + n * q(p1);
  const rho = a * Math.sqrt(C - n * q(lat * r)) / n, rho0 = a * Math.sqrt(C - n * q(23 * r)) / n, t = n * (lon + 96) * r;
  return [rho * Math.sin(t), rho0 - rho * Math.cos(t)];
}

// the record at a point ({ mukey, mu, comps, hz, areas, eco, ylds, plants, restr }), or null where there is no map unit
async function soilAt(base, lat, lon) {
  const get = async p => { const res = await fetch(`${base}/${p}`); if (!res.ok) throw new Error(`${p}: HTTP ${res.status}`); return res; };
  const ix = await (await get("r30/index.json")).json();
  const [x, y] = albers5070(lon, lat);
  const col = Math.floor((x - ix.x0) / ix.res), row = Math.floor((ix.y0 - y) / ix.res);
  const tr = Math.floor(row / ix.tile), tc = Math.floor(col / ix.tile);
  if (!ix.have.includes(`${tr}/${tc}`)) return null;
  const body = (await get(`r30/${tr}/${tc}.bin`)).body.pipeThrough(new DecompressionStream("deflate"));
  const tile = new DataView(await new Response(body).arrayBuffer());
  const i = tile.getUint16(((row - tr * ix.tile) * ix.tile + (col - tc * ix.tile)) * 2, true);
  const mukey = (await (await get("mukeys.json")).json())[i];
  return mukey ? JSON.parse(await new Response((await get(`mu/${mukey % 1024}.json.gz`)).body.pipeThrough(new DecompressionStream("gzip"))).text())[mukey] || null : null;
}
```

`index.json` and `mukeys.json` (3 kB and 94 kB, 1 kB and 31 kB gzipped) are worth keeping once loaded; a point then costs one tile
(about 55 kB) and one record file (about 130 kB, 17 kB gzipped). `tools/lookup_point.py` does the same in Python:
`python tools/lookup_point.py TX 31.044 -97.347`.

## Rebuilding

1. Download `SQLite_SSURGO_TX.7z` (1.9 GB) from NRCS's Box share, https://nrcs.app.box.com/v/soils, folder
   *SSURGO / SQLite SSURGO (Geopackages) / States*, and unpack it (7-Zip): it holds `SQLite_SSURGO_TX.gpkg` (5.3 GB) and
   `muraster_10m_TX.tif` with its `.ovr` and value table.
2. Run, with Python 3.9 or later, numpy and rasterio:

   ```
   python tools/build_ssurgo.py TX --src <the unpacked SQLite_SSURGO_TX folder>
   ```

   It replaces `TX/mu` and `TX/r30` and rewrites `TX/mukeys.json` and `TX/meta.json` (about a minute for Texas with
   16 processes). `--res 30` makes another cell size (a multiple of 10 m, in `TX/r30`); another state's download builds
   that state's folder (`python tools/build_ssurgo.py OK --src ...`).

## Credit and disclaimer

Soil Survey Staff, Natural Resources Conservation Service, United States Department of Agriculture. Soil Survey
Geographic (SSURGO) Database for Texas, FY2027 (SQLite SSURGO state download, published October 1, 2026). Available
online at https://nrcs.app.box.com/v/soils. Public domain.

This is not an official NRCS product, and NRCS has not reviewed it. It is a converted copy: the tables are reshaped
and the map unit raster is coarsened from 10 m to 30 m, so the map unit found at a point can differ from the one in
NRCS's data, most often near the edge of a map unit. For official soil information use
[Web Soil Survey](https://websoilsurvey.nrcs.usda.gov/) or [Soil Data Access](https://sdmdataaccess.nrcs.usda.gov/).
