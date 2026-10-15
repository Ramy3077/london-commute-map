# London Commute Map

[![CI](https://github.com/Ramy3077/london-commute-map/actions/workflows/ci.yml/badge.svg)](https://github.com/Ramy3077/london-commute-map/actions/workflows/ci.yml)

Ships pre-built for 4 destinations (Liverpool Street, King's Cross St Pancras, Canary Wharf, Paddington); to rebuild for yours, add it to `DESTINATIONS` in `config.py` and run `uv run build.py --dest your-id` (see [Rebuild for your own destination](#rebuild-for-your-own-destination)).

A map of how long a door-to-desk commute to a London station takes from across the city, with a direct-only view for single-seat rides.

**Live: https://web-production-62771.up.railway.app**

![London Commute Map](docs/screenshot.png)

## What the map shows

- **Colour bands** are door-to-desk public transport time to the star (the destination you pick in the dropdown), including the walk at both ends, for a journey arriving at 09:00 on a weekday. Yellow is the shortest, dark purple the longest. Areas beyond 70 minutes are left unshaded, and the colour is clipped to the postcode districts the build covers rather than ending in a rectangle.
- **Direct only** recolours the map by the best single-seat ride: one tube, rail or bus leg with no changes, walking allowed at either end. Areas with no such ride are faded. It is the view to use when strikes or delays make changes risky.
- **Stations** are drawn with their TfL zone and lines.
- **Districts** are ranked by commute time, with links out to SpareRoom and OpenRent searches for that postcode district. The project holds no rent or listing data; bring your own.

There is no server. The page is `index.html` plus pre-built JSON in `data/`: `base.json` (stations and district shapes, shared) and one `<destination>.json` per destination, fetched when you pick it.

## How the pipeline works

`build.py` produces the files in `data/`. Run `uv run build.py` for every destination, or `uv run build.py --dest kings-cross` for one (dependencies are declared inline, so there is nothing to install first).

1. **H3 grid.** The bounding box in `config.py` is covered with [H3](https://h3geo.org) hexagons at resolution 8. Each hex is one sample point, taken at its centre.
2. **Journey planner sampling.** For every hex centre, and for each destination, the script asks the TfL Journey Planner for trips to it, arriving at the reference time, with two preferences: least time and least interchange. Each hex stores its fastest journey overall and the lines used.
3. **Direct-only computation.** From the journeys returned for a hex, those with at most one non-walking leg count as single-seat rides. The fastest of those is the hex's direct time, and the lines used within ten minutes of it are kept as the "via" list. Hexes with no such journey have no direct time.
4. **Contours.** Hex times are rasterised to a grid, blurred slightly (skipping rivers and parks that have no data), and turned into filled bands with matplotlib. This is done once for all routes and once for direct-only, then clipped to the union of the matched postcode districts (trimmed to an ellipse inside the bounding box).
5. **Districts and stations.** Each hex is reverse-geocoded to a postcode district with OpenStreetMap Nominatim. Districts get their median times and most common lines. District outlines come from open postcode polygons. Station names, zones and lines come from the TfL StopPoint API.
6. **Caching and resume.** Every API response is cached as a small JSON file under `cache/` (not committed), with journeys kept in one folder per destination. A run that is interrupted, rate limited or offline picks up where it stopped, and a re-run with warm caches takes seconds. If a destination's coordinates or the date or time change, that destination's cached journeys are cleared automatically; geocodes and stations are shared.

## Rebuild for your own destination

1. Get a free TfL key at <https://api-portal.tfl.gov.uk>.
2. Set it in your shell. It is read from the environment and never stored in the repo:

   ```sh
   export TFL_APP_KEY=your-tfl-key
   ```

3. Edit `config.py`: add your station to `DESTINATIONS` (`id`, `name`, `lat`, `lng`), adjust `BBOX` for the area if needed, and optionally `COMMUTE_DATE` and `COMMUTE_TIME`. By default the date is the next Monday, because TfL rejects dates more than a week in the past.
4. Run `uv run build.py --dest your-id`. A cold build makes two TfL requests per hex, so it takes a while and is paced to stay inside TfL's rate limit; a re-run resumes from the cache.
5. Serve the folder with any static host (`python3 -m http.server`). `index.html` loads `data/` with `fetch`, so opening it as a file will not work.

## Tests

`pip install -r requirements-dev.txt && pytest` runs the tests for the pure parts of `build.py` (hex generation, direct-route classification, cache-key invalidation). CI runs the same on every push.

## Notes and limits

- Times follow the published timetable, not strike-day reality.
- Hex centres are samples. A hex that straddles a river or park can read slightly off.
- Nominatim is used politely (about one request a second) with a descriptive User-Agent.

## Attribution

- Journey times, stations and zones: **Powered by TfL Open Data**, used under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/). Contains OS data &copy; Crown copyright and database rights.
- Map data and tiles &copy; [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors. Reverse geocoding by OpenStreetMap Nominatim.
- Postcode district outlines: [missinglink/uk-postcode-polygons](https://github.com/missinglink/uk-postcode-polygons).
- Map rendering: [Leaflet](https://leafletjs.com).

## Licence

MIT, see [LICENSE](LICENSE). The data sources above keep their own licences.
