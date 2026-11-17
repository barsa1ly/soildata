#!/usr/bin/env python3
"""fetch_states.py: download NRCS's SQLite SSURGO state packages from the public NRCS Box share and build each state's
static soil survey copy with build_ssurgo.py, one state after another, unattended.

What it does:
  1. Lists the share https://nrcs.app.box.com/v/soils, folder SSURGO / SQLite SSURGO (Geopackages) / States, and saves
     every SQLite_SSURGO_<ST>.7z name, Box file id and size to tools/box_states.json (when the share cannot be listed,
     the last saved listing is used).
  2. For each state given on the command line, in order:
       download  to <work>/dl/SQLite_SSURGO_<ST>.7z; a partial file is resumed with a Range request, the final size must
                 equal the listing's, and a failed try is retried up to 3 times
       extract   with 7-Zip into <work>/unpacked/SQLite_SSURGO_<ST>/, flat: only what build_ssurgo.py reads (*.gpkg,
                 muraster_10m_<ST>.tif with its .ovr, .vat* and .aux.xml files, and _ReadMe.txt for the publication date)
       build     python -B -E -P build_ssurgo.py <ST> --src <that folder> --out <the state's repo folder>
       cleanup   delete the unpacked folder (the .7z is kept)
     A state whose <repo>/<ST>/meta.json exists is skipped (build_ssurgo.py writes meta.json last), unless --rebuild.
     One state's failure does not stop the others.
  Each step writes a line to <work>/fetch_states.log (UTC time, state, step, sizes, any error); 7-Zip's and
  build_ssurgo.py's own output go to <work>/logs/<ST>_7z.log and <work>/logs/<ST>_build.log.

Repo folders (siblings of this repo; a new one is created with tools/build_ssurgo.py, README.md and .nojekyll copied
from this repo; nothing is committed or pushed):
  soildata          TX OK NM LA AR
  soildata-west     CA OR WA NV AZ UT ID MT WY CO
  soildata-central  ND SD NE KS MN IA MO WI IL
  soildata-east     every other state and DC

Usage (Python 3.11+ with requests; run it from this tools folder with -B -E -P: -I would hide rasterio, which
build_ssurgo.py needs, in the user site-packages):
  python -B -E -P fetch_states.py OK NM LA AR      download and build these states, in this order
  python -B -E -P fetch_states.py --list           only save the share listing
  python -B -E -P fetch_states.py OK --rebuild     build OK again although OK/meta.json exists
  --work and --out move the work folder and put every state in one repo folder (for tests).
"""
import argparse
import ctypes
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

# ------------------------------------------------------------------------------------------------------------------
# Where things are
# ------------------------------------------------------------------------------------------------------------------
TOOLS = Path(__file__).resolve().parent     # this repo's tools folder (E:/LocalAi/soildata/tools)
REPO = TOOLS.parent                         # this repo (E:/LocalAi/soildata)
ROOT = REPO.parent                          # the folder that holds the repos (E:/LocalAi)
WORK = ROOT / "soildata-build"              # dl/ (the .7z files), unpacked/, logs/ and fetch_states.log
LISTING = TOOLS / "box_states.json"         # the share's state packages: name, Box file id, size
SEVENZIP = r"C:\Program Files\7-Zip\7z.exe"

# which repo folder (a sibling of this repo) holds which states; every other state and DC go to OTHER_REPO
REPOS = {"soildata": "TX OK NM LA AR".split(),
         "soildata-west": "CA OR WA NV AZ UT ID MT WY CO".split(),
         "soildata-central": "ND SD NE KS MN IA MO WI IL".split()}
OTHER_REPO = "soildata-east"
NEW_REPO_FILES = ("tools/build_ssurgo.py", "README.md", ".nojekyll")   # copied from this repo when missing

# ------------------------------------------------------------------------------------------------------------------
# The NRCS Box share
# ------------------------------------------------------------------------------------------------------------------
SHARE = "https://nrcs.app.box.com/v/soils"            # a folder page is SHARE/folder/<id>, page 2 on is ?page=<n>
SHARE_PATH = ("SSURGO", "SQLite SSURGO (Geopackages)", "States")
# a shared file's download link (vanity_name, not shared_name); it redirects to Box's download server
DOWNLOAD = "https://nrcs.app.box.com/index.php?rm=box_download_shared_file&vanity_name=soils&file_id={id}"
# Box's share pages answer only a browser-like client (anything else gets 404 "sharedNotFound")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/131.0.0.0 Safari/537.36",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}
# each folder page carries its items as JSON: Box.postStreamData = {...};</script>
PAGE_DATA = re.compile(r"Box\.postStreamData\s*=\s*(\{.*?\});\s*</script>", re.S)
STATE_FILE = re.compile(r"SQLite_SSURGO_([A-Z]{2})\.7z")
RETRIES = 3       # a failed download is tried again up to 3 times (4 tries in all), each resuming where it stopped

