"""Realtime prediction reconciliation: offline tests (no network, no clock).

The time helpers are pure functions of (service date, GTFS time), so the DST
cases are exercised by passing the boundary dates in rather than by freezing
the clock.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from google.transit import gtfs_realtime_pb2

from metra_mcp.client import parse_trip_updates
from metra_mcp.common import CHICAGO_TZ
from metra_mcp.gtfs import GTFSData
from metra_mcp.predictions import (
    infer_service_date,
    parse_gtfs_time,
    resolve_stop_updates,
    scheduled_datetime,
    scheduled_epoch,
    service_day_start,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tripupdates_upn_2026-11-17.json"
FMT = "%Y-%m-%d %H:%M:%S %Z"


def _parse_local(text: str) -> datetime:
    """'2026-11-17 09:27:36 CST' -> aware datetime (unambiguous dates only)."""
    return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHICAGO_TZ)


def _make_gtfs(tmp_path, trips, stop_times) -> GTFSData:
    gtfs = GTFSData(cache_dir=tmp_path)
    gtfs._snap = GTFSData._build_indexes([], [], trips, stop_times, [], [])
    gtfs._loaded = True
    return gtfs


def _make_feed(trip_updates) -> gtfs_realtime_pb2.FeedMessage:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    for i, tu in enumerate(trip_updates):
        entity = feed.entity.add()
        entity.id = str(i)
        entity.trip_update.trip.trip_id = tu["trip_id"]
        entity.trip_update.trip.route_id = tu["route_id"]
        if tu.get("start_date"):
            entity.trip_update.trip.start_date = tu["start_date"]
        if "direction_id" in tu:
            entity.trip_update.trip.direction_id = tu["direction_id"]
        for s in tu["stop_time_updates"]:
            stu = entity.trip_update.stop_time_update.add()
            stu.stop_sequence = s["stop_sequence"]
            stu.stop_id = s["stop_id"]
            for kind in ("arrival", "departure"):
                if kind in s:
                    event = getattr(stu, kind)
                    if "time" in s[kind]:
                        event.time = s[kind]["time"]
                    if "delay" in s[kind]:
                        event.delay = s[kind]["delay"]
    # Round-trip through the wire format, as a fetched feed would be.
    wire = feed.SerializeToString()
    parsed = gtfs_realtime_pb2.FeedMessage()
    parsed.ParseFromString(wire)
    return parsed


@pytest.fixture
def upn(tmp_path):
    """The 2026-11-17 UP-N capture, parsed against its static rows."""
    data = json.loads(FIXTURE.read_text())
    gtfs = _make_gtfs(tmp_path, data["static"]["trips"], data["static"]["stop_times"])
    updates = parse_trip_updates(_make_feed(data["trip_updates"]), "UP-N", None, gtfs)
    return {u["train_number"]: u for u in updates}


def _stops(update) -> dict[str, dict]:
    return {s["stop_id"]: s for s in update["stop_time_updates"]}


# --- 1. Regression: the 2026-11-17 terminal-stop predictions -----------------


@pytest.mark.parametrize(
    ("train", "otc_scheduled"),
    [("320", "09:27:00"), ("322", "09:40:00"), ("324", "09:57:00")],
)
def test_bad_terminal_prediction_is_flagged_and_corrected(upn, train, otc_scheduled):
    stops = _stops(upn[train])
    otc, clybourn = stops["OTC"], stops["CLYBOURN"]
    scheduled = _parse_local(f"2026-11-17 {otc_scheduled}")
    predicted = _parse_local(otc["arrival_time"])

    assert abs(predicted - scheduled) <= timedelta(minutes=5)
    assert timedelta(0) < predicted - _parse_local(clybourn["arrival_time"]) < timedelta(minutes=20)
    assert otc["suspect"] is True
    assert otc["prediction_source"] == "derived"
    # The feed's own value stays visible, an hour out.
    assert _parse_local(otc["feed_arrival_time"]) - scheduled > timedelta(minutes=55)
    assert otc["arrival_delay"] == int((predicted - scheduled).total_seconds())
    assert otc["arrival_time"].endswith(" CST")


def test_train_320_exact_values(upn):
    stops = _stops(upn["320"])
    assert stops["CLYBOURN"]["arrival_time"] == "2026-11-17 09:16:36 CST"
    assert stops["CLYBOURN"]["arrival_delay"] == 36
    assert stops["CLYBOURN"]["suspect"] is False
    assert stops["CLYBOURN"]["prediction_source"] == "feed_time"
    # Clybourn's prediction + the scheduled 11 minutes to OTC.
    assert stops["OTC"]["arrival_time"] == "2026-11-17 09:27:36 CST"
    assert stops["OTC"]["feed_arrival_time"] == "2026-11-17 10:27:06 CST"
    assert stops["OTC"]["scheduled_arrival_time"] == "2026-11-17 09:27:00 CST"


def test_correct_terminal_prediction_is_left_alone(upn):
    """Train 318: OTC was the next stop and the feed had it right."""
    otc = _stops(upn["318"])["OTC"]
    assert otc["arrival_time"] == "2026-11-17 09:11:33 CST"
    assert otc["arrival_delay"] == 93
    assert otc["suspect"] is False
    assert otc["prediction_source"] == "feed_time"


def test_genuinely_late_train_is_not_flagged():
    """An hour late at every stop is a late train, not a bad prediction."""
    day = date(2026, 11, 17)
    static = [
        {"stop_id": "A", "stop_sequence": "1", "arrival_time": "09:00:00"},
        {"stop_id": "B", "stop_sequence": "2", "arrival_time": "09:10:00"},
    ]
    raw = [
        {"stop_sequence": i + 1, "stop_id": row["stop_id"], "departure": None,
         "arrival": {"time": scheduled_epoch(day, row["arrival_time"]) + 3600, "delay": None}}
        for i, row in enumerate(static)
    ]
    out = resolve_stop_updates(raw, static, day)
    assert [s["suspect"] for s in out] == [False, False]
    assert [s["arrival_delay"] for s in out] == [3600, 3600]


def _raw(seq, stop_id, epoch):
    return {"stop_sequence": seq, "stop_id": stop_id, "departure": None,
            "arrival": {"time": epoch, "delay": None}}


def test_smaller_terminal_inflation_is_flagged():
    """Live 2026-11-17: BNSF 1225, Route 59 on time, Aurora 11 min late."""
    day = date(2026, 11, 17)
    static = [
        {"stop_id": "NAPERVILLE", "stop_sequence": "25", "arrival_time": "11:35:00"},
        {"stop_id": "ROUTE59", "stop_sequence": "26", "arrival_time": "11:39:00"},
        {"stop_id": "AURORA", "stop_sequence": "28", "arrival_time": "11:51:00"},
    ]
    raw = [
        _raw(25, "NAPERVILLE", scheduled_epoch(day, "11:36:00")),
        _raw(26, "ROUTE59", scheduled_epoch(day, "11:40:19")),
        _raw(28, "AURORA", scheduled_epoch(day, "12:02:25")),
    ]
    out = resolve_stop_updates(raw, static, day)
    assert [s["suspect"] for s in out] == [False, False, True]
    aurora = out[-1]
    assert aurora["prediction_source"] == "derived"
    assert aurora["arrival_time"] == "2026-11-17 11:52:19 CST"  # Route 59 + scheduled 12 min
    assert aurora["arrival_delay"] == 79
    assert aurora["feed_arrival_time"] == "2026-11-17 12:02:25 CST"


def test_same_jump_mid_trip_is_not_flagged():
    """The tight test is for the last stop only; mid-trip a train can lose 11 min."""
    day = date(2026, 11, 17)
    static = [
        {"stop_id": "ROUTE59", "stop_sequence": "26", "arrival_time": "11:39:00"},
        {"stop_id": "AURORA", "stop_sequence": "28", "arrival_time": "11:51:00"},
        {"stop_id": "BEYOND", "stop_sequence": "29", "arrival_time": "11:59:00"},
    ]
    raw = [
        _raw(26, "ROUTE59", scheduled_epoch(day, "11:40:19")),
        _raw(28, "AURORA", scheduled_epoch(day, "12:02:25")),
        _raw(29, "BEYOND", scheduled_epoch(day, "12:10:25")),
    ]
    out = resolve_stop_updates(raw, static, day)
    assert [s["suspect"] for s in out] == [False, False, False]


def test_terminal_padding_recovery_and_small_slip_are_not_flagged():
    day = date(2026, 11, 17)
    static = [
        {"stop_id": "HALSTED", "stop_sequence": "27", "arrival_time": "11:11:00"},
        {"stop_id": "CUS", "stop_sequence": "28", "arrival_time": "11:20:00"},
    ]
    # Live BNSF 1254: 9 min late at Halsted, under 3 at CUS (padding).
    early = [_raw(27, "HALSTED", scheduled_epoch(day, "11:20:11")),
             _raw(28, "CUS", scheduled_epoch(day, "11:22:50"))]
    assert [s["suspect"] for s in resolve_stop_updates(early, static, day)] == [False, False]
    # Losing 4 more minutes into the terminal is believable.
    slip = [_raw(27, "HALSTED", scheduled_epoch(day, "11:12:00")),
            _raw(28, "CUS", scheduled_epoch(day, "11:25:00"))]
    assert [s["suspect"] for s in resolve_stop_updates(slip, static, day)] == [False, False]


def _otc_static():
    return [
        {"stop_id": "RAVENSWOOD", "stop_sequence": "28", "arrival_time": "12:32:00"},
        {"stop_id": "CLYBOURN", "stop_sequence": "29", "arrival_time": "12:39:00"},
        {"stop_id": "OTC", "stop_sequence": "30", "arrival_time": "12:50:00"},
    ]


def _fastest(pairs):
    table = {frozenset(k): v for k, v in pairs.items()}
    return lambda a, b: table.get(frozenset((a, b)))


def test_too_fast_terminal_arrival_is_flagged():
    """Live 2026-11-17, UP-N 334: 9 min late at Clybourn, OTC 3.6 min later."""
    day = date(2026, 11, 17)
    raw = [
        _raw(28, "RAVENSWOOD", scheduled_epoch(day, "12:40:56")),
        _raw(29, "CLYBOURN", scheduled_epoch(day, "12:47:44")),
        _raw(30, "OTC", scheduled_epoch(day, "12:51:21")),
    ]
    fastest = _fastest({("CLYBOURN", "OTC"): 480})
    out = resolve_stop_updates(raw, _otc_static(), day, fastest_run=fastest)
    assert [s["suspect"] for s in out] == [False, False, True]
    otc = out[-1]
    assert otc["prediction_source"] == "derived"
    assert otc["arrival_time"] == "2026-11-17 12:55:44 CST"  # Clybourn + fastest 8 min
    assert otc["arrival_delay"] == 344
    assert otc["feed_arrival_time"] == "2026-11-17 12:51:21 CST"
    # Without the schedule's fastest run there is nothing to judge it by.
    plain = resolve_stop_updates(raw, _otc_static(), day)
    assert plain[-1]["suspect"] is False


def test_making_up_time_into_the_terminal_is_not_flagged():
    """Live UP-N 330: Clybourn -> OTC in 9:02 against 11 scheduled, 8 fastest."""
    day = date(2026, 11, 17)
    raw = [
        _raw(29, "CLYBOURN", scheduled_epoch(day, "12:41:41")),
        _raw(30, "OTC", scheduled_epoch(day, "12:50:43")),
    ]
    fastest = _fastest({("CLYBOURN", "OTC"): 480})
    out = resolve_stop_updates(raw, _otc_static(), day, fastest_run=fastest)
    assert [s["suspect"] for s in out] == [False, False]
    # Right at the limit (two thirds of the fastest run = 5:20) still passes.
    edge = [_raw(29, "CLYBOURN", scheduled_epoch(day, "12:41:00")),
            _raw(30, "OTC", scheduled_epoch(day, "12:46:20"))]
    assert resolve_stop_updates(edge, _otc_static(), day, fastest_run=fastest)[-1]["suspect"] is False


def test_too_fast_test_applies_to_the_last_stop_only():
    day = date(2026, 11, 17)
    raw = [
        _raw(28, "RAVENSWOOD", scheduled_epoch(day, "12:40:00")),
        _raw(29, "CLYBOURN", scheduled_epoch(day, "12:41:00")),
        _raw(30, "OTC", scheduled_epoch(day, "12:50:00")),
    ]
    fastest = _fastest({("RAVENSWOOD", "CLYBOURN"): 360, ("CLYBOURN", "OTC"): 480})
    out = resolve_stop_updates(raw, _otc_static(), day, fastest_run=fastest)
    assert [s["suspect"] for s in out] == [False, False, False]


def test_fastest_run_comes_from_either_direction(tmp_path):
    trips = [
        {"route_id": "UP-N", "service_id": "A", "trip_id": "IN", "direction_id": "1"},
        {"route_id": "UP-N", "service_id": "A", "trip_id": "OUT", "direction_id": "0"},
    ]
    def row(trip, seq, stop, hms):
        return {"trip_id": trip, "stop_sequence": str(seq), "stop_id": stop,
                "arrival_time": hms, "departure_time": hms}
    stop_times = [
        row("IN", 29, "CLYBOURN", "12:39:00"), row("IN", 30, "OTC", "12:50:00"),
        row("OUT", 1, "OTC", "11:32:00"), row("OUT", 2, "CLYBOURN", "11:40:00"),
        row("OUT", 3, "RAVENSWOOD", "11:45:00"),
    ]
    gtfs = _make_gtfs(tmp_path, trips, stop_times)
    assert gtfs.fastest_run("CLYBOURN", "OTC") == 480
    assert gtfs.fastest_run("OTC", "CLYBOURN") == 480
    assert gtfs.fastest_run("OTC", "RAVENSWOOD") is None  # not adjacent on any trip

    day = date(2026, 11, 17)
    feed = _make_feed([{"trip_id": "IN", "route_id": "UP-N", "start_date": "20261117",
                        "stop_time_updates": [
        {"stop_sequence": 29, "stop_id": "CLYBOURN",
         "arrival": {"time": scheduled_epoch(day, "12:47:44")}},
        {"stop_sequence": 30, "stop_id": "OTC",
         "arrival": {"time": scheduled_epoch(day, "12:51:21")}}]}])
    otc = parse_trip_updates(feed, gtfs=gtfs)[0]["stop_time_updates"][-1]
    assert otc["suspect"] is True
    assert otc["arrival_time"] == "2026-11-17 12:55:44 CST"


def test_terminal_as_next_stop_is_trusted():
    """With no earlier stop left to compare, the feed's terminal time stands."""
    day = date(2026, 11, 17)
    static = [
        {"stop_id": "ROUTE59", "stop_sequence": "26", "arrival_time": "10:39:00"},
        {"stop_id": "AURORA", "stop_sequence": "28", "arrival_time": "10:51:00"},
    ]
    out = resolve_stop_updates([_raw(28, "AURORA", scheduled_epoch(day, "10:55:12"))], static, day)
    assert out[0]["suspect"] is False
    assert out[0]["arrival_delay"] == 252


