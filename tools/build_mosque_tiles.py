#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Builds the mosque area files the Hanat app downloads.

Mosques are taken from OpenStreetMap's ready-made country files on Geofabrik
(download.geofabrik.de), one country or region at a time: each file is
downloaded, its places of worship are pulled out with osmium, and the file is
deleted again. Plain file downloads do not time out the way the shared query
service does.

Each region's result is saved under `countries/<region>.json`, so a region that
fails on a given run simply keeps its previous data. The area files in `v1/`
are then rebuilt from everything saved.

    python3 tools/build_mosque_tiles.py                     (every region)
    python3 tools/build_mosque_tiles.py bangladesh maldives (only these; the rest keep their saved data)

Area file name:  v1/<floor(lat*2)>_<floor(lon*2)>.json
Area file body:  {"v":1,"m":[[lat, lon, name, {"en": ..., "bn": ...}, women], ...]}
                 name is "" when the map has none; the names object holds only the
                 translations the map has; women is 1 only for female=yes.

`coverage.json` lists how many mosques each region has.

Data (c) OpenStreetMap contributors, ODbL. Needs Python 3, curl and osmium-tool.
"""
import json
import math
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

INDEX_URL = "https://download.geofabrik.de/index-v1-nogeom.json"
USER_AGENT = "Hanat-data-script (github.com/TahmidRahman/hanat-data)"
TILES_PER_DEGREE = 2
LANGUAGES = ("en", "bn", "ar", "ur")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COUNTRY_DIR = os.path.join(ROOT, "countries")
TILE_DIR = os.path.join(ROOT, "v1")
# Where country files are downloaded to; each is deleted once read. The largest is about 10 GB.
WORK_DIR = os.environ.get("HANAT_WORK", os.path.join(ROOT, ".work"))

# Two downloads at once is polite to Geofabrik and keeps disk use within a few large files.
PARALLEL = int(os.environ.get("HANAT_PARALLEL", "2"))
# Stop starting new regions after this long, so a run always finishes and publishes what it has.
TIME_BUDGET_SECONDS = int(os.environ.get("HANAT_TIME_BUDGET", str(4 * 60 * 60)))

# Done first, so that if a run runs out of time it is these that are fresh: roughly the
# largest Muslim populations first, then the rest in Geofabrik's order.
PRIORITY = """
indonesia pakistan india bangladesh nigeria egypt iran turkey algeria sudan iraq morocco ethiopia
afghanistan gcc-states uzbekistan yemen malaysia-singapore-brunei niger syria mali tunisia kazakhstan
senegal-and-gambia guinea somalia burkina-faso azerbaijan chad jordan china volga-fed-district
north-caucasus-fed-district libya tajikistan sierra-leone kyrgyzstan israel-and-palestine lebanon
mauritania turkmenistan kosovo albania bosnia-herzegovina djibouti maldives comores tanzania kenya
ivory-coast cameroon uganda mozambique ghana eritrea benin togo sri-lanka myanmar philippines thailand
france germany united-kingdom us canada italy spain netherlands belgium sweden austria switzerland
norway denmark australia new-zealand south-africa guyana suriname
""".split()


# Geofabrik's extra groupings, which repeat countries or states listed on their own.
GROUPINGS = set("""
alps dach britain-and-ireland great-britain sea south-africa-and-lesotho kaliningrad canary-islands
us-midwest us-northeast us-pacific us-south us-west
""".split())


def regions_from_index():
    """Every region directly under a continent, leaving out Geofabrik's overlapping extra groupings."""
    raw = subprocess.run(["curl", "-sS", "--fail", "-A", USER_AGENT, INDEX_URL], capture_output=True, check=True).stdout
    entries = [feature["properties"] for feature in json.loads(raw)["features"]]
    by_id = {e["id"]: e for e in entries}
    continents = {e["id"] for e in entries if not e.get("parent")}
    regions = {}
    for e in entries:
        parent = by_id.get(e.get("parent"))
        # Only regions directly under a continent; states and provinces have a "/" in their name.
        if parent is None or parent["id"] not in continents or "/" in e["id"] or e["id"] in GROUPINGS:
            continue
        # Groupings such as "dach" or "alps" repeat countries that are listed on their own.
        if e["id"] in parent.get("special_regions", []):
            continue
        regions[e["id"]] = e["urls"]["pbf"]
    # Regions with no saved result yet come first, so ones left over by a run that ran out of
    # time are not left over again; then the priority list; then the rest.
    def missing(r):
        return not os.path.exists(os.path.join(COUNTRY_DIR, r + ".json"))
    first = sorted(r for r in regions if missing(r))
    rest = [r for r in PRIORITY if r in regions and not missing(r)]
    rest += sorted(r for r in regions if r not in PRIORITY and not missing(r))
    ordered = first + rest
    return [(r, regions[r]) for r in ordered]