# what 7-Zip takes from a package (wherever it sits in the archive): the files build_ssurgo.py reads
EXTRACT = ("*.gpkg", "muraster_10m_*.tif", "muraster_10m_*.tif.ovr", "muraster_10m_*.tif.vat*",
           "muraster_10m_*.tif.aux.xml", "_ReadMe.txt")

LOG = WORK / "fetch_states.log"             # set again from --work in main()


# ------------------------------------------------------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------------------------------------------------------
def utc():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(st, step, msg):
    """One progress line (UTC time, state, step, message) to the log file and the console."""
    line = f"{utc()}  {st:<2}  {step:<8}  {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def size(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


def tree_bytes(folder):
    return sum(p.stat().st_size for p in Path(folder).rglob("*") if p.is_file())


def tail(path, n=3):
    """The last n non-empty lines of a log file, on one line (for error messages)."""
    lines = [s.strip() for s in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if s.strip()]
    return " | ".join(lines[-n:])


def pid_alive(pid):
    """True when process pid still runs (Windows; elsewhere assume it does)."""
    if os.name != "nt":
        return True
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    k.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    k.CloseHandle.argtypes = (ctypes.c_void_p,)
    h = k.OpenProcess(0x1000, 0, pid)                  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ctypes.get_last_error() == 5             # access denied: it exists
    code = ctypes.c_ulong()
    ok = k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return bool(ok) and code.value == 259               # STILL_ACTIVE


def take_lock(path):
    """Refuse to run twice at once: two runs would write the same downloads."""
    if path.exists():
        try:
            pid = int(path.read_text().strip() or 0)
        except ValueError:
            pid = 0
        if pid and pid != os.getpid() and pid_alive(pid):
            raise SystemExit(f"fetch_states.py is already running (pid {pid}); stop it, or delete {path} if it is not")
    path.write_text(str(os.getpid()))


def keep_awake():
    """Ask Windows not to sleep while this process runs (Windows drops the request when the process ends)."""
    if os.name == "nt":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED


def repo_for(st):
    for name, states in REPOS.items():
        if st in states:
            return ROOT / name
    return ROOT / OTHER_REPO


def ensure_repo(st, repo):
    """Create a repo folder, with this repo's build tool, README.md and .nojekyll when it lacks them (no git)."""
    for rel in NEW_REPO_FILES:
        dst = repo / rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dst)
            log(st, "repo", f"copied {rel} to {repo}")


# ------------------------------------------------------------------------------------------------------------------
# 1. The share listing
# ------------------------------------------------------------------------------------------------------------------
def box_folder(sess, folder_id=None):
    """Every item of a shared folder (the share's top folder when folder_id is None), over all its pages."""
    url = SHARE if folder_id is None else f"{SHARE}/folder/{folder_id}"
    items, page, pages = [], 1, 1
    while page <= pages:
        r = sess.get(url if page == 1 else f"{url}?page={page}", timeout=60)
        r.raise_for_status()
        m = PAGE_DATA.search(r.text)
        if not m:
            raise RuntimeError(f"no item list in {r.url}")
        data = json.loads(m.group(1))["/app-api/enduserapp/shared-folder"]
        items += data["items"]
        pages = int(data.get("pageCount") or 1)
        page += 1
    return items


def list_states(sess):
    """Walk the share to the States folder; every SQLite_SSURGO_<ST>.7z there with its Box id and size."""
    folder_id = None
    for name in SHARE_PATH:
        items = box_folder(sess, folder_id)
        hit = [it for it in items if it.get("type") == "folder" and it.get("name", "").strip().lower() == name.lower()]
        if not hit:
            raise RuntimeError(f"no folder {name!r} (found: {', '.join(it.get('name', '?') for it in items)})")
        folder_id = hit[0]["id"]
    files = []
    for it in box_folder(sess, folder_id):
        m = STATE_FILE.fullmatch(it.get("name", ""))
        if it.get("type") == "file" and m:
            files.append({"state": m.group(1), "name": it["name"], "id": it["typedID"], "size": int(it.get("itemSize") or 0)})
    files.sort(key=lambda f: f["state"])
    return {"share": SHARE, "path": "/".join(SHARE_PATH), "folderId": folder_id, "listed": utc(),
            "count": len(files), "bytes": sum(f["size"] for f in files), "files": files}