# --- 2. DST boundaries -------------------------------------------------------

# (service date, GTFS time) -> expected wall clock and UTC offset in hours.
# GTFS times count from "noon minus 12h", so on a DST-change day the times
# before the change are an hour off the wall clock by design; everything a
# train actually runs at (after ~03:00) matches it.
DST_CASES = [
    # Spring forward, 2026-03-08: the service day starts at 23:00 CST on the 7th.
    (date(2026, 3, 8), "01:30:00", "2026-03-08 00:30:00", -6),
    (date(2026, 3, 8), "03:30:00", "2026-03-08 03:30:00", -5),
    (date(2026, 3, 8), "09:27:00", "2026-03-08 09:27:00", -5),
    # Fall back, 2026-11-01: the service day starts at 01:00 CDT.
    (date(2026, 11, 1), "00:30:00", "2026-11-01 01:30:00", -5),
    (date(2026, 11, 1), "01:30:00", "2026-11-01 01:30:00", -6),  # second 01:30
    (date(2026, 11, 1), "02:30:00", "2026-11-01 02:30:00", -6),
    (date(2026, 11, 1), "09:27:00", "2026-11-01 09:27:00", -6),
    # Ordinary days.
    (date(2026, 11, 17), "00:30:00", "2026-11-17 00:30:00", -6),
    (date(2026, 11, 17), "09:27:00", "2026-11-17 09:27:00", -6),
    (date(2026, 7, 14), "00:30:00", "2026-07-14 00:30:00", -5),
    (date(2026, 7, 14), "09:27:00", "2026-07-14 09:27:00", -5),
]


