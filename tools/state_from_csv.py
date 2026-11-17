#!/usr/bin/env python3
"""state_from_csv.py: a state's SSURGO tables as a SQLite file, made from NRCS's national CSV tables, for build_ssurgo.py.

For a state whose SQLite SSURGO package cannot be used: in FY2027, Arkansas's package (SQLite_SSURGO_AR.7z) holds the
map unit raster but no GeoPackage, and Minnesota's GeoPackage is damaged ("database disk image is malformed"). NRCS
publishes the same FY2027 tables for the whole conterminous US as CSV: ssurgo_tabular_CONUS_2027.7z (2.0 GB; 51 GB
unpacked) on the NRCS Box share https://nrcs.app.box.com/v/soils, folder SSURGO / Open Formatted Files / US /
tabular (csv). Unpack only the tables build_ssurgo.py reads, and the column types (schema.ini):

  7z e ssurgo_tabular_CONUS_2027.7z -o<csv folder> csv_ssurgo\\schema.ini csv_ssurgo\\mapunit.csv csv_ssurgo\\legend.csv
     csv_ssurgo\\muaggatt.csv csv_ssurgo\\sacatalog.csv csv_ssurgo\\component.csv csv_ssurgo\\chorizon.csv
     csv_ssurgo\\chtexturegrp.csv csv_ssurgo\\laoverlap.csv csv_ssurgo\\muaoverlap.csv csv_ssurgo\\coecoclass.csv
     csv_ssurgo\\cocropyld.csv csv_ssurgo\\coeplants.csv csv_ssurgo\\corestrictions.csv

Then (Python 3.9+ with numpy):
  python state_from_csv.py AR MN --csv <csv folder> --out <folder>
writes <folder>/SQLite_SSURGO_<ST>.sqlite for each state, and build_ssurgo.py reads it with --tables:
  python build_ssurgo.py AR --src <unpacked SQLite_SSURGO_AR folder (raster)> --tables <folder>/SQLite_SSURGO_AR.sqlite

A state's rows are its survey areas (legend.areasymbol starting with the state code, as in NRCS's state packages) and
every row that hangs from them by key: map units, their aggregated attributes and overlapping areas, components, and
the components' horizons, texture groups, ecological classes, crop yields, plants and restrictions. Values are typed
as in NRCS's GeoPackages: keys and whole numbers as integers, reals as reals (a "Single" column, 4-byte in NRCS's
database and written with 7 decimals in the CSV such as 4.5999999, is read back as the shortest decimal of that 4-byte
number, 4.6), dates as "MM/DD/YYYY HH:MM:SS", text as it is, an empty field as NULL. A table _source names the CSV
file, for build_ssurgo.py's meta.json.
"""
import argparse
import csv
import datetime as dt
import re
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

csv.field_size_limit(1 << 30)    # sacatalog.fgdcmetadata holds whole XML documents

# the tables build_ssurgo.py reads, in the order they are read here (each one filtered by the keys found before it)
TABLES = ["legend", "sacatalog", "laoverlap", "mapunit", "muaggatt", "muaoverlap", "component", "chorizon",
          "chtexturegrp", "coecoclass", "cocropyld", "coeplants", "corestrictions"]