# ------------------------------------------------------------------------------------------------------------------
# 2. One state: download, extract, build
# ------------------------------------------------------------------------------------------------------------------
def download(sess, st, f, dest):
    """Download a listed package to dest, resuming a partial file, until it has the listing's size."""
    total = f["size"]
    if total <= 0:
        raise RuntimeError(f"no size for {f['name']} in the listing")
    url = DOWNLOAD.format(id=f["id"])
    for attempt in range(1, RETRIES + 2):
        have = dest.stat().st_size if dest.exists() else 0
        if have == total:
            log(st, "download", f"have {dest.name} ({size(total)})")
            return
        if have > total:                                # larger than the listing: not this file, start again
            dest.unlink()
            have = 0
        if attempt > 1:
            time.sleep(60 * (attempt - 1))
            try:
                sess.get(SHARE, timeout=60)             # fresh share cookies before trying again
            except requests.RequestException:
                pass
        t0 = time.time()
        try:
            hdrs = {"Accept-Encoding": "identity", **({"Range": f"bytes={have}-"} if have else {})}
            with sess.get(url, headers=hdrs, stream=True, timeout=(30, 300)) as r:
                if r.status_code == 416:                # the server will not resume this: start again next try
                    dest.unlink()
                    raise RuntimeError(f"HTTP 416 resuming at byte {have:,}; deleted the partial file")
                r.raise_for_status()
                if "text/html" in r.headers.get("Content-Type", ""):
                    raise RuntimeError(f"got a web page, not the file (HTTP {r.status_code}, {r.url[:100]})")
                if have and (r.status_code != 206 or not r.headers.get("Content-Range", "").startswith(f"bytes {have}-")):
                    have = 0                            # the whole file came back: write it from the start
                log(st, "download", f"{'resume at ' + size(have) + ' of' if have else 'start:'} {size(total)}, "
                                    f"try {attempt} of {RETRIES + 1}")
                with open(dest, "ab" if have else "wb") as out:
                    for chunk in r.iter_content(chunk_size=1 << 22):
                        out.write(chunk)
            got = dest.stat().st_size
            if got != total:
                raise RuntimeError(f"stopped at {got:,} bytes of {total:,}")
            secs = max(time.time() - t0, 1)
            log(st, "download", f"done: {size(total)} ({size(got - have)} in {secs:.0f} s, {(got - have) / 1e6 / secs:.1f} MB/s)")
            return
        except Exception as e:                          # network, HTTP or size error: try again (resuming)
            log(st, "download", f"ERROR try {attempt} of {RETRIES + 1}: {type(e).__name__}: {e}")
    raise RuntimeError(f"download failed after {RETRIES + 1} tries")


def extract(st, archive, dest, logs):
    """Unpack the files build_ssurgo.py reads from archive into dest (flat), with 7-Zip."""
    if dest.exists():
        shutil.rmtree(dest)                             # left from a stopped or failed run
    dest.mkdir(parents=True)
    t0 = time.time()
    out = logs / f"{st}_7z.log"
    with open(out, "wb") as f:
        rc = subprocess.run([SEVENZIP, "e", str(archive), f"-o{dest}", "-y", "-aoa", "-r", "-bsp0", *EXTRACT],
                            stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL).returncode
    if rc not in (0, 1):                                # 1 = warning, 2 and up = error
        raise RuntimeError(f"7-Zip exit code {rc}: {tail(out)}")
    missing = [n for n in (f"SQLite_SSURGO_{st}.gpkg", f"muraster_10m_{st}.tif") if not (dest / n).exists()]
    if missing:
        have = ", ".join(sorted(p.name for p in dest.iterdir())) or "nothing"
        raise RuntimeError(f"not in the package: {', '.join(missing)} (unpacked: {have})")
    files = [p for p in dest.iterdir() if p.is_file()]
    log(st, "extract", f"done: {len(files)} files, {size(sum(p.stat().st_size for p in files))} in "
                       f"{time.time() - t0:.0f} s" + (f" (7-Zip warning, see {out})" if rc == 1 else ""))


def build(st, src, repo, logs):
    """Run build_ssurgo.py for one state into its repo folder."""
    t0 = time.time()
    out = logs / f"{st}_build.log"
    cmd = [sys.executable, "-B", "-E", "-P", str(TOOLS / "build_ssurgo.py"), st, "--src", str(src), "--out", str(repo)]
    with open(out, "wb") as f:
        rc = subprocess.run(cmd, cwd=TOOLS, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL).returncode
    if rc:
        raise RuntimeError(f"build_ssurgo.py exit code {rc}: {tail(out)}")
    log(st, "build", f"done in {time.time() - t0:.0f} s: {repo / st} ({size(tree_bytes(repo / st))})")