@pytest.mark.parametrize(("day", "hms", "wall", "offset_hours"), DST_CASES)
def test_scheduled_time_round_trips_across_dst(day, hms, wall, offset_hours):
    dt = scheduled_datetime(day, hms)
    assert dt.strftime("%Y-%m-%d %H:%M:%S") == wall
    assert dt.utcoffset() == timedelta(hours=offset_hours)

    epoch = scheduled_epoch(day, hms)
    assert dt.timestamp() == epoch
    back = datetime.fromtimestamp(epoch, tz=CHICAGO_TZ)
    assert back == dt
    assert back.strftime(FMT) == dt.strftime(FMT)
    # And back to the GTFS time it came from.
    assert epoch - service_day_start(day) == parse_gtfs_time(hms)


def test_service_day_is_anchored_at_noon_minus_12h():
    midnight = lambda d: int(datetime(d.year, d.month, d.day, tzinfo=CHICAGO_TZ).timestamp())
    assert service_day_start(date(2026, 11, 17)) == midnight(date(2026, 11, 17))
    assert service_day_start(date(2026, 3, 8)) == midnight(date(2026, 3, 8)) - 3600
    assert service_day_start(date(2026, 11, 1)) == midnight(date(2026, 11, 1)) + 3600


def test_no_fixed_offset_between_cdt_and_cst_days():
    """09:27 scheduled is 09:27 on the wall in July (UTC-5) and November (UTC-6)."""
    july = scheduled_epoch(date(2026, 7, 14), "09:27:00")
    november = scheduled_epoch(date(2026, 11, 17), "09:27:00")
    assert (november - july) % 86400 == 3600