class Failed(Exception):
    pass


def run(command, what):
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        raise Failed("%s: %s" % (what, result.stderr.decode("utf-8", "replace").strip()[-300:]))


def centre(geometry):
    """A point for the mosque: the point itself, or the average of a building outline's corners."""
    kind, coords = geometry["type"], geometry["coordinates"]
    if kind == "Point":
        return coords
    if kind == "LineString":
        points = coords
    elif kind == "Polygon":
        points = coords[0]
    elif kind == "MultiPolygon":
        points = [p for polygon in coords for p in polygon[0]]
    else:
        return None
    if not points:
        return None
    return [sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)]


def to_record(tags, lon, lat):
    name = (tags.get("name") or tags.get("name:en") or "").strip()
    names = {}
    for language in LANGUAGES:
        value = (tags.get("name:" + language) or "").strip()
        if value and value != name:
            names[language] = value
    women = 1 if tags.get("female") == "yes" else 0
    return [round(lat, 5), round(lon, 5), name, names, women]


def key_of(record):
    # The same mosque is often mapped as both a point and a building, and
    # neighbouring country files overlap a little at the borders.
    return (record[2], round(record[0], 3), round(record[1], 3))


def fetch_region(region, url):
    """All mosques in one Geofabrik region, as a list of records."""
    folder = os.path.join(WORK_DIR, region)
    os.makedirs(folder, exist_ok=True)
    whole = os.path.join(folder, "whole.osm.pbf")
    worship = os.path.join(folder, "worship.osm.pbf")
    features = os.path.join(folder, "worship.geojsonseq")
    try:
        for attempt in range(1, 4):
            try:
                run(["curl", "-sS", "--fail", "-L", "-A", USER_AGENT, "--retry", "2", "-o", whole, url], "download")
                break
            except Failed:
                if attempt == 3:
                    raise
                time.sleep(60 * attempt)
        # Places of worship with every node their outlines need; religion is checked below.
        run(["osmium", "tags-filter", "--overwrite", "-o", worship, whole, "nwr/amenity=place_of_worship"], "filter")
        os.remove(whole)
        run(["osmium", "export", "--overwrite", "-f", "geojsonseq", "-o", features,
             "--geometry-types=point,linestring,polygon", worship], "export")
        records = {}
        with open(features, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip().lstrip("\x1e")
                if not line:
                    continue
                feature = json.loads(line)
                tags = feature.get("properties") or {}
                if tags.get("religion") != "muslim":
                    continue
                point = centre(feature["geometry"])
                if point:
                    record = to_record(tags, point[0], point[1])
                    records.setdefault(key_of(record), record)
        return sorted(records.values(), key=lambda r: (r[0], r[1]))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def rebuild_tiles():
    merged = {}
    coverage = {}
    for file_name in sorted(os.listdir(COUNTRY_DIR)):
        if not file_name.endswith(".json"):
            continue
        with open(os.path.join(COUNTRY_DIR, file_name), encoding="utf-8") as handle:
            records = json.load(handle)
        coverage[file_name[:-5]] = len(records)
        for record in records:
            merged.setdefault(key_of(record), record)

    # Never replace good published files with an empty or badly shrunken set.
    previous = 0
    coverage_path = os.path.join(ROOT, "coverage.json")
    if os.path.exists(coverage_path):
        with open(coverage_path, encoding="utf-8") as handle:
            previous = json.load(handle).get("total", 0)
    if not merged or len(merged) < previous * 0.7:
        print("Only %d mosques gathered against %d published; leaving the area files as they are."
              % (len(merged), previous))
        return

    tiles = {}
    for record in merged.values():
        tile = (math.floor(record[0] * TILES_PER_DEGREE), math.floor(record[1] * TILES_PER_DEGREE))
        tiles.setdefault(tile, []).append(record)

    os.makedirs(TILE_DIR, exist_ok=True)
    # Start clean so an area that lost its last mosque does not keep a stale file.
    for old in os.listdir(TILE_DIR):
        if old.endswith(".json"):
            os.remove(os.path.join(TILE_DIR, old))
    total_bytes = 0
    for (tile_lat, tile_lon), mosques in tiles.items():
        mosques.sort(key=lambda r: (r[0], r[1]))
        path = os.path.join(TILE_DIR, "%d_%d.json" % (tile_lat, tile_lon))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"v": 1, "m": mosques}, handle, ensure_ascii=False, separators=(",", ":"))
        total_bytes += os.path.getsize(path)

    with open(coverage_path, "w", encoding="utf-8") as handle:
        json.dump({"total": len(merged), "countries": coverage}, handle, indent=1, sort_keys=True)

    largest = max((len(m) for m in tiles.values()), default=0)
    print("Area files: %d mosques from %d regions in %d files, %.1f MB; largest file holds %d."
          % (len(merged), sum(1 for n in coverage.values() if n), len(tiles), total_bytes / 1e6, largest))