# ------------------------------------------------------------------------------------------------------------------
def main():
    global LOG
    ap = argparse.ArgumentParser(description="Download NRCS SQLite SSURGO state packages from the NRCS Box share and "
                                             "build each state with build_ssurgo.py (see the top of this file).")
    ap.add_argument("states", nargs="*", help="two-letter state codes, built in this order (OK NM LA AR ...)")
    ap.add_argument("--list", action="store_true", help="only save the share listing to tools/box_states.json")
    ap.add_argument("--rebuild", action="store_true", help="also build states whose <repo>/<ST>/meta.json exists")
    ap.add_argument("--work", default=str(WORK), help="folder for dl/, unpacked/, logs/ and the log (default %(default)s)")
    ap.add_argument("--out", help="one repo folder for every state (for tests; default: by region, see the top of this file)")
    ap.add_argument("--detach", action="store_true", help="start this same run as a hidden process outside the caller's "
                                                          "job object (it keeps running after the caller's shell ends), print its pid and return")
    args = ap.parse_args()

    if args.detach:
        cmd = [sys.executable, "-B", "-E", "-P", str(Path(__file__).resolve()), *[a for a in sys.argv[1:] if a != "--detach"]]
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0                              # SW_HIDE: its own console, hidden
        flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        try:                                            # leave the caller's job, so closing the caller does not end it
            p = subprocess.Popen(cmd, cwd=TOOLS, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, startupinfo=si, close_fds=True)
        except OSError as e:                            # the caller's job does not allow that
            print(f"could not leave the caller's job ({e}); started inside it")
            p = subprocess.Popen(cmd, cwd=TOOLS, creationflags=flags, startupinfo=si, close_fds=True)
        print(f"started pid {p.pid}: {subprocess.list2cmdline(cmd)}")
        return 0

    work = Path(args.work).resolve()
    dl, unpacked, logs = work / "dl", work / "unpacked", work / "logs"
    for d in (dl, unpacked, logs):
        d.mkdir(parents=True, exist_ok=True)
    LOG = work / "fetch_states.log"
    states = list(dict.fromkeys(s.upper() for s in args.states))       # upper case, each once, in order
    if not states and not args.list:
        ap.error("give state codes, or --list")
    lock = work / "fetch_states.lock"
    if states:
        take_lock(lock)
        keep_awake()
    try:
        log("--", "start", f"pid {os.getpid()}; states ({len(states)}): {' '.join(states) or '-'}")
        sess = requests.Session()
        sess.headers.update(HEADERS)

        # 1. the listing (the last saved one when the share does not answer)
        try:
            listing = list_states(sess)
            LISTING.write_text(json.dumps(listing, indent=1), encoding="utf-8")
            log("--", "list", f"{listing['count']} state packages, {size(listing['bytes'])}, saved to {LISTING}")
        except Exception as e:
            if not LISTING.exists():
                log("--", "list", f"ERROR {type(e).__name__}: {e}; no saved listing, stopping")
                return 1
            listing = json.loads(LISTING.read_text(encoding="utf-8"))
            log("--", "list", f"ERROR {type(e).__name__}: {e}; using the listing saved {listing.get('listed')}")
            try:
                sess.get(SHARE, timeout=60)             # share cookies for the downloads
            except requests.RequestException:
                pass
        by_state = {f["state"]: f for f in listing["files"]}

        # 2. each state in turn; a failure is logged and the next state goes on
        done, failed, skipped = [], [], []
        for st in states:
            repo = Path(args.out).resolve() if args.out else repo_for(st)
            t0, step = time.time(), "check"
            try:
                if (repo / st / "meta.json").exists() and not args.rebuild:
                    log(st, "skip", f"already built: {repo / st / 'meta.json'} (--rebuild builds it again)")
                    skipped.append(st)
                    continue
                f = by_state.get(st)
                if not f:
                    raise RuntimeError(f"no SQLite_SSURGO_{st}.7z in the listing")
                step = "download"
                archive = dl / f["name"]
                download(sess, st, f, archive)
                step = "extract"
                src = unpacked / f"SQLite_SSURGO_{st}"
                extract(st, archive, src, logs)
                step = "build"
                ensure_repo(st, repo)
                build(st, src, repo, logs)
                step = "cleanup"
                freed = tree_bytes(src)
                shutil.rmtree(src)
                log(st, "cleanup", f"deleted {src} ({size(freed)}); kept {archive}")
                done.append(st)
                log(st, "ok", f"{(time.time() - t0) / 60:.1f} min")
            except Exception as e:
                failed.append(st)
                log(st, step, f"ERROR {type(e).__name__}: {e}")
        log("--", "end", f"built {len(done)}: {' '.join(done) or '-'}; failed {len(failed)}: {' '.join(failed) or '-'}; "
                         f"skipped {len(skipped)}: {' '.join(skipped) or '-'}")
        return 1 if failed else 0
    finally:
        if states and lock.exists() and lock.read_text().strip() == str(os.getpid()):
            lock.unlink()


if __name__ == "__main__":
    sys.exit(main())