# --- 3. Times past midnight --------------------------------------------------


def test_time_past_midnight_rolls_to_next_day():
    dt = scheduled_datetime(date(2026, 11, 17), "24:32:00")
    assert dt.strftime(FMT) == "2026-11-18 00:32:00 CST"
    assert parse_gtfs_time("24:32:00") == 24 * 3600 + 32 * 60
    assert parse_gtfs_time("") is None
    assert parse_gtfs_time("9:61:00") is None


def test_service_date_inferred_for_a_past_midnight_stop():
    seen = scheduled_epoch(date(2026, 11, 17), "24:32:00") + 45
    assert infer_service_date(seen, "24:32:00") == date(2026, 11, 17)
    assert infer_service_date(seen, "00:32:00") == date(2026, 11, 18)


def test_trip_without_start_date_past_midnight(tmp_path):
    trips = [{"route_id": "BNSF", "service_id": "A1", "trip_id": "BNSF_BN1293_V2_A",
              "trip_headsign": "Aurora", "direction_id": "0"}]
    stop_times = [{"trip_id": "BNSF_BN1293_V2_A", "arrival_time": "24:04:00",
                   "departure_time": "24:04:00", "stop_id": "LAGRANGE", "stop_sequence": "13"}]
    gtfs = _make_gtfs(tmp_path, trips, stop_times)
    when = scheduled_epoch(date(2026, 11, 17), "24:04:00") + 120
    feed = _make_feed([{"trip_id": "BNSF_BN1293_V2_A", "route_id": "BNSF", "stop_time_updates": [
        {"stop_sequence": 13, "stop_id": "LAGRANGE", "arrival": {"time": when}}]}])
    stop = parse_trip_updates(feed, gtfs=gtfs)[0]["stop_time_updates"][0]
    assert stop["arrival_time"] == "2026-11-18 00:06:00 CST"
    assert stop["scheduled_arrival_time"] == "2026-11-18 00:04:00 CST"
    assert stop["arrival_delay"] == 120


