# hanat-data

Mosque locations for the Hanat prayer app, as small files the app downloads on demand.

- `v1/<lat>_<lon>.json` — one file per half-degree area. `lat` and `lon` are
  `floor(latitude * 2)` and `floor(longitude * 2)`. Areas with no mosques have no file.
- Each file is `{"v":1,"m":[[latitude, longitude, name, {translations}, women], ...]}`.
  `name` is empty when the map has none; `women` is `1` only where the map says
  women are provided for.

The files are rebuilt from OpenStreetMap once a month by `tools/build_mosque_tiles.py`
(see `.github/workflows/refresh.yml`). Coverage is only as complete as the map,
and varies a great deal between countries.

## Licence

The data is © OpenStreetMap contributors and is available under the
[Open Database License](https://opendatacommons.org/licenses/odbl/1-0/).
See <https://www.openstreetmap.org/copyright>.