SOURCE = {
    "name": "USDA-NRCS Soil Survey Geographic Database (SSURGO): the tables from the national CSV download, the map unit "
            "raster from the SQLite SSURGO state download",
    "tables": "ssurgo_tabular_CONUS_2027.7z (csv_ssurgo), the state's survey areas",
    "tablesUrl": "https://nrcs.app.box.com/v/soils",
    "tablesPath": "SSURGO/Open Formatted Files/US/tabular (csv)",
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def read_schema(path):
    """schema.ini -> {table: [(column, type), ...]} (type: Text, Long, Short, Single, Double, Date, ...)."""
    out, sec = {}, None
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        m = re.match(r"\[(.+)\.csv\]$", line, re.I)
        if m:
            sec = m.group(1).lower()
            out[sec] = []
            continue
        m = re.match(r"Col\d+=(\S+)\s+(\S+)", line)
        if m and sec is not None:
            out[sec].append((m.group(1), m.group(2)))
    return out


def sql_type(col, typ):
    if typ in ("Long", "Short", "Byte", "Integer") or (typ == "Text" and col.endswith("key")):
        return "INTEGER"
    if typ in ("Single", "Double", "Float", "Currency"):
        return "REAL"
    return "TEXT"


def f32(s):
    return float(np.format_float_positional(np.float32(s), unique=True, trim="-"))


def to_int(s):
    try:
        return int(s)
    except ValueError:
        return int(float(s))


def to_date(s):
    try:
        return dt.datetime.strptime(s, "%m/%d/%Y %H:%M:%S").strftime("%m/%d/%Y %H:%M:%S")
    except ValueError:
        return s


def converter(col, typ):
    t = sql_type(col, typ)
    if t == "INTEGER":
        return to_int
    if typ == "Single":
        return f32
    if t == "REAL":
        return float
    if typ == "Date":
        return to_date
    return None                                        # text: as it is


# which already-kept key a table's rows are chosen by: (column, the key set it must be in)
BY = {"legend": None, "sacatalog": None, "laoverlap": ("lkey", "lkey"), "mapunit": ("lkey", "lkey"),
      "muaggatt": ("mukey", "mukey"), "muaoverlap": ("mukey", "mukey"), "component": ("mukey", "mukey"),
      "chorizon": ("cokey", "cokey"), "chtexturegrp": ("chkey", "chkey"), "coecoclass": ("cokey", "cokey"),
      "cocropyld": ("cokey", "cokey"), "coeplants": ("cokey", "cokey"), "corestrictions": ("cokey", "cokey")}
# the keys a table's kept rows add, for the tables after it
GIVES = {"legend": ("lkey", "lkey"), "mapunit": ("mukey", "mukey"), "component": ("cokey", "cokey"),
         "chorizon": ("chkey", "chkey")}


def main():
    ap = argparse.ArgumentParser(description="Make SQLite files of states' SSURGO tables from NRCS's national CSV "
                                             "tables (see the top of this file).")
    ap.add_argument("states", nargs="+", help="two-letter state codes (AR MN)")
    ap.add_argument("--csv", required=True, help="folder with schema.ini and the tables' .csv files")
    ap.add_argument("--out", required=True, help="folder for SQLite_SSURGO_<ST>.sqlite")
    args = ap.parse_args()
    states = [s.upper() for s in args.states]
    src, out = Path(args.csv), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    schema = read_schema(src / "schema.ini")

    cons, keys = {}, {st: {} for st in states}
    for st in states:
        p = out / f"SQLite_SSURGO_{st}.sqlite"
        if p.exists():
            p.unlink()
        con = sqlite3.connect(p)
        con.execute("PRAGMA journal_mode = OFF")
        con.execute("PRAGMA synchronous = OFF")
        for t in TABLES:
            cols = ", ".join(f'"{c}" {sql_type(c, ty)}' for c, ty in schema[t])
            con.execute(f'CREATE TABLE "{t}" ({cols})')
        con.execute("CREATE TABLE _source (key TEXT, value TEXT)")
        con.executemany("INSERT INTO _source VALUES (?, ?)", list(SOURCE.items()))
        cons[st] = con

    for t in TABLES:
        t0 = time.time()
        cols = schema[t]
        conv = [converter(c, ty) for c, ty in cols]
        rows = {st: [] for st in states}
        with open(src / f"{t}.csv", newline="", encoding="utf-8-sig", errors="replace") as f:   # the files start with a BOM
            rd = csv.reader(f)
            head = next(rd)
            if head != [c for c, _ in cols]:
                raise SystemExit(f"{t}.csv: the header differs from schema.ini: {head[:5]} ... vs {[c for c, _ in cols][:5]} ...")
            by = BY[t]
            ia = head.index("areasymbol") if by is None else None
            ib = head.index(by[0]) if by else None
            for r in rd:
                if by is None:
                    st = r[ia][:2].upper()
                    if st not in rows:
                        continue
                else:
                    v = r[ib]
                    if not v:
                        continue
                    v = int(v)
                    st = next((s for s in states if v in keys[s].get(by[1], ())), None)
                    if st is None:
                        continue
                rows[st].append(tuple(None if x == "" else (cv(x) if cv else x) for x, cv in zip(r, conv)))
        for st in states:
            cons[st].executemany(f'INSERT INTO "{t}" VALUES ({", ".join("?" * len(cols))})', rows[st])
            if t in GIVES:
                col, name = GIVES[t]
                i = head.index(col)
                keys[st][name] = {r[i] for r in rows[st]}
        log(f"{t}: " + ", ".join(f"{st} {len(rows[st])}" for st in states) + f" rows ({time.time() - t0:.0f} s)")

    for st, con in cons.items():
        con.execute("CREATE INDEX mapunit_mukey ON mapunit (mukey)")
        con.execute("CREATE INDEX component_cokey ON component (cokey)")
        con.execute("CREATE INDEX chorizon_chkey ON chorizon (chkey)")
        con.execute("CREATE INDEX laoverlap_key ON laoverlap (lareaovkey)")
        con.execute("CREATE INDEX legend_lkey ON legend (lkey)")
        con.execute("CREATE INDEX sacatalog_area ON sacatalog (areasymbol)")
        con.execute("CREATE INDEX muaggatt_mukey ON muaggatt (mukey)")
        con.execute("CREATE INDEX chtexturegrp_chkey ON chtexturegrp (chkey)")
        con.commit()
        con.close()
        log(f"{st}: {out / f'SQLite_SSURGO_{st}.sqlite'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