# --- 4. Delay is computed, never made up -------------------------------------


def test_time_without_delay_gets_a_computed_delay(upn):
    ravenswood = _stops(upn["320"])["RAVENSWOOD"]  # feed: time only, 09:10:03 vs 09:09
    assert ravenswood["arrival_delay"] == 63


def test_no_schedule_means_delay_is_none_not_zero():
    feed = _make_feed([{"trip_id": "UP-N_UN320_V2_AA", "route_id": "UP-N", "stop_time_updates": [
        {"stop_sequence": 30, "stop_id": "OTC", "arrival": {"time": 1794932826}}]}])
    update = parse_trip_updates(feed)[0]  # no static schedule at all
    stop = update["stop_time_updates"][0]
    assert stop["arrival_delay"] is None
    assert stop["arrival_time"] == "2026-11-17 10:27:06 CST"
    assert "departure_delay" not in stop
    assert update["direction_id"] is None


def test_delay_only_update_uses_schedule_plus_delay():
    day = date(2026, 11, 17)
    static = [{"stop_id": "OTC", "stop_sequence": "30", "arrival_time": "09:27:00"}]
    raw = [{"stop_sequence": 30, "stop_id": "OTC", "departure": None,
            "arrival": {"time": None, "delay": 240}}]
    stop = resolve_stop_updates(raw, static, day)[0]
    assert stop["arrival_time"] == "2026-11-17 09:31:00 CST"
    assert stop["arrival_delay"] == 240
    assert stop["prediction_source"] == "feed_delay"