def main():
    regions = regions_from_index()
    wanted = [name.lower() for name in sys.argv[1:]]
    if wanted:
        unknown = [w for w in wanted if w not in dict(regions)]
        if unknown:
            print("Not Geofabrik region names: %s" % " ".join(unknown))
            return 1
        regions = [(r, u) for r, u in regions if r in wanted]
    os.makedirs(COUNTRY_DIR, exist_ok=True)
    started = time.time()
    done, failed, skipped = [], [], []

    def work(region, url):
        if time.time() - started > TIME_BUDGET_SECONDS:
            return region, None, "skipped"
        try:
            return region, fetch_region(region, url), None
        except (Failed, OSError, ValueError) as error:
            return region, None, str(error)

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        futures = [pool.submit(work, region, url) for region, url in regions]
        for future in as_completed(futures):
            region, records, problem = future.result()
            if problem == "skipped":
                skipped.append(region)
                continue
            if problem:
                # Skip it and carry on; this region keeps whatever data it had before.
                failed.append(region)
                print("%s  FAILED, keeping earlier data (%s)" % (region, problem), flush=True)
                continue
            with open(os.path.join(COUNTRY_DIR, region + ".json"), "w", encoding="utf-8") as handle:
                json.dump(records, handle, ensure_ascii=False, separators=(",", ":"))
            done.append(region)
            print("%s  %d mosques  (%d/%d done, %.0f min)"
                  % (region, len(records), len(done), len(regions), (time.time() - started) / 60), flush=True)

    rebuild_tiles()
    print("Refreshed %d regions in %.0f minutes." % (len(done), (time.time() - started) / 60))
    if failed:
        print("Failed this time, kept earlier data (%d): %s" % (len(failed), " ".join(sorted(failed))))
    if skipped:
        print("Out of time, left for the next run (%d): %s" % (len(skipped), " ".join(sorted(skipped))))


if __name__ == "__main__":
    sys.exit(main())
