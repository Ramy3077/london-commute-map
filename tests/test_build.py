"""Tests for the pure parts of build.py: no network, no TfL key."""

import h3

import build

SMALL_BBOX = {"north": 51.53, "south": 51.50, "west": -0.10, "east": -0.06}
DEST = {"id": "x", "name": "Somewhere", "lat": 51.5178, "lng": -0.0823}


# ---------- hex generation ----------

def test_cells_cover_every_corner_and_centre():
    cells = set(build.cells_in_bbox(SMALL_BBOX, 8))
    for lat, lng in [(51.50, -0.10), (51.53, -0.06), (51.515, -0.08), (51.50, -0.06)]:
        assert h3.latlng_to_cell(lat, lng, 8) in cells


def test_cells_are_unique_sorted_valid_and_near_the_box():
    cells = build.cells_in_bbox(SMALL_BBOX, 8)
    assert cells == sorted(set(cells))
    assert all(h3.is_valid_cell(c) and h3.get_resolution(c) == 8 for c in cells)
    for c in cells:
        lat, lng = h3.cell_to_latlng(c)
        assert SMALL_BBOX["south"] - 0.02 <= lat <= SMALL_BBOX["north"] + 0.02
        assert SMALL_BBOX["west"] - 0.02 <= lng <= SMALL_BBOX["east"] + 0.02


def test_bigger_box_gives_more_cells():
    wide = dict(SMALL_BBOX, east=0.0)
    assert len(build.cells_in_bbox(wide, 8)) > len(build.cells_in_bbox(SMALL_BBOX, 8))


# ---------- direct-route classification ----------

def test_no_journeys_is_unreachable():
    assert build.classify_journeys([]) == {"any": 999, "direct": None, "via": []}


def test_single_leg_is_direct():
    r = build.classify_journeys([(25, ["Central"])])
    assert r == {"any": 25, "direct": 25, "via": ["Central"]}


def test_changes_are_not_direct():
    r = build.classify_journeys([(20, ["Northern", "Central"])])
    assert r == {"any": 20, "direct": None, "via": []}


def test_fastest_overall_can_differ_from_fastest_direct():
    r = build.classify_journeys([(20, ["Northern", "Central"]), (35, ["Bus 55"])])
    assert r["any"] == 20 and r["direct"] == 35 and r["via"] == ["Bus 55"]


def test_via_keeps_lines_within_ten_minutes_of_best_direct():
    r = build.classify_journeys([(30, ["Central"]), (40, ["Elizabeth line"]), (41, ["Bus 8"])])
    assert r["direct"] == 30
    assert r["via"] == ["Central", "Elizabeth line"]


def test_walk_only_journey_counts_as_direct_walk():
    r = build.classify_journeys([(12, [])])
    assert r["direct"] == 12 and r["via"] == ["Walk"]


def test_parse_journeys_skips_walking_and_names_buses():
    payload = {"journeys": [{"duration": 31, "legs": [
        {"mode": {"id": "walking"}},
        {"mode": {"id": "bus"}, "routeOptions": [{"name": "55"}]},
        {"mode": {"id": "tube"}, "routeOptions": [{"name": "Central"}]},
    ]}]}
    assert build._parse_journeys(payload) == [(31, ["Bus 55", "Central"])]


# ---------- cache-key invalidation ----------

def test_key_changes_with_destination_date_and_time():
    base = build.cache_key(DEST, "20261019", "0900")
    assert build.cache_key(dict(DEST, lat=51.53), "20261019", "0900") != base
    assert build.cache_key(dict(DEST, lng=-0.12), "20261019", "0900") != base
    assert build.cache_key(DEST, "20261026", "0900") != base
    assert build.cache_key(DEST, "20261019", "0830") != base


def test_key_ignores_label_and_id():
    base = build.cache_key(DEST, "20261019", "0900")
    assert build.cache_key(dict(DEST, name="Renamed", id="y"), "20261019", "0900") == base


def test_reset_keeps_cache_when_key_matches(tmp_path):
    d = tmp_path / "t"
    key = build.cache_key(DEST, "20261019", "0900")
    assert build.reset_if_stale(d, key) is False
    (d / "abc.json").write_text("{}")
    assert build.reset_if_stale(d, key) is False
    assert (d / "abc.json").exists()


def test_reset_clears_journeys_when_key_changes(tmp_path):
    d = tmp_path / "t"
    build.reset_if_stale(d, build.cache_key(DEST, "20261019", "0900"))
    (d / "abc.json").write_text("{}")
    new = build.cache_key(DEST, "20261026", "0900")
    assert build.reset_if_stale(d, new) is True
    assert not (d / "abc.json").exists()
    assert (d / ".key").read_text() == new
