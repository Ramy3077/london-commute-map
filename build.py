# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.31",
#     "h3>=4.1",
#     "numpy>=1.26",
#     "matplotlib>=3.8",
#     "shapely>=2.0",
# ]
# ///
"""Build data.js for the London commute map.

Run:
    export TFL_APP_KEY=...   # required
    export ORS_API_KEY=...   # optional, walking times only
    uv run build.py

Resumable: every API response is cached under ./cache/. A full build from
cold caches takes ~10-15 min (bottleneck: TfL Journey Planner, 2 queries per
hex). Re-runs with warm caches take seconds.
"""

import json
import math
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import h3
import matplotlib
import numpy as np
import requests

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from shapely.geometry import Polygon as SPolygon
from shapely.geometry import shape as sshape

from config import (
    BBOX,
    COMMUTE_DATE,
    COMMUTE_TIME,
    H3_RES,
    DESTINATION,
    ORS_API_KEY,
    TFL_APP_KEY,
)

CACHE = Path("cache")
for sub in ("transit", "walk", "geocode", "stations"):
    (CACHE / sub).mkdir(parents=True, exist_ok=True)

def reset_if_destination_changed():
    """Cached journeys and walks belong to one destination and date. Wipe them
    if the config changed so stale times never leak into a new build."""
    stamp = CACHE / "destination.json"
    now = json.dumps([DESTINATION, COMMUTE_DATE, COMMUTE_TIME], sort_keys=True)
    if stamp.exists() and stamp.read_text() != now:
        for sub in ("transit", "walk"):
            for f in (CACHE / sub).glob("*.json"):
                f.unlink()
        log("Destination or date changed: cleared cached journeys and walks")
    stamp.write_text(now)


def log(msg):
    print(msg, flush=True)


# ---------- H3 cells ----------

def cells_in_bbox():
    step = {8: 0.0025, 9: 0.0008}.get(H3_RES, 0.001)
    cells = set()
    lat = BBOX["south"]
    while lat <= BBOX["north"] + 1e-9:
        lng = BBOX["west"]
        while lng <= BBOX["east"] + 1e-9:
            cells.add(h3.latlng_to_cell(lat, lng, H3_RES))
            lng += step
        lat += step
    return sorted(cells)


# ---------- TfL journeys: duration + interchange info ----------

def _parse_journeys(payload):
    """-> list of (duration, [line names of transit legs])."""
    out = []
    for j in payload.get("journeys") or []:
        legs = []
        for leg in j.get("legs", []):
            mode = leg.get("mode", {}).get("id", "")
            if mode == "walking":
                continue
            name = ""
            opts = leg.get("routeOptions") or []
            if opts:
                name = opts[0].get("name") or ""
            if mode == "bus":
                name = f"Bus {name}".strip()
            elif not name:
                name = mode
            legs.append(name)
        out.append((j["duration"], legs))
    return out


def fetch_transit(h3_idx, lat, lng):
    """Best journey overall + best direct (<=1 transit leg) journey."""
    cache_file = CACHE / "transit" / f"{h3_idx}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text()), True

    src = f"{lat:.5f},{lng:.5f}"
    dst = f"{DESTINATION['lat']:.5f},{DESTINATION['lng']:.5f}"
    url = f"https://api.tfl.gov.uk/Journey/JourneyResults/{src}/to/{dst}"
    base = {
        "app_key": TFL_APP_KEY,
        "date": COMMUTE_DATE,
        "time": COMMUTE_TIME,
        "timeIs": "Arriving",
    }

    journeys = []
    unreachable = False
    for pref in ("LeastTime", "LeastInterchange"):
        params = dict(base, journeyPreference=pref)
        for attempt in range(6):
            try:
                r = requests.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                # Ride out short network/DNS outages rather than burning cells.
                log(f"    retry {h3_idx} ({type(exc).__name__})")
                time.sleep(3 + attempt * 5)
                continue
            if r.status_code == 200:
                journeys.extend(_parse_journeys(r.json()))
                break
            if r.status_code == 300:
                unreachable = True  # ambiguous point (park/river)
                break
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 + attempt * 2)
                continue
            log(f"    tfl {r.status_code} for {h3_idx}: {r.text[:100]}")
            return None, False
        else:
            return None, False
        if unreachable:
            break

    if unreachable or not journeys:
        result = {"any": 999, "direct": None, "via": []}
    else:
        best_any = min(d for d, _ in journeys)
        directs = [(d, legs) for d, legs in journeys if len(legs) <= 1]
        if directs:
            best_direct = min(d for d, _ in directs)
            via = []
            for d, legs in directs:
                if d <= best_direct + 10:
                    via.extend(legs if legs else ["Walk"])
            via = sorted(set(via))
        else:
            best_direct, via = None, []
        result = {"any": best_any, "direct": best_direct, "via": via}

    cache_file.write_text(json.dumps(result))
    return result, False


