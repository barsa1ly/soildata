#!/usr/bin/env python3
"""build_ssurgo.py: a static copy of one state's USDA-NRCS soil survey (SSURGO), for a web page to read when USDA's
Soil Data Access (SDA) does not answer.

The copy answers the question the SoilGuard app asks SDA for a point (js/soil.js batch() and
tablesAt()): which map unit is at the point, and that map unit's rows from eight tables (map unit, components,
horizons, overlapping areas, ecological classes, crop yields, plants, restrictions), with the same columns, the same
row order and every value a string, as SDA's JSON gives them.

Input: NRCS's SQLite SSURGO state download (SQLite_SSURGO_<ST>.7z on the NRCS Box share https://nrcs.app.box.com/v/soils,
folder SSURGO/SQLite SSURGO (Geopackages)/States), unpacked into one folder:
  SQLite_SSURGO_<ST>.gpkg         the SSURGO tables (a SQLite file)
  muraster_10m_<ST>.tif           the 10 m map unit raster (pixel value = mukey, 0 = no map unit), with its .ovr
  muraster_10m_<ST>.tif.vat.dbf   its value table (VALUE, COUNT, MUKEY)
  _ReadMe.txt                     its first line is the publication date (read for meta.json)

Output, under <out>/<ST>/:
  mu/<k>.json.gz          k = mukey % 1024: {"<mukey>": {"mukey": <int>, "mu": {...}, "comps": [...], "hz": [...],
                         "areas": [...], "eco": [...], "ylds": [...], "plants": [...], "restr": [...]}}, the shape
                         tablesAt() returns (mu one row object, the rest arrays of row objects)
  mukeys.json            [null, mukey, mukey, ...]: raster index -> mukey (index 0 = no map unit)
  r<res>/index.json      the tile grid: {"crs", "res", "tile", "x0", "y0", "rows", "cols", "have": ["row/col", ...]}
  r<res>/<row>/<col>.bin one tile: 512 x 512 uint16 little-endian indices into mukeys.json, row by row from the
                         north-west corner, zlib-compressed (what DecompressionStream("deflate") reads in a browser)
  meta.json              source, fiscal year, survey dates, counts, build date and this tool's name
and <out>/.nojekyll (so GitHub Pages serves the files as they are).

Usage (Python 3.9+ with numpy and rasterio; run it from a folder of your own):
  python build_ssurgo.py TX --src E:/LocalAi/soildata-build/unpacked/SQLite_SSURGO_TX
  python build_ssurgo.py TX --res 30          another resolution, a multiple of the 10 m source
  python build_ssurgo.py TX --only tables     only mu/, mukeys.json and meta.json (--only raster: only the tiles)
  python build_ssurgo.py AR --tables <file>   the tables from another SQLite file, such as one state_from_csv.py makes
                                              from NRCS's national CSV tables (a state whose package lacks a usable
                                              GeoPackage: AR and MN in FY2027); its _source table goes into meta.json
--src defaults to <repo>/../soildata-build/unpacked/SQLite_SSURGO_<ST>, --out to the repo this file is in.
A run replaces <out>/<ST>/mu and <out>/<ST>/r<res> as a whole.
"""
import argparse
import gzip
import datetime as dt
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import numpy as np

TOOL = "tools/build_ssurgo.py"
SHARDS = 1024   # map unit records go to SHARDS files by mukey % SHARDS
TILE = 512      # raster tile edge, in cells
CHUNK = 32      # output cell rows each raster worker makes at a time

# ------------------------------------------------------------------------------------------------------------------
# What the apps read for a point: the column lists of js/soil.js batch(), copied as they are. The app's one() helper
# strips the table alias ("mu.mukey" -> "mukey") and hits() makes each row an object with these keys, in this order.
# Lists without aliases read one table (given below).
# ------------------------------------------------------------------------------------------------------------------
MU_COLS = """mu.mukey, mu.musym, mu.muname, mu.mukind, l.areasymbol, l.areaname, sc.saverest,
  ma.slopegraddcp, ma.slopegradwta, ma.brockdepmin, ma.wtdepannmin, ma.flodfreqdcd, ma.drclassdcd,
  ma.hydgrpdcd, ma.aws0150wta, ma.niccdcd, ma.iccdcd, ma.wtdepaprjunmin"""
