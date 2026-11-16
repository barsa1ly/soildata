#!/usr/bin/env python3
"""lookup_point.py: read the soil survey copy at a point, the way a web page would (a reference reader for checks).

  python lookup_point.py TX 31.044 -97.347                  map unit, name and components at Temple, Texas
  python lookup_point.py TX 31.044 -97.347 --raster <muraster_10m_TX.tif>   also the 10 m raster's map unit there
  python lookup_point.py TX 31.044 -97.347 --json           the whole record, as the page receives it

It needs only Python's standard library (rasterio for --raster). The lon/lat -> EPSG:5070 step is albers5070() below,
the same formulas as the JavaScript in README.md; it is checked against PROJ (pyproj) when pyproj is installed.
"""
import argparse
import json
import math
import struct
import sys
import zlib
from pathlib import Path


def albers5070(lon, lat):
    """lon/lat in degrees (NAD83; WGS84 differs by about a metre) -> NAD83 / Conus Albers (EPSG:5070) x, y in metres.
    Snyder (1987), Map Projections: A Working Manual, eqs. 14-1 to 14-6 and 3-12, ellipsoid GRS 1980."""
    a, f = 6378137.0, 1 / 298.257222101
    e2 = f * (2 - f)
    e = math.sqrt(e2)
    r = math.pi / 180
    m = lambda p: math.cos(p) / math.sqrt(1 - e2 * math.sin(p) ** 2)
    def q(p):
        s = math.sin(p)
        return (1 - e2) * (s / (1 - e2 * s * s) - math.log((1 - e * s) / (1 + e * s)) / (2 * e))
    p1, p2, p0, l0 = 29.5 * r, 45.5 * r, 23 * r, -96 * r
    n = (m(p1) ** 2 - m(p2) ** 2) / (q(p2) - q(p1))
    C = m(p1) ** 2 + n * q(p1)
    rho = a * math.sqrt(C - n * q(lat * r)) / n
    rho0 = a * math.sqrt(C - n * q(p0)) / n
    t = n * (lon * r - l0)
    return rho * math.sin(t), rho0 - rho * math.cos(t)


def lookup(root, st, lat, lon, res=90):
    """-> (x, y, index, mukey, record or None, tile "row/col", pixel (row, col))"""
    d = Path(root) / st
    ix = json.loads((d / f"r{res}" / "index.json").read_text(encoding="utf-8"))
    if ix["crs"] != "EPSG:5070":
        raise SystemExit(f"this reader projects to EPSG:5070 only, the grid is {ix['crs']}")
    x, y = albers5070(lon, lat)
    col = math.floor((x - ix["x0"]) / ix["res"])
    row = math.floor((ix["y0"] - y) / ix["res"])
    T = ix["tile"]
    tr, tc = row // T, col // T
    key = f"{tr}/{tc}"
    if not (0 <= tr < ix["rows"] and 0 <= tc < ix["cols"]) or key not in set(ix["have"]):
        return x, y, 0, None, None, key, None
    raw = zlib.decompress((d / f"r{res}" / str(tr) / f"{tc}.bin").read_bytes())
    pr, pc = row - tr * T, col - tc * T
    i = struct.unpack_from("<H", raw, (pr * T + pc) * 2)[0]
    mukeys = json.loads((d / "mukeys.json").read_text(encoding="utf-8"))
    mk = mukeys[i]
    rec = None
    if mk:
        shard = json.loads((d / "mu" / f"{mk % 1024}.json").read_text(encoding="utf-8"))
        rec = shard.get(str(mk))
    return x, y, i, mk, rec, key, (pr, pc)


def raster_mukey(path, x, y):
    import rasterio
    from rasterio.windows import Window
    with rasterio.open(path) as ds:
        r, c = ds.index(x, y)
        if not (0 <= r < ds.height and 0 <= c < ds.width):
            return None
        v = int(ds.read(1, window=Window(c, r, 1, 1))[0, 0])
        return v or None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("state")
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--res", type=int, default=90)
    ap.add_argument("--raster", help="the 10 m map unit GeoTIFF, to compare")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    x, y, i, mk, rec, tile, px = lookup(a.root, a.state.upper(), a.lat, a.lon, a.res)
    try:
        from pyproj import Transformer
        X, Y = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True).transform(a.lon, a.lat)
        proj = f" (PROJ: {X:.2f}, {Y:.2f})"
    except ImportError:
        proj = ""
    if a.json:
        print(json.dumps(rec, ensure_ascii=False, indent=1))
        return
    print(f"{a.lat}, {a.lon} -> EPSG:5070 {x:.2f}, {y:.2f}{proj}; tile {tile} pixel {px}; index {i}; mukey {mk}")
    if a.raster:
        print(f"  10 m raster: mukey {raster_mukey(a.raster, x, y)}")
    if rec:
        mu = rec["mu"]
        print(f"  {mu.get('muname')} ({mu.get('musym')}, {mu.get('areasymbol')} {mu.get('areaname')}, survey {mu.get('saverest')})")
        for c in rec["comps"][:4]:
            nh = sum(1 for h in rec["hz"] if h["cokey"] == c["cokey"])
            print(f"  {c['comppct_r']}% {c['compname']} ({c['compkind']}, major {c['majcompflag']}), {nh} horizons")


if __name__ == "__main__":
    main()