TRANSIT_WORKERS = 8  # ~100 req/min at ~4.5s per TfL call; limit is 500/min


def fetch_all_transit(cells_ll):
    missing = [c for c in cells_ll if not (CACHE / "transit" / f"{c[0]}.json").exists()]
    log(f"TfL journeys: {len(cells_ll) - len(missing)} cached, "
        f"{len(missing)} to fetch ({TRANSIT_WORKERS} workers)")
    done = 0
    with ThreadPoolExecutor(max_workers=TRANSIT_WORKERS) as ex:
        for _ in ex.map(lambda c: fetch_transit(*c), missing):
            done += 1
            if done % 50 == 0:
                log(f"  {done}/{len(missing)}")


# ---------- ORS walking matrix ----------

WALK_BATCH = 500


def fetch_walk_matrix(cell_ids):
    if not ORS_API_KEY:
        log("No ORS_API_KEY set: skipping walking times (not shown on the map)")
        return
    walk_dir = CACHE / "walk"
    missing = [h for h in cell_ids if not (walk_dir / f"{h}.json").exists()]
    if not missing:
        log(f"Walk cache complete ({len(cell_ids)} cells)")
        return
    log(f"Walking times: {len(missing)} cells via ORS matrix...")
    for i in range(0, len(missing), WALK_BATCH):
        batch = missing[i:i + WALK_BATCH]
        locations = [[DESTINATION["lng"], DESTINATION["lat"]]]
        for h in batch:
            lat, lng = h3.cell_to_latlng(h)
            locations.append([lng, lat])
        body = {
            "locations": locations,
            "sources": [0],
            "destinations": list(range(1, len(locations))),
            "metrics": ["duration"],
        }
        r = requests.post(
            "https://api.openrouteservice.org/v2/matrix/foot-walking",
            json=body,
            headers={"Authorization": ORS_API_KEY},
            timeout=120,
        )
        if r.status_code != 200:
            log(f"  ORS {r.status_code}: {r.text[:120]}: walk times skipped")
            return
        durations = r.json()["durations"][0]
        for h, sec in zip(batch, durations):
            val = round(sec / 60) if sec is not None else 999
            (walk_dir / f"{h}.json").write_text(json.dumps({"walk_min": val}))
        time.sleep(2)


def walk_min_for_cell(h3_idx):
    f = CACHE / "walk" / f"{h3_idx}.json"
    if f.exists():
        return json.loads(f.read_text())["walk_min"]
    return None


# ---------- Nominatim reverse geocoding ----------

def fetch_geocodes(cells_ll):
    missing = [c for c in cells_ll if not (CACHE / "geocode" / f"{c[0]}.json").exists()]
    log(f"Geocodes: {len(cells_ll) - len(missing)} cached, {len(missing)} to fetch")
    for i, (h3_idx, lat, lng) in enumerate(missing):
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 14},
            headers={"User-Agent": "london-commute-map/1.0 (open-source hobby project)"},
            timeout=30,
        )
        code, name = "", ""
        if r.status_code == 200:
            addr = r.json().get("address", {})
            pc = addr.get("postcode", "")
            m = re.match(r"([A-Z]{1,2}\d{1,2})", pc.replace(" ", ""))
            if m:
                code = m.group(1)
            name = (
                addr.get("suburb")
                or addr.get("neighbourhood")
                or addr.get("quarter")
                or addr.get("town")
                or ""
            )
        (CACHE / "geocode" / f"{h3_idx}.json").write_text(
            json.dumps({"district_code": code, "district_name": name})
        )
        if i % 20 == 0 and i:
            log(f"  {i}/{len(missing)}")
        time.sleep(1.1)


def geocode_for_cell(h3_idx):
    f = CACHE / "geocode" / f"{h3_idx}.json"
    if f.exists():
        return json.loads(f.read_text())
    return {"district_code": "", "district_name": ""}


# ---------- TfL stations with zones ----------

STATION_MODES = ["tube", "dlr", "overground", "elizabeth-line"]