COMP_COLS = """cokey, compname, comppct_r, majcompflag, compkind, localphase, taxclname, taxorder, taxsuborder,
  taxgrtgroup, taxsubgrp, taxpartsize, taxtempregime, taxmoistscl, drainagecl, hydgrp, slope_l, slope_r,
  slope_h, slopelenusle_r, weg, wei, tfact, albedodry_r, runoff, elev_r, nirrcapcl, nirrcapscl, irrcapcl,
  irrcapscl, rsprod_r, geomdesc, map_r, airtempa_r, ffd_r, erocl, hydricrating"""
HZ_COLS = """ch.cokey, ch.chkey, ch.hzname, ch.hzdept_r, ch.hzdepb_r, ch.sandtotal_r, ch.silttotal_r,
  ch.claytotal_r, ch.sandvf_r, ch.sandfine_r, ch.sandmed_r, ch.sandco_r, ch.sandvc_r, ch.om_r,
  ch.dbthirdbar_r, ch.wthirdbar_r, ch.wfifteenbar_r, ch.wsatiated_r, ch.awc_r, ch.ksat_r,
  ch.ph1to1h2o_r, ch.cec7_r, ch.ecec_r, ch.caco3_r, ch.gypsum_r, ch.ec_r, ch.sar_r, ch.kwfact, ch.kffact,
  ch.sieveno10_r, ch.frag3to10_r, ch.fraggt10_r, ch.fragvoltot_r, ch.lep_r, ch.partdensity, tg.texture, tg.texdesc,
  ch.ph01mcacl2_r, ch.esp_r, ch.sumbases_r"""