def test_stop_with_no_realtime_data_reports_schedule_and_no_delay():
    day = date(2026, 11, 17)
    static = [{"stop_id": "OTC", "stop_sequence": "30", "arrival_time": "09:27:00"}]
    raw = [{"stop_sequence": 30, "stop_id": "OTC", "departure": None,
            "arrival": {"time": None, "delay": None}}]
    stop = resolve_stop_updates(raw, static, day)[0]
    assert stop["arrival_time"] == "2026-11-17 09:27:00 CST"
    assert stop["arrival_delay"] is None
    assert stop["prediction_source"] == "scheduled"


def test_feed_delay_disagreeing_with_computed_delay_is_logged(caplog):
    from metra_mcp import predictions

    predictions._last_mismatch_log = float("-inf")
    day = date(2026, 11, 17)
    static = [{"stop_id": "CLYBOURN", "stop_sequence": "29", "arrival_time": "09:16:00"}]
    raw = [{"stop_sequence": 29, "stop_id": "CLYBOURN", "departure": None,
            "arrival": {"time": scheduled_epoch(day, "09:16:00") + 600, "delay": 0}}]
    with caplog.at_level("WARNING", logger="metra_mcp.predictions"):
        stop = resolve_stop_updates(raw, static, day, "UP-N_UN320_V2_AA")[0]
    assert stop["arrival_delay"] == 600  # ours, not the feed's 0
    assert "feed 0s, computed 600s" in caplog.text


# --- 5. Terminal stop takes the same path ------------------------------------


def test_terminal_stop_converts_like_every_other_stop():
    """Same scheduled offset in -> same output at the last stop as mid-trip."""
    day = date(2026, 11, 17)
    static = [
        {"stop_id": "RAVENSWOOD", "stop_sequence": "28", "arrival_time": "09:09:00"},
        {"stop_id": "CLYBOURN", "stop_sequence": "29", "arrival_time": "09:16:00"},
        {"stop_id": "OTC", "stop_sequence": "30", "arrival_time": "09:27:00"},
    ]
    raw = [
        {"stop_sequence": int(row["stop_sequence"]), "stop_id": row["stop_id"], "departure": None,
         "arrival": {"time": scheduled_epoch(day, row["arrival_time"]) + 90, "delay": None}}
        for row in static
    ]
    out = resolve_stop_updates(raw, static, day)
    assert [s["arrival_delay"] for s in out] == [90, 90, 90]
    assert {s["prediction_source"] for s in out} == {"feed_time"}
    assert out[-1]["arrival_time"] == "2026-11-17 09:28:30 CST"
    assert set(out[-1]) == set(out[0])


# --- Direction ---------------------------------------------------------------


def test_direction_filled_from_static_trips(upn):
    assert upn["320"]["direction_id"] == "1"
    assert upn["320"]["direction"] == "inbound"


def test_feed_direction_zero_is_outbound_not_null():
    feed = _make_feed([{"trip_id": "UP-N_UN321_V9_AA", "route_id": "UP-N", "direction_id": 0,
                        "stop_time_updates": []}])
    update = parse_trip_updates(feed)[0]
    assert update["direction_id"] == "0"
    assert update["direction"] == "outbound"
