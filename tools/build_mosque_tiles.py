#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Builds the mosque area files the Hanat app downloads.

Mosques are read from OpenStreetMap through the Overpass service, one country
at a time. Each country's result is saved under `countries/`, so a country that
fails on a given day simply keeps its previous data. The area files in `v1/` are
then rebuilt from everything saved.

    python3 tools/build_mosque_tiles.py                 (every country)
    python3 tools/build_mosque_tiles.py BD ID TR        (only these; the rest keep their saved data)

Area file name:  v1/<floor(lat*2)>_<floor(lon*2)>.json
Area file body:  {"v":1,"m":[[lat, lon, name, {"en": ..., "bn": ...}, women], ...]}
                 name is "" when the map has none; the names object holds only the
                 translations the map has; women is 1 only for female=yes.

`coverage.json` lists how many mosques the map has for each country.

Data (c) OpenStreetMap contributors, ODbL. Needs only Python 3 and curl.
"""
import json
import math
import os
import subprocess
import sys
import time

ENDPOINT = "https://overpass-api.de/api/interpreter"
FILTER = 'nwr["amenity"="place_of_worship"]["religion"="muslim"]'
TILES_PER_DEGREE = 2
LANGUAGES = ("en", "bn", "ar", "ur")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COUNTRY_DIR = os.path.join(ROOT, "countries")
TILE_DIR = os.path.join(ROOT, "v1")

# ISO 3166-1 codes, plus XK (Kosovo), which the map uses.
COUNTRIES = """
AD AE AF AG AI AL AM AO AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BW BY BZ CA CC
CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA
GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HN HR HT HU ID IE IL IM IN IQ IR IS IT JE JM JO JP KE
KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS
MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO
RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TL TM TN TO TR TT TV
TW TZ UA UG US UY UZ VA VC VE VG VI VN VU WF WS XK YE YT ZA ZM ZW
""".split()


class TooLarge(Exception):
    """The service answered, but the request covers more than it can return at once."""


class Unavailable(Exception):
    """The service would not answer after several tries."""


def run_query(query, attempts=4, max_seconds=400):
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
        wait = 45 * attempt
        print("    attempt %d failed (%s); waiting %ds" % (attempt, problem, wait), flush=True)
        time.sleep(wait)
    raise Unavailable()


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


def key_of(record):
    # The same mosque is often mapped as both a point and a building.
    return (record[2], round(record[0], 3), round(record[1], 3))


def fetch_box(south, west, north, east, records):
    """Fetches one rectangle, splitting it into quarters when it is too much for one request."""
    query = "[out:json][timeout:170];%s(%s,%s,%s,%s);out center tags;" % (FILTER, south, west, north, east)
    try:
        for element in run_query(query, max_seconds=200):
            record = to_record(element)
            if record:
                records.setdefault(key_of(record), record)
        time.sleep(2)
        return
    except TooLarge:
        if north - south <= 0.25:
            raise
    mid_lat, mid_lon = (south + north) / 2, (west + east) / 2
    for box in ((south, west, mid_lat, mid_lon), (south, mid_lon, mid_lat, east),
                (mid_lat, west, north, mid_lon), (mid_lat, mid_lon, north, east)):
        fetch_box(*box, records)


def fetch_country(code):
    """All mosques inside one country, as a list of records."""
    area = 'area["ISO3166-1"="%s"]->.c;' % code
    records = {}
    try:
        elements = run_query("[out:json][timeout:300][maxsize:1500000000];%s%s(area.c);out center tags;" % (area, FILTER))
        for element in elements:
            record = to_record(element)
            if record:
                records.setdefault(key_of(record), record)
    except TooLarge as reason:
        # A country with very many mosques: find its outline box and fetch that in pieces.
        # The pieces may include a few mosques just across the border; they are merged away later.
        print("    too large in one request (%s); fetching in pieces" % reason, flush=True)
        outline = run_query("[out:json][timeout:60];%srel(pivot.c);out bb;" % area, max_seconds=90)
        boxes = [e["bounds"] for e in outline if "bounds" in e]
        if not boxes:
            raise Unavailable()
        for b in boxes:
            step = 4.0
            lat = b["minlat"]
            while lat < b["maxlat"]:
                lon = b["minlon"]
                while lon < b["maxlon"]:
                    fetch_box(lat, lon, min(lat + step, b["maxlat"]), min(lon + step, b["maxlon"]), records)
                    lon += step
                lat += step
    return sorted(records.values(), key=lambda r: (r[0], r[1]))


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
    print("Area files: %d mosques from %d countries in %d files, %.1f MB; largest file holds %d."
          % (len(merged), sum(1 for n in coverage.values() if n), len(tiles), total_bytes / 1e6, largest))


def main():
    wanted = [code.upper() for code in sys.argv[1:]] or COUNTRIES
    os.makedirs(COUNTRY_DIR, exist_ok=True)
    failed = []
    down_in_a_row = 0
    for index, code in enumerate(wanted, 1):
        print("[%d/%d] %s" % (index, len(wanted), code), flush=True)
        try:
            records = fetch_country(code)
        except (Unavailable, TooLarge):
            failed.append(code)
            down_in_a_row += 1
            print("    FAILED; keeping any earlier data for %s" % code, flush=True)
            if down_in_a_row >= 5:
                # The service is down, not this country: stop asking, keep what was gathered.
                failed += wanted[index:]
                print("  Five failures in a row; stopping early.", flush=True)
                break
            continue
        down_in_a_row = 0
        with open(os.path.join(COUNTRY_DIR, code + ".json"), "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False, separators=(",", ":"))
        print("    %d mosques" % len(records), flush=True)
        time.sleep(3)  # be polite to the shared service

    rebuild_tiles()
    if failed:
        print("Not refreshed this time (%d): %s" % (len(failed), " ".join(failed)))


if __name__ == "__main__":
    sys.exit(main())