AREA_COLS = "la.areasymbol, la.areaname, la.areatypename, mo.areaovacres"
ECO_COLS = "cokey, ecoclassid, ecoclassname, ecoclasstypename"          # coecoclass
YLD_COLS = "cokey, cropname, yldunits, nonirryield_r, irryield_r"       # cocropyld
PLANT_COLS = "cokey, plantsym, plantsciname, plantcomname, rangeprod"   # coeplants
RESTR_COLS = "cokey, reskind, reshard, resdept_r, resdepb_r"            # corestrictions


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------------------------------------------------------
# Values as SDA's JSON gives them: every value a string, null as null. Whole numbers carry no ".0" (SDA writes a float
# of 9 as "9"); other reals are Python's shortest repr ("0.3"); text is kept as it is (".37", "No "). A survey date
# ("09/03/2026 13:37:01" in the GeoPackage) is written the way SDA writes a date and time: "9/3/2026 1:37:01 PM".
# ------------------------------------------------------------------------------------------------------------------
def sda_str(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def sda_datetime(v):
    if v is None:
        return None
    s = str(v).strip()
    for f in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"):
        try:
            d = dt.datetime.strptime(s, f)
        except ValueError:
            continue
        h = d.hour % 12 or 12
        return f"{d.month}/{d.day}/{d.year} {h}:{d.minute:02d}:{d.second:02d} {'AM' if d.hour < 12 else 'PM'}"
    return s


def parse_survey_date(v):
    for f in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(str(v).strip(), f)
        except (ValueError, TypeError):
            pass
    return None


# ------------------------------------------------------------------------------------------------------------------
# Tables
# ------------------------------------------------------------------------------------------------------------------
def open_gpkg(path):
    # read-only and immutable: nothing is written next to the source file
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def columns_of(con):
    have = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        have[t] = {r[1] for r in con.execute(f'PRAGMA table_info("{t}")')}
    return have


def select_items(cols, aliases, have, default=None):
    """The app's column list -> SQL select items, output names, and the columns this source does not have (read as NULL)."""
    items, names, missing = [], [], []
    for c in cols.split(","):
        c = c.strip()
        a, col = c.split(".", 1) if "." in c else (default, c)
        t = aliases[a]
        if col in have.get(t, ()):
            items.append(f'{a}."{col}"')
        else:
            items.append("NULL")
            missing.append(f"{t}.{col}")
        names.append(col)
    return items, names, missing


def grouped(con, sql):
    """Rows of a query whose first column is the mukey, grouped by it: {mukey: [row tuple without the mukey, ...]}."""
    out = {}
    for r in con.execute(sql):
        out.setdefault(r[0], []).append(r[1:])
    return out


def read_tables(con):
    """Every map unit's rows of the eight tables, as {mukey: {"mu": [...], "comps": [...], ...}} (rows as tuples of the
    output columns, sorted the way SDA sorts them), plus the output names of each table and the missing columns."""
    have = columns_of(con)
    names, missing, data = {}, [], {}

    def take(key, cols, aliases, default, frm, group, extra, sort):
        items, nm, miss = select_items(cols, aliases, have, default)
        names[key] = nm
        missing.extend(miss)
        sql = f"SELECT {group}{''.join(', ' + e for e in extra)}, {', '.join(items)} FROM {frm}"
        g = grouped(con, sql)
        nx = len(extra)
        col = {n: nx + i for i, n in enumerate(nm)}
        for mk, rows in g.items():
            rows.sort(key=lambda r: sort(r, col))
            data.setdefault(mk, {})[key] = [r[nx:] for r in rows]
        log(f"  {key}: {sum(len(v) for v in g.values())} rows for {len(g)} map units")

    nulls_last_desc = lambda v: (v is None, -(v or 0))   # SQL Server: ORDER BY x DESC puts NULL last
    nulls_first_asc = lambda v: (v is not None, v or 0)  # ... and ORDER BY x puts NULL first
    ikey = lambda v: int(v) if v is not None else -1

    # the map unit, its survey area (legend) and the survey's date (sacatalog), its aggregated attributes (muaggatt)
    take("mu", MU_COLS, {"mu": "mapunit", "l": "legend", "ma": "muaggatt", "sc": "sacatalog"}, None,
         "mapunit mu JOIN legend l ON l.lkey = mu.lkey LEFT JOIN muaggatt ma ON ma.mukey = mu.mukey "
         "LEFT JOIN sacatalog sc ON sc.areasymbol = l.areasymbol", "mu.mukey", [], lambda r, c: 0)
    # components: ORDER BY comppct_r DESC (ties by cokey)
    take("comps", COMP_COLS, {"c": "component"}, "c", "component c", "c.mukey", [],
         lambda r, c: (*nulls_last_desc(r[c["comppct_r"]]), ikey(r[c["cokey"]])))
    # horizons with their representative texture group: ORDER BY cokey, hzdept_r (ties by chkey, chtgkey)
    take("hz", HZ_COLS, {"ch": "chorizon", "tg": "chtexturegrp"}, None,
         "chorizon ch JOIN component c ON c.cokey = ch.cokey "
         "LEFT JOIN chtexturegrp tg ON tg.chkey = ch.chkey AND tg.rvindicator = 'Yes'",
         "c.mukey", ["tg.chtgkey"],
         lambda r, c: (ikey(r[c["cokey"]]), *nulls_first_asc(r[c["hzdept_r"]]), ikey(r[c["chkey"]]), ikey(r[0])))
    # overlapping areas (counties, MLRA, climate and rainfall factor areas...): ORDER BY areaovacres DESC
    take("areas", AREA_COLS, {"la": "laoverlap", "mo": "muaoverlap"}, None,
         "muaoverlap mo JOIN laoverlap la ON la.lareaovkey = mo.lareaovkey", "mo.mukey", ["mo.muareaovkey"],
         lambda r, c: (*nulls_last_desc(r[c["areaovacres"]]), ikey(r[0])))
    # component child tables, no ORDER BY in the app: by cokey, then the table's own key (the order SQL Server's
    # index lookups give)
    for key, cols, t, k in (("eco", ECO_COLS, "coecoclass", "coecoclasskey"), ("ylds", YLD_COLS, "cocropyld", "cocropyldkey"),
                            ("plants", PLANT_COLS, "coeplants", "coeplantskey"), ("restr", RESTR_COLS, "corestrictions", "corestrictkey")):
        take(key, cols, {"t": t}, "t", f"{t} t JOIN component c ON c.cokey = t.cokey", "c.mukey", [f"t.{k}"],
             lambda r, c: (ikey(r[c["cokey"]]), ikey(r[0])))
    return data, names, sorted(set(missing))


def record(mukey, d, names):
    """One map unit as tablesAt() returns it, values as SDA's strings."""
    def rows(key):
        nm = names[key]
        out = []
        for r in d.get(key, []):
            o = {}
            for n, v in zip(nm, r):
                o[n] = sda_datetime(v) if n == "saverest" else sda_str(v)
            out.append(o)
        return out
    mu = rows("mu")
    rec = {"mukey": int(mukey), "mu": mu[0] if mu else {}}
    for key in ("comps", "hz", "areas", "eco", "ylds", "plants", "restr"):
        rec[key] = rows(key)
    return rec


def write_shards(out_dir, mukeys, data, names):
    mu_dir = out_dir / "mu"
    if mu_dir.exists():
        shutil.rmtree(mu_dir)
    mu_dir.mkdir(parents=True)
    shards = [dict() for _ in range(SHARDS)]
    for mk in mukeys:                                        # ascending mukey within each shard
        shards[mk % SHARDS][str(mk)] = record(mk, data.get(mk, {}), names)
    sizes = []
    for k, s in enumerate(shards):                           # every shard is written, an empty one as {}
        b = json.dumps(s, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        g = gzip.compress(b, 9, mtime=0)                     # stored gzipped: the browser inflates it (DecompressionStream)
        (mu_dir / f"{k}.json.gz").write_bytes(g)
        sizes.append(len(g))
    return sizes


# ------------------------------------------------------------------------------------------------------------------
# Raster value table (dBASE file next to the GeoTIFF)
# ------------------------------------------------------------------------------------------------------------------
def read_dbf(path):
    b = Path(path).read_bytes()
    nrec, hlen, rlen = struct.unpack("<IHH", b[4:12])
    fields, off = [], 32
    while b[off] != 0x0D:
        fields.append((b[off:off + 11].split(b"\0")[0].decode("ascii").upper(), b[off + 16]))
        off += 32
    out = []
    for i in range(nrec):
        r = b[hlen + i * rlen: hlen + (i + 1) * rlen]
        if r[:1] == b"*":                                    # deleted record
            continue
        pos, d = 1, {}
        for name, ln in fields:
            d[name] = r[pos:pos + ln].decode("latin-1").strip()
            pos += ln
        out.append(d)
    return out


# ------------------------------------------------------------------------------------------------------------------
# Raster: the 10 m map unit raster to <res> m cells. Each output cell takes the source pixels whose centres fall in it
# (f x f of them, f = res / 10) and keeps the map unit that covers most of them; pixels with no map unit do not count,
# so a cell is empty only when all its pixels are; a tie goes to the centre pixel's map unit, then to the lower index.
# With --resample nearest a cell takes its centre pixel's map unit instead.
# ------------------------------------------------------------------------------------------------------------------
def block_mode(idx, f, how="mode"):
    """idx: (f*R, f*C) uint16 map unit indices (0 = none) -> (R, C) uint16."""
    R, C = idx.shape[0] // f, idx.shape[1] // f
    b = idx.reshape(R, f, C, f).transpose(0, 2, 1, 3).reshape(R * C, f * f)
    centre = (f // 2) * f + f // 2
    if how == "nearest":
        return b[:, centre].reshape(R, C).copy()
    mn, mx = b.min(axis=1), b.max(axis=1)
    out = mx.copy()                                          # a uniform cell (all one value, or all 0) is that value
    nu = np.flatnonzero(mn != mx)
    if nu.size:
        sub = b[nu]
        cen = sub[:, centre]
        s = np.sort(sub, axis=1)
        n, m = s.shape
        start = np.empty((n, m), bool)                       # where a run of equal values starts in each sorted row
        start[:, 0] = True
        np.not_equal(s[:, 1:], s[:, :-1], out=start[:, 1:])
        fs = np.flatnonzero(start)
        run = np.diff(np.append(fs, n * m))                  # run lengths (each row starts a run, so none spans rows)
        row = fs // m
        val = s.ravel()[fs]
        score = run.astype(np.int32) * 2 + (val == cen[row])
        score[val == 0] = -1                                 # no map unit never wins (these rows hold at least one)
        order = np.lexsort((-score, row))                    # by row, best score first, lower value first (stable)
        rs = row[order]
        first = np.ones(rs.size, bool)
        first[1:] = rs[1:] != rs[:-1]
        out[nu] = val[order[first]]
    return out.reshape(R, C)


_W = {}   # per worker process: the open raster and the grid


def _init_worker(raster, lut, geo):
    import rasterio
    env = rasterio.Env(GDAL_CACHEMAX=64)                     # each strip is read once: no need for a big block cache
    env.__enter__()
    _W.update(env=env, ds=rasterio.open(raster), lut=lut, geo=geo)


def _do_chunk(k):
    """Output cell rows [k*CHUNK, (k+1)*CHUNK): read the source strip under them, map mukeys to indices, resample."""
    from rasterio.windows import Window
    g, ds, lut = _W["geo"], _W["ds"], _W["lut"]
    f, ncols = g["f"], g["ncols"]
    r_a = k * g["chunk"]
    r_b = min(r_a + g["chunk"], g["nrows"])
    i_a, i_b = g["i0"] + f * r_a, g["i0"] + f * r_b          # source rows under these cells
    idx = np.zeros((f * (r_b - r_a), f * ncols), np.uint16)
    unknown, n10 = set(), 0
    ra, rb = max(i_a, 0), min(i_b, g["H"])
    ca, cb = max(g["j0"], 0), min(g["j0"] + f * ncols, g["W"])  # source columns inside the grid
    if ra < rb and ca < cb:
        a = ds.read(1, window=Window(ca, ra, cb - ca, rb - ra))
        if g["nodata"] not in (None, 0):
            a[a == g["nodata"]] = 0
        if a.dtype.kind == "i" and (a < 0).any():             # a signed raster: a negative value is no mukey
            unknown.update(np.unique(a[a < 0]).tolist())
            a[a < 0] = 0
        big = a >= lut.size
        if big.any():
            unknown.update(np.unique(a[big]).tolist())
            a[big] = 0
        m = lut[a]
        bad = (m == 0) & (a != 0)
        if bad.any():
            unknown.update(np.unique(a[bad]).tolist())
        idx[ra - i_a: rb - i_a, ca - g["j0"]: cb - g["j0"]] = m
        n10 = int(np.count_nonzero(m))
    out = block_mode(idx, f, g["how"])
    # how often the cell's map unit is the one at a 10 m pixel (over pixels with a map unit)
    same = 0
    if n10:
        rep = np.repeat(np.repeat(out, f, axis=0), f, axis=1)
        same = int(np.count_nonzero((idx == rep) & (idx != 0)))
    p10 = np.bincount(idx.ravel(), minlength=g["nidx"]) > 0
    p90 = np.bincount(out.ravel(), minlength=g["nidx"]) > 0
    return k, out, sorted(unknown), n10, same, p10, p90


def build_raster(raster, out_dir, mukeys, value_to_mukey, res, workers, how):
    import rasterio
    with rasterio.open(raster) as ds:
        t = ds.transform
        if abs(t.b) > 1e-9 or abs(t.d) > 1e-9 or t.e >= 0:
            raise SystemExit("the raster must be north-up")
        src = t.a
        if abs(src + t.e) > 1e-9:
            raise SystemExit("the raster's pixels must be square")
        f = res / src
        if abs(f - round(f)) > 1e-9 or round(f) < 1:
            raise SystemExit(f"--res must be a multiple of the source's {src:g} m")
        f = int(round(f))
        W, H = ds.width, ds.height
        left, top = t.c, t.f
        right, bottom = left + src * W, top - src * H
        epsg = ds.crs.to_epsg() if ds.crs else None
        crs = f"EPSG:{epsg}" if epsg else (ds.crs.to_wkt() if ds.crs else None)
        nodata = ds.nodata
        if ds.dtypes[0] not in ("uint8", "uint16", "uint32", "int32"):
            raise SystemExit(f"unexpected raster type {ds.dtypes[0]}")
    ts = res * TILE                                          # tile edge in metres; the grid's origin is a multiple of it
    x0 = math.floor(left / ts) * ts
    y0 = math.ceil(top / ts) * ts
    tcols = math.ceil((right - x0) / ts - 1e-9)
    trows = math.ceil((y0 - bottom) / ts - 1e-9)
    nrows, ncols = trows * TILE, tcols * TILE
    # first source row / column whose pixel centre lies in output row / column 0 (cell rows run (y-res, y], columns [x, x+res))
    i0 = math.ceil((top - src / 2 - y0) / src - 1e-9)
    j0 = math.ceil((x0 - left - src / 2) / src - 1e-9)
    log(f"  grid {crs} {res} m, origin x0={x0} y0={y0}, {trows} x {tcols} tiles of {TILE} ({nrows} x {ncols} cells); "
        f"source {W} x {H} at {src:g} m, {f} x {f} pixels per cell, i0={i0} j0={j0}")

    # source value -> index into mukeys.json (0 = none)
    pos = {mk: i for i, mk in enumerate(mukeys) if i}
    lut = np.zeros(max(max(value_to_mukey, default=0), max(mukeys[1:])) + 1, np.uint16)
    if all(v == mk for v, mk in value_to_mukey.items()):     # pixel values are mukeys (also with no value table)
        for mk, i in pos.items():
            lut[mk] = i
    for v, mk in value_to_mukey.items():
        lut[v] = pos[mk]

    geo = dict(f=f, ncols=ncols, nrows=nrows, chunk=CHUNK, i0=i0, j0=j0, W=W, H=H, nodata=nodata, how=how, nidx=len(mukeys))
    grid = np.zeros((nrows, ncols), np.uint16)
    nchunks = math.ceil(nrows / CHUNK)
    unknown, n10, same = set(), 0, 0
    p10 = np.zeros(len(mukeys), bool)
    p90 = np.zeros(len(mukeys), bool)
    t_0 = time.time()
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(str(raster), lut, geo)) as ex:
        for done, (k, out, unk, a, b, q10, q90) in enumerate(ex.map(_do_chunk, range(nchunks)), 1):
            grid[k * CHUNK: k * CHUNK + out.shape[0]] = out
            unknown.update(unk)
            n10 += a
            same += b
            p10 |= q10
            p90 |= q90
            if done % 50 == 0 or done == nchunks:
                log(f"  raster rows: {done}/{nchunks} chunks, {time.time() - t_0:.0f} s")
    if unknown:
        raise SystemExit(f"raster values with no map unit in the tables or the value table: {sorted(unknown)[:20]}...")

    rdir = out_dir / f"r{res}"
    if rdir.exists():
        shutil.rmtree(rdir)
    rdir.mkdir(parents=True)
    jobs = [(r, c) for r in range(trows) for c in range(tcols)
            if grid[r * TILE:(r + 1) * TILE, c * TILE:(c + 1) * TILE].any()]

    def write_tile(rc):
        r, c = rc
        a = np.ascontiguousarray(grid[r * TILE:(r + 1) * TILE, c * TILE:(c + 1) * TILE]).astype("<u2")
        (rdir / str(r)).mkdir(exist_ok=True)
        b = zlib.compress(a.tobytes(), 9)
        (rdir / str(r) / f"{c}.bin").write_bytes(b)
        return len(b)

    for r in {r for r, _ in jobs}:
        (rdir / str(r)).mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as tp:
        sizes = list(tp.map(write_tile, jobs))
    index = {"crs": crs, "res": res, "tile": TILE, "x0": x0, "y0": y0, "rows": trows, "cols": tcols,
             "have": [f"{r}/{c}" for r, c in jobs],
             "format": "zlib-compressed uint16 little-endian, row-major from the north-west corner; "
                       "value = index into ../mukeys.json (0 = no map unit)",
             "resample": how}
    (rdir / "index.json").write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    stats = {"tiles": len(jobs), "tileBytes": sum(sizes), "cells": int(np.count_nonzero(grid)),
             "pixels10m": n10, "sameAs10m": same, "mapunitsIn10m": int(p10[1:].sum()), "mapunitsInGrid": int(p90[1:].sum()),
             "grid": {k: index[k] for k in ("crs", "res", "tile", "x0", "y0", "rows", "cols")}}
    log(f"  {len(jobs)} tiles, {sum(sizes) / 1e6:.1f} MB; cells with a map unit {stats['cells']}; "
        f"map units: {stats['mapunitsIn10m']} at 10 m, {stats['mapunitsInGrid']} at {res} m; "
        f"{res} m cell = 10 m pixel's map unit for {100 * same / max(n10, 1):.2f}% of pixels")
    return stats


# ------------------------------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Build a static copy of one state's SSURGO soil survey from NRCS's SQLite "
                                             "SSURGO download (see the top of this file).")
    ap.add_argument("state", help="two-letter state code, as in SQLite_SSURGO_<ST>.7z (TX)")
    ap.add_argument("--res", type=int, default=30, help="output raster cell size in metres, a multiple of 10 (default 30)")
    ap.add_argument("--src", help="folder with SQLite_SSURGO_<ST>.gpkg and muraster_10m_<ST>.tif")
    ap.add_argument("--out", help="the repo folder (default: the one this tool is in)")
    ap.add_argument("--tables", help="a SQLite file with the tables, instead of <src>/SQLite_SSURGO_<ST>.gpkg")
    ap.add_argument("--only", choices=("all", "tables", "raster"), default="all")
    ap.add_argument("--resample", choices=("mode", "nearest"), default="mode")
    ap.add_argument("--workers", type=int, default=min(16, os.cpu_count() or 1))
    args = ap.parse_args()

    st = args.state.upper()
    repo = Path(args.out).resolve() if args.out else Path(__file__).resolve().parent.parent
    src = Path(args.src).resolve() if args.src else (repo.parent / "soildata-build" / "unpacked" / f"SQLite_SSURGO_{st}")
    gpkg = Path(args.tables).resolve() if args.tables else src / f"SQLite_SSURGO_{st}.gpkg"
    raster = src / f"muraster_10m_{st}.tif"
    vat = src / f"muraster_10m_{st}.tif.vat.dbf"
    for p in (gpkg, raster):
        if not p.exists():
            raise SystemExit(f"missing {p}")
    out_dir = repo / st
    out_dir.mkdir(parents=True, exist_ok=True)
    (repo / ".nojekyll").touch()
    log(f"{st}: {gpkg} -> {out_dir}")

    con = open_gpkg(gpkg)
    # every map unit of the tables and of the raster's value table, in mukey order; index 0 = no map unit
    table_mk = [r[0] for r in con.execute("SELECT mukey FROM mapunit")]
    value_to_mukey = {}
    if vat.exists():
        for d in read_dbf(vat):
            v = int(d.get("VALUE") or d.get("MUKEY"))
            value_to_mukey[v] = int(d.get("MUKEY") or v)
    mukeys = [None] + sorted(set(int(m) for m in table_mk) | set(value_to_mukey.values()))
    if len(mukeys) > 65535:
        raise SystemExit("more than 65535 map units: uint16 tiles cannot hold them")
    only_raster = sorted(set(value_to_mukey.values()) - set(int(m) for m in table_mk))
    differ = sum(1 for v, mk in value_to_mukey.items() if v != mk)
    log(f"  {len(table_mk)} map units in the tables, {len(value_to_mukey)} in the raster's value table "
        f"({len(only_raster)} only there; {differ} with VALUE != MUKEY); {len(mukeys) - 1} in mukeys.json")

    meta_path = out_dir / "meta.json"
    old = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta = {"state": st}

    # source and publication date (first line of the download's _ReadMe.txt, e.g. "October 01, 2026")
    published, fy = old.get("published"), old.get("fy")
    readme = src / "_ReadMe.txt"
    if readme.exists():
        for line in readme.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    d = dt.datetime.strptime(line.strip(), "%B %d, %Y").date()
                    published, fy = d.isoformat(), f"FY{d.year + 1 if d.month >= 10 else d.year}"
                except ValueError:
                    pass
                break
    meta["source"] = {
        "name": "USDA-NRCS Soil Survey Geographic Database (SSURGO), SQLite SSURGO (GeoPackage) state download",
        "file": f"SQLite_SSURGO_{st}.7z",
        "url": "https://nrcs.app.box.com/v/soils",
        "path": "SSURGO/SQLite SSURGO (Geopackages)/States",
        "tables": gpkg.name, "raster": raster.name}
    if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '_source'").fetchone():
        meta["source"].update(dict(con.execute("SELECT key, value FROM _source")))   # where the tables came from
    meta["fy"], meta["published"] = fy, published
    sv = [parse_survey_date(r[0]) for r in con.execute("SELECT saverest FROM sacatalog")]
    sv = [d for d in sv if d]
    meta["surveyAreas"] = con.execute("SELECT COUNT(*) FROM sacatalog").fetchone()[0]
    meta["saverest"] = {"min": min(sv).isoformat(sep=" ") if sv else None, "max": max(sv).isoformat(sep=" ") if sv else None}

    if args.only in ("all", "tables"):
        log("tables")
        data, names, missing = read_tables(con)
        if missing:
            log(f"  not in this source (written as null): {', '.join(missing)}")
        sizes = write_shards(out_dir, mukeys[1:], data, names)
        (out_dir / "mukeys.json").write_text(json.dumps(mukeys, separators=(",", ":")), encoding="utf-8")
        log(f"  {SHARDS} shards, {sum(sizes) / 1e6:.1f} MB, largest {max(sizes) / 1e3:.0f} kB")
        meta["counts"] = {
            "mapunits": len(mukeys) - 1,
            "mapunitsWithRows": sum(1 for mk in mukeys[1:] if data.get(mk, {}).get("mu")),
            "components": con.execute("SELECT COUNT(*) FROM component").fetchone()[0],
            "horizons": con.execute("SELECT COUNT(*) FROM chorizon").fetchone()[0],
            "hzRows": sum(len(d.get("hz", [])) for d in data.values())}
        meta["nullColumns"] = missing
    else:
        for k in ("counts", "nullColumns"):
            if k in old:
                meta[k] = old[k]
    if args.only in ("all", "raster"):
        log("raster")
        meta["raster"] = {"path": f"r{args.res}/index.json", "resample": args.resample,
                          **build_raster(raster, out_dir, mukeys, value_to_mukey, args.res, args.workers, args.resample)}
        if args.only == "raster" and not (out_dir / "mukeys.json").exists():
            (out_dir / "mukeys.json").write_text(json.dumps(mukeys, separators=(",", ":")), encoding="utf-8")
    elif "raster" in old:
        meta["raster"] = old["raster"]
    meta["shards"] = {"path": "mu/<mukey % 1024>.json", "count": SHARDS}
    meta["built"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta["tool"] = TOOL
    meta_path.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    log("done")


if __name__ == "__main__":
    main()