def fetch_stations():
    cache_file = CACHE / "stations" / "stations.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())

    by_name = {}
    for mode in STATION_MODES:
        page = 1
        while True:
            r = requests.get(
                f"https://api.tfl.gov.uk/StopPoint/Mode/{mode}",
                params={"app_key": TFL_APP_KEY, "page": page},
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            stops = data.get("stopPoints", [])
            for sp in stops:
                if sp.get("stopType") not in ("NaptanMetroStation", "NaptanRailStation"):
                    continue
                zone = next(
                    (p["value"] for p in sp.get("additionalProperties", []) if p["key"] == "Zone"),
                    "",
                )
                name = sp["commonName"].replace(" Underground Station", "").replace(
                    " Rail Station", ""
                ).replace(" DLR Station", "").replace(" Station", "")
                lines = [l["name"] for l in sp.get("lines", [])]
                entry = by_name.get(name)
                if entry is None:
                    by_name[name] = {
                        "name": name, "lat": sp["lat"], "lng": sp["lon"],
                        "zone": zone, "lines": lines,
                    }
                else:
                    entry["lines"] = sorted(set(entry["lines"]) | set(lines))
                    if not entry["zone"] and zone:
                        entry["zone"] = zone
            if len(stops) < 1000:
                break
            page += 1
        log(f"  stations: {mode} done ({len(by_name)} total)")
        time.sleep(0.5)

    stations = [
        s for s in by_name.values()
        if BBOX["south"] <= s["lat"] <= BBOX["north"]
        and BBOX["west"] <= s["lng"] <= BBOX["east"]
    ]
    cache_file.write_text(json.dumps(stations))
    return stations


def district_rings(cell_ids):
    """Outer rings ([lng, lat]) of the district's merged hex polygon."""
    geo = h3.cells_to_geo(cell_ids)
    coords = geo["coordinates"]
    if geo["type"] == "Polygon":
        coords = [coords]
    return [poly[0] for poly in coords]


# ---------- isochrone contour bands ----------

LEVELS = [0, 20, 30, 40, 50, 70]
GRID_NX, GRID_NY = 260, 220


def _conv(a, k, axis):
    pad = len(k) // 2
    ap = np.pad(a, [(pad, pad) if ax == axis else (0, 0) for ax in range(2)], mode="edge")
    return np.apply_along_axis(lambda m: np.convolve(m, k, mode="valid"), axis, ap)


def _blur(a, passes=2):
    """Gaussian-ish blur that respects missing data (rivers, parks)."""
    k = np.array([1.0, 4.0, 6.0, 4.0, 1.0])
    k /= k.sum()
    v = np.nan_to_num(a)
    w = np.isfinite(a).astype(float)
    for _ in range(passes):
        for axis in (0, 1):
            v = _conv(v, k, axis)
            w = _conv(w, k, axis)
    return np.divide(v, w, out=np.full_like(v, np.nan), where=w > 0.35)


def _latlng_ring(coords):
    return [[round(float(y), 5), round(float(x), 5)] for x, y in coords]


def _assemble_polys(rings):
    """Group matplotlib path rings into [outer, hole, ...] polygons."""
    candidates = []
    for r in rings:
        if len(r) < 4:
            continue
        p = SPolygon(r)
        if not p.is_valid:
            p = p.buffer(0)
        if p.is_empty or p.area < 1e-7:
            continue
        candidates.append((p, r))
    candidates.sort(key=lambda t: -t[0].area)

    outers = []  # [shapely poly, ring, holes]
    for p, r in candidates:
        for o in outers:
            if o[0].contains(p.representative_point()):
                o[2].append(r)
                break
        else:
            outers.append([p, r, []])

    result = []
    for _, outer, holes in outers:
        poly = SPolygon(outer, holes).simplify(0.0006, preserve_topology=True)
        if poly.is_empty:
            continue
        geoms = poly.geoms if hasattr(poly, "geoms") else [poly]
        for g in geoms:
            result.append(
                [_latlng_ring(g.exterior.coords)]
                + [_latlng_ring(h.coords) for h in g.interiors]
            )
    return result


def _bands(grid, lngs, lats):
    z = np.ma.masked_invalid(_blur(grid))
    fig = plt.figure()
    cs = fig.add_subplot().contourf(lngs, lats, z, levels=LEVELS)
    bands = []
    paths = cs.get_paths()  # matplotlib >= 3.8: one Path per level interval
    for path, lo, hi in zip(paths, LEVELS[:-1], LEVELS[1:]):
        polys = _assemble_polys(path.to_polygons(closed_only=False))
        bands.append({"lo": lo, "hi": hi, "polys": polys})
    plt.close(fig)
    return bands


def build_contours(cells):
    lngs = np.linspace(BBOX["west"], BBOX["east"], GRID_NX)
    lats = np.linspace(BBOX["south"], BBOX["north"], GRID_NY)
    by_h3 = {c["h3"]: c for c in cells}
    any_g = np.full((GRID_NY, GRID_NX), np.nan)
    dir_g = np.full((GRID_NY, GRID_NX), np.nan)
    for i, la in enumerate(lats):
        for j, lo in enumerate(lngs):
            cell = by_h3.get(h3.latlng_to_cell(la, lo, H3_RES))
            if cell is None:
                continue
            if cell["ta"] < 999:
                any_g[i, j] = cell["ta"]
            if cell["td"] is not None:
                dir_g[i, j] = cell["td"]
    return {"any": _bands(any_g, lngs, lats), "direct": _bands(dir_g, lngs, lats)}


# ---------- postcode district boundaries ----------

BOUNDARY_URL = "https://raw.githubusercontent.com/missinglink/uk-postcode-polygons/master/geojson/{}.geojson"


def fetch_boundaries(codes):
    (CACHE / "boundaries").mkdir(exist_ok=True)
    areas = sorted({re.match(r"[A-Z]+", c).group(0) for c in codes if re.match(r"[A-Z]+", c)})
    feats = {}
    for area in areas:
        f = CACHE / "boundaries" / f"{area}.geojson"
        if not f.exists():
            r = requests.get(BOUNDARY_URL.format(area), timeout=60)
            if r.status_code != 200:
                log(f"  boundaries: HTTP {r.status_code} for area {area}")
                continue
            f.write_text(r.text)
        for feat in json.loads(f.read_text())["features"]:
            feats[feat["properties"]["name"]] = feat["geometry"]

    out = {}
    parents_used = set()
    # exact matches first so prefix fallbacks don't steal them
    for code in sorted(codes, key=lambda c: (c not in feats, c)):
        geom = feats.get(code)
        if geom is None:
            parent = re.sub(r"[A-Z]$", "", code)
            if parent in feats and parent not in parents_used and parent not in codes:
                geom = feats[parent]
                parents_used.add(parent)
        if geom is None:
            continue  # frontend falls back to the hex-blob rings
        g = sshape(geom).simplify(0.0004, preserve_topology=True)
        geoms = g.geoms if hasattr(g, "geoms") else [g]
        polys = []
        for p in geoms:
            if p.is_empty:
                continue
            polys.append(
                [_latlng_ring(p.exterior.coords)]
                + [_latlng_ring(h.coords) for h in p.interiors]
            )
        if polys:
            out[code] = polys
    log(f"  boundaries: {len(out)}/{len(codes)} districts matched")
    return out


# ---------- main pipeline ----------

def main():
    t0 = time.time()
    if not TFL_APP_KEY:
        raise SystemExit("Set TFL_APP_KEY (free from https://api-portal.tfl.gov.uk)")
    reset_if_destination_changed()
    cell_ids = cells_in_bbox()
    cells_ll = [(h, *h3.cell_to_latlng(h)) for h in cell_ids]
    log(f"{len(cell_ids)} H3 cells at res {H3_RES}")

    fetch_geocodes(cells_ll)
    fetch_all_transit(cells_ll)
    fetch_walk_matrix(cell_ids)
    stations = fetch_stations()
    log(f"{len(stations)} stations in bbox")

    # Assemble cells
    cells = []
    for h3_idx, lat, lng in cells_ll:
        transit, _ = fetch_transit(h3_idx, lat, lng)
        geo = geocode_for_cell(h3_idx)
        cells.append({
            "h3": h3_idx,
            "ta": transit["any"] if transit else 999,
            "td": transit["direct"] if transit else None,
            "via": transit["via"] if transit else [],
            "walk": walk_min_for_cell(h3_idx),
            "dc": geo["district_code"],
            "dn": geo["district_name"],
        })

    # Districts
    by_code = {}
    for c in cells:
        if c["dc"]:
            by_code.setdefault(c["dc"], []).append(c)

    districts = {}
    for code in sorted(by_code):
        group = by_code[code]
        ids = [c["h3"] for c in group]
        names = [c["dn"] for c in group if c["dn"]]
        name = statistics.mode(names) if names else code
        tas = sorted(c["ta"] for c in group if c["ta"] < 999)
        tds = sorted(c["td"] for c in group if c["td"] is not None)
        via_count = {}
        for c in group:
            for v in c["via"]:
                via_count[v] = via_count.get(v, 0) + 1
        top_via = [v for v, _ in sorted(via_count.items(), key=lambda kv: -kv[1])[:4]]

        districts[code] = {
            "code": code,
            "name": name,
            "ta": tas[len(tas) // 2] if tas else None,
            "td": tds[len(tds) // 2] if tds else None,
            "direct_share": round(len(tds) / len(group), 2),
            "via": top_via,
            "rings": district_rings(ids),
        }

    log("Building isochrone contours...")
    contours = build_contours(cells)
    log("Fetching district boundaries...")
    boundaries = fetch_boundaries(list(districts.keys()))

    data = {
        "destination": DESTINATION,
        "built_at": datetime.now().isoformat(timespec="minutes"),
        "commute_ref": f"arrive {COMMUTE_TIME[:2]}:{COMMUTE_TIME[2:]} Mon",
        "levels": LEVELS,
        "contours": contours,
        "boundaries": boundaries,
        "districts": districts,
        "stations": stations,
    }

    Path("data.json").write_text(json.dumps(data))
    Path("data.js").write_text("const DATA = " + json.dumps(data) + ";")
    log(f"Wrote data.js ({Path('data.js').stat().st_size // 1024} KB) "
        f"in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
