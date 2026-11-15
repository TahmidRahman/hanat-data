#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Builds the mosque area files the app downloads.

Mosques are read from OpenStreetMap through the Overpass service, one region at a
time, and written as small files that each cover half a degree of latitude and
longitude. The app fetches only the files around the user.

    python3 tools/build_mosque_tiles.py --out v1                               (whole world)
    python3 tools/build_mosque_tiles.py --out v1 --bbox 20.5 88.0 26.7 92.7    (one region)

File name:  <floor(lat*2)>_<floor(lon*2)>.json
File body:  {"v":1,"m":[[lat, lon, name, {"en": ..., "bn": ...}, women], ...]}
            name is "" when the map has none; the names object holds only the
            translations the map has; women is 1 only for female=yes.

Data (c) OpenStreetMap contributors, ODbL. Needs only Python 3 and curl.
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time

ENDPOINT = "https://overpass-api.de/api/interpreter"
TILES_PER_DEGREE = 2
LANGUAGES = ("en", "bn", "ar", "ur")


class TooLarge(Exception):
    """The service answered, but the area holds more than one request can return."""


def run_query(query, attempts, max_seconds):
    """Returns the elements. Raises TooLarge if the area must be split, and stops
    the whole run if the service stays unavailable, so a bad day never publishes
    half the world."""
    for attempt in range(1, attempts + 1):
        # curl is used for the download because the Python bundled with macOS cannot
        # complete a secure connection to this server.
        result = subprocess.run(
            ["curl", "-sS", "--fail", "-m", str(max_seconds), "-A", "Hanat-data-script",
             "--data-urlencode", "data=" + query, ENDPOINT],
            capture_output=True,
        )
        if result.returncode == 28:
            raise TooLarge("no answer within %ds" % max_seconds)
        if result.returncode == 0:
            try:
                reply = json.loads(result.stdout.decode("utf-8"))
                # A query that ran out of time or memory still replies, with a remark and partial data.
                if reply.get("remark"):
                    raise TooLarge(reply["remark"])
                return reply["elements"]
            except (ValueError, KeyError) as error:
                problem = "unreadable reply: %s" % error
        else:
            problem = result.stderr.decode("utf-8", "replace").strip() or "curl exit %d" % result.returncode
        wait = 60 * attempt
        print("  attempt %d failed (%s); waiting %ds" % (attempt, problem, wait), flush=True)
        time.sleep(wait)
    raise SystemExit("Overpass is not answering; nothing was written. Try again later.")


FILTER = 'nwr["amenity"="place_of_worship"]["religion"="muslim"]'


def fetch_world():
    print("Fetching the whole world in one request", flush=True)
    try:
        return run_query("[out:json][timeout:900][maxsize:2000000000];%s;out center tags;" % FILTER, 2, 1200)
    except TooLarge as reason:
        print("  too much for one request (%s); going area by area" % reason, flush=True)
        return None


def fetch_area(south, west, north, east, add):
    """Fetches one rectangle, splitting it into quarters when it is too much for one request."""
    print("Fetching %.2f,%.2f to %.2f,%.2f" % (south, west, north, east), flush=True)
    query = "[out:json][timeout:170];%s(%s,%s,%s,%s);out center tags;" % (FILTER, south, west, north, east)
    try:
        add(run_query(query, 6, 200))
        time.sleep(3)  # be polite to the shared service
        return
    except TooLarge as reason:
        if north - south <= 0.5:
            raise SystemExit("Area %s,%s,%s,%s cannot be fetched: %s" % (south, west, north, east, reason))
        print("  splitting (%s)" % reason, flush=True)
    mid_lat, mid_lon = (south + north) / 2, (west + east) / 2
    for cell in ((south, west, mid_lat, mid_lon), (south, mid_lon, mid_lat, east),
                 (mid_lat, west, north, mid_lon), (mid_lat, mid_lon, north, east)):
        fetch_area(*cell, add)


def to_record(element):
    position = element if "lat" in element else element.get("center")
    if not position:
        return None
    tags = element.get("tags", {})
    name = (tags.get("name") or tags.get("name:en") or "").strip()
    names = {}
    for language in LANGUAGES:
        value = (tags.get("name:" + language) or "").strip()
        if value and value != name:
            names[language] = value
    women = 1 if tags.get("female") == "yes" else 0
    return [round(position["lat"], 5), round(position["lon"], 5), name, names, women]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"),
                        help="one region only; leave out to fetch the whole world")
    parser.add_argument("--out", required=True, help="folder to write the area files into")
    parser.add_argument("--step", type=float, default=10.0, help="degrees per request when fetching by area")
    args = parser.parse_args()

    records = {}

    def add(elements):
        for element in elements:
            record = to_record(element)
            if record:
                # The same mosque is often mapped as both a point and a building.
                key = (record[2], round(record[0], 3), round(record[1], 3))
                records.setdefault(key, record)
        print("  %d elements, %d mosques so far" % (len(elements), len(records)), flush=True)

    world = None if args.bbox else fetch_world()
    if world is not None:
        add(world)
    else:
        # Either one region was asked for, or the single world request failed: go area by area.
        # Nothing is mapped south of 50 S or north of 72 N.
        south, west, north, east = args.bbox or (-50.0, -180.0, 72.0, 180.0)
        lat = south
        while lat < north:
            lon = west
            while lon < east:
                fetch_area(lat, lon, min(lat + args.step, north), min(lon + args.step, east), add)
                lon += args.step
            lat += args.step

    tiles = {}
    for record in records.values():
        tile = (math.floor(record[0] * TILES_PER_DEGREE), math.floor(record[1] * TILES_PER_DEGREE))
        tiles.setdefault(tile, []).append(record)

    if len(records) < 1000 and not args.bbox:
        raise SystemExit("Only %d mosques fetched; refusing to replace the published files." % len(records))
    os.makedirs(args.out, exist_ok=True)
    # Start clean so an area that lost its last mosque does not keep a stale file.
    for old in os.listdir(args.out):
        if old.endswith(".json"):
            os.remove(os.path.join(args.out, old))
    total_bytes = 0
    for (tile_lat, tile_lon), mosques in tiles.items():
        mosques.sort(key=lambda r: (r[0], r[1]))
        path = os.path.join(args.out, "%d_%d.json" % (tile_lat, tile_lon))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"v": 1, "m": mosques}, handle, ensure_ascii=False, separators=(",", ":"))
        total_bytes += os.path.getsize(path)

    named = sum(1 for r in records.values() if r[2])
    women = sum(1 for r in records.values() if r[4])
    largest = max((len(m) for m in tiles.values()), default=0)
    print("Wrote %d mosques (%d named, %d with a women's area) into %d files, %.1f MB; largest file holds %d."
          % (len(records), named, women, len(tiles), total_bytes / 1e6, largest))


if __name__ == "__main__":
    sys.exit(main())
