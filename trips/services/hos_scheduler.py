"""
HOS (Hours of Service) Scheduler
=================================

Pure-Python scheduler that converts trip inputs (current location, pickup,
dropoff, current cycle hours) into a continuous timeline of duty-status
segments compliant with FMCSA 49 CFR Part 395.

This module has zero Django imports. It can be tested in isolation by calling
plan_trip() with TripInputs and two Leg objects.

Rules enforced:
  - 11-hour driving limit per shift                  (§ 395.3(a)(3))
  - 14-hour driving window per shift                 (§ 395.3(a)(2))
  - 30-minute break after 8 cumulative driving hrs   (§ 395.3(a)(3)(ii))
  - 10-hour consecutive off-duty reset               (§ 395.3(a)(1))
  - 70-hour / 8-day rolling cycle                    (§ 395.3(b)(2))
  - 34-hour restart                                  (§ 395.3(c))

Plus assignment-specific:
  - Pickup and dropoff: 1 hour each (On Duty Not Driving)
  - Fuel stop every <=1000 miles (30 min, On Duty Not Driving)

Simplifying assumptions documented in README.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import List, Tuple, Optional
from zoneinfo import ZoneInfo


# ---------- HOS constants ----------

class HOS:
    MAX_DRIVE_PER_SHIFT_HRS = 11.0
    MAX_WINDOW_PER_SHIFT_HRS = 14.0
    MIN_OFF_DUTY_RESET_HRS = 10.0
    MAX_DRIVE_BEFORE_BREAK_HRS = 8.0
    MIN_BREAK_HRS = 0.5
    MAX_CYCLE_HRS = 70.0
    RESTART_HRS = 34.0
    MAX_MILES_BETWEEN_FUEL = 1000.0

    PICKUP_HRS = 1.0
    DROPOFF_HRS = 1.0
    FUEL_HRS = 0.5


EPS = 1e-4  # epsilon for float comparisons (a few seconds of slack)


# ---------- Data classes ----------

@dataclass
class LatLng:
    lat: float
    lng: float
    address: str = ""


@dataclass
class Leg:
    """One drivable segment of the trip."""
    distance_miles: float
    duration_hours: float
    geometry: List[List[float]]  # list of [lng, lat] from routing API
    fuel_stations: List[dict] = field(default_factory=list)
    # Each station: {mile_marker, lat, lng, name} sorted by mile_marker

    @property
    def avg_speed_mph(self) -> float:
        if self.duration_hours <= 0:
            return 60.0  # fallback
        return self.distance_miles / self.duration_hours


@dataclass
class TripInputs:
    current: LatLng
    pickup: LatLng
    dropoff: LatLng
    cycle_hours_used: float
    start_datetime: datetime  # tz-aware
    home_tz: str  # IANA tz string, e.g. "America/New_York"


@dataclass
class Segment:
    """One contiguous block of a single duty status."""
    status: str  # 'off_duty' | 'sleeper' | 'driving' | 'on_duty'
    start: datetime
    end: datetime
    label: str
    location: LatLng
    miles_at_start: float = 0.0
    miles_at_end: float = 0.0
    is_stop_marker: bool = False  # True for pickup/dropoff/fuel/sleeper/restart
    stop_type: Optional[str] = None

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    @property
    def miles_driven(self) -> float:
        return max(0.0, self.miles_at_end - self.miles_at_start)


@dataclass
class DailyLogData:
    date: date
    segments: List[Segment]
    total_miles_driving: float
    total_off_duty_hrs: float
    total_sleeper_hrs: float
    total_driving_hrs: float
    total_on_duty_hrs: float


@dataclass
class ClockState:
    """Mutable scheduler state, advanced as the timeline is built."""
    now: datetime
    cycle_hours_used: float
    miles: float = 0.0
    miles_since_fuel: float = 0.0
    drive_hours_this_shift: float = 0.0
    drive_hours_since_break: float = 0.0
    shift_start: Optional[datetime] = None
    last_location: Optional[LatLng] = None


# ---------- Geometry helpers ----------

def _haversine_miles(a: LatLng, b: LatLng) -> float:
    from math import radians, sin, cos, sqrt, asin
    lat1, lng1 = radians(a.lat), radians(a.lng)
    lat2, lng2 = radians(b.lat), radians(b.lng)
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return 2 * 3958.7613 * asin(sqrt(h))


def interpolate_along_route(geometry: List[List[float]], target_miles: float) -> LatLng:
    """Walk along a [[lng,lat],...] polyline and return the LatLng at target_miles.

    Used to place fuel/rest/sleeper markers at the correct point on the route.
    """
    if not geometry:
        return LatLng(0.0, 0.0)
    if len(geometry) == 1 or target_miles <= 0:
        lng, lat = geometry[0][0], geometry[0][1]
        return LatLng(lat, lng)

    accumulated = 0.0
    for i in range(len(geometry) - 1):
        lng1, lat1 = geometry[i][0], geometry[i][1]
        lng2, lat2 = geometry[i + 1][0], geometry[i + 1][1]
        a = LatLng(lat1, lng1)
        b = LatLng(lat2, lng2)
        seg_len = _haversine_miles(a, b)

        if accumulated + seg_len >= target_miles:
            # Interpolate between a and b
            if seg_len < EPS:
                return a
            frac = (target_miles - accumulated) / seg_len
            return LatLng(
                lat=lat1 + (lat2 - lat1) * frac,
                lng=lng1 + (lng2 - lng1) * frac,
            )
        accumulated += seg_len

    # Beyond the end of the route — return last point
    last = geometry[-1]
    return LatLng(lat=last[1], lng=last[0])


# ---------- Segment helpers ----------

def _emit_segment(timeline: List[Segment], clock: ClockState, *,
                  status: str, hours: float, label: str,
                  location: Optional[LatLng] = None,
                  is_stop_marker: bool = False,
                  stop_type: Optional[str] = None) -> Segment:
    """Append a segment to the timeline and advance the clock's wall time only."""
    if hours <= 0:
        return None  # type: ignore
    end = clock.now + timedelta(hours=hours)
    loc = location if location is not None else (clock.last_location or LatLng(0.0, 0.0))
    seg = Segment(
        status=status,
        start=clock.now,
        end=end,
        label=label,
        location=loc,
        miles_at_start=clock.miles,
        miles_at_end=clock.miles,  # non-driving segments don't accumulate miles
        is_stop_marker=is_stop_marker,
        stop_type=stop_type,
    )
    timeline.append(seg)
    clock.now = end
    clock.last_location = loc
    return seg


# ---------- Insertion helpers (for breaks, fuel, resets, restarts) ----------


def _insert_30min_break(timeline: List[Segment], clock: ClockState):
    """Mandatory 30-min break (Off Duty)."""
    _emit_segment(
        timeline, clock,
        status='off_duty',
        hours=HOS.MIN_BREAK_HRS,
        label="30-minute break",
        is_stop_marker=True,
        stop_type='rest_30min',
    )
    clock.drive_hours_since_break = 0.0
    # NOTE: 30-min break does NOT pause the 14-hour window (rule clarified by FMCSA).


def _insert_fuel_stop(timeline: List[Segment], clock: ClockState, label: Optional[str] = None):
    """Fuel stop — On Duty, ~30 min. Also satisfies the 30-min break."""
    if label is None:
        label = f"Fuel — mile {round(clock.miles)}"
    _emit_segment(
        timeline, clock,
        status='on_duty',
        hours=HOS.FUEL_HRS,
        label=label,
        is_stop_marker=True,
        stop_type='fuel',
    )
    clock.cycle_hours_used += HOS.FUEL_HRS
    clock.miles_since_fuel = 0.0
    # Fuel >= 30 min satisfies the break requirement
    clock.drive_hours_since_break = 0.0


def _insert_10hr_reset(timeline: List[Segment], clock: ClockState):
    """10-hour off-duty (sleeper berth) reset between shifts."""
    _emit_segment(
        timeline, clock,
        status='sleeper',
        hours=HOS.MIN_OFF_DUTY_RESET_HRS,
        label="10-hour off-duty reset",
        is_stop_marker=True,
        stop_type='sleeper_10hr',
    )
    # Reset shift-level clocks (cycle is NOT reset by 10-hr — only 34-hr does that)
    clock.drive_hours_this_shift = 0.0
    clock.drive_hours_since_break = 0.0
    clock.shift_start = clock.now


def _insert_34hr_restart(timeline: List[Segment], clock: ClockState):
    """34-hour restart — resets the rolling 70-hr cycle."""
    _emit_segment(
        timeline, clock,
        status='off_duty',
        hours=HOS.RESTART_HRS,
        label="34-hour restart",
        is_stop_marker=True,
        stop_type='reset_34hr',
    )
    # Reset everything
    clock.drive_hours_this_shift = 0.0
    clock.drive_hours_since_break = 0.0
    clock.cycle_hours_used = 0.0
    clock.shift_start = clock.now


# ---------- Fuel station helpers ----------

def _station_to_stop_at(
    stations: List[dict], miles_into_leg: float, remaining_miles: float
) -> Optional[dict]:
    """
    Return the farthest fuel station that is:
      - ahead of the current position (mile_marker > miles_into_leg)
      - reachable before the 1000-mile hard limit

    Picking the farthest one means we drive as far as possible before fueling.
    Returns None if no station qualifies.
    """
    result = None
    for s in stations:  # sorted by mile_marker ascending
        if s["mile_marker"] <= miles_into_leg + EPS:
            continue  # behind or at current position
        if s["mile_marker"] > miles_into_leg + remaining_miles + EPS:
            break  # list is sorted; everything beyond is also out of range
        result = s  # keep updating — last valid one is the farthest
    return result


# ---------- The driving loop ----------

def _drive_phase(timeline: List[Segment], clock: ClockState, leg: Leg, label: str):
    """Consume a Leg, inserting breaks/fuel/sleepers/restarts as required."""
    if leg.distance_miles < EPS:
        return  # nothing to drive

    miles_remaining = leg.distance_miles
    avg_speed = leg.avg_speed_mph

    while miles_remaining > EPS:
        # Step 1: cycle exhausted?
        if clock.cycle_hours_used >= HOS.MAX_CYCLE_HRS - EPS:
            _insert_34hr_restart(timeline, clock)
            continue

        # Step 2: 11-hour drive limit reached?
        if clock.drive_hours_this_shift >= HOS.MAX_DRIVE_PER_SHIFT_HRS - EPS:
            _insert_10hr_reset(timeline, clock)
            continue

        # Step 3: 14-hour window expired?
        if clock.shift_start is None:
            clock.shift_start = clock.now  # safety: should already be set at shift start
        window_used = (clock.now - clock.shift_start).total_seconds() / 3600.0
        if window_used >= HOS.MAX_WINDOW_PER_SHIFT_HRS - EPS:
            _insert_10hr_reset(timeline, clock)
            continue

        # Step 4: 30-min break needed?
        if clock.drive_hours_since_break >= HOS.MAX_DRIVE_BEFORE_BREAK_HRS - EPS:
            _insert_30min_break(timeline, clock)
            continue

        # Step 5: fuel needed?
        miles_into_leg = leg.distance_miles - miles_remaining
        if clock.miles_since_fuel >= HOS.MAX_MILES_BETWEEN_FUEL - EPS:
            # Hard 1000-mile limit — stop at current interpolated position
            fuel_loc = interpolate_along_route(leg.geometry, target_miles=miles_into_leg)
            clock.last_location = fuel_loc
            _insert_fuel_stop(timeline, clock)
            continue

        # Step 5b: reached a scheduled fuel station (actual OSM stop, before hard limit)?
        # Guard with miles_since_fuel > EPS: after fueling, miles_since_fuel resets to 0
        # so this block is skipped until the chunk drives forward, preventing re-trigger.
        if leg.fuel_stations and clock.miles_since_fuel > EPS:
            _rem = HOS.MAX_MILES_BETWEEN_FUEL - clock.miles_since_fuel
            # Search from slightly behind current position so a station sitting
            # exactly at miles_into_leg isn't skipped by the > miles_into_leg+EPS guard
            _tgt = _station_to_stop_at(leg.fuel_stations, miles_into_leg - 0.05, _rem + 0.05)
            if _tgt and miles_into_leg >= _tgt["mile_marker"] - 0.05:
                clock.last_location = LatLng(lat=_tgt["lat"], lng=_tgt["lng"])
                _insert_fuel_stop(
                    timeline, clock,
                    label=f"Fuel — {_tgt['name']} (mile {round(clock.miles)})",
                )
                continue

        # No interruption needed — drive as much as we can before the next clock fires
        until_drive_limit = HOS.MAX_DRIVE_PER_SHIFT_HRS - clock.drive_hours_this_shift
        until_window_close = HOS.MAX_WINDOW_PER_SHIFT_HRS - window_used
        until_break_due = HOS.MAX_DRIVE_BEFORE_BREAK_HRS - clock.drive_hours_since_break
        until_cycle_full = HOS.MAX_CYCLE_HRS - clock.cycle_hours_used
        until_fuel_due = (HOS.MAX_MILES_BETWEEN_FUEL - clock.miles_since_fuel) / max(avg_speed, 1.0)
        until_leg_done = miles_remaining / max(avg_speed, 1.0)

        # Limit chunk to stop exactly at the next scheduled fuel station (if within range)
        until_next_station_hrs = float("inf")
        if leg.fuel_stations:
            _rem = HOS.MAX_MILES_BETWEEN_FUEL - clock.miles_since_fuel
            _tgt = _station_to_stop_at(leg.fuel_stations, miles_into_leg, _rem)
            if _tgt and _tgt["mile_marker"] > miles_into_leg + EPS:
                until_next_station_hrs = (
                    (_tgt["mile_marker"] - miles_into_leg) / max(avg_speed, 1.0)
                )

        max_drive_hrs = min(
            until_drive_limit,
            until_window_close,
            until_break_due,
            until_cycle_full,
            until_fuel_due,
            until_next_station_hrs,
            until_leg_done,
        )
        # Avoid zero-length emissions due to float noise
        if max_drive_hrs < EPS:
            max_drive_hrs = min(0.01, until_leg_done)

        miles_this_chunk = min(miles_remaining, max_drive_hrs * avg_speed)
        hours_this_chunk = miles_this_chunk / avg_speed

        # Compute end location by interpolation
        end_loc = interpolate_along_route(
            leg.geometry,
            target_miles=miles_into_leg + miles_this_chunk,
        )

        end_time = clock.now + timedelta(hours=hours_this_chunk)
        timeline.append(Segment(
            status='driving',
            start=clock.now,
            end=end_time,
            label=label,
            location=end_loc,
            miles_at_start=clock.miles,
            miles_at_end=clock.miles + miles_this_chunk,
            is_stop_marker=False,
        ))
        clock.now = end_time
        clock.last_location = end_loc
        clock.miles += miles_this_chunk
        clock.miles_since_fuel += miles_this_chunk
        clock.drive_hours_this_shift += hours_this_chunk
        clock.drive_hours_since_break += hours_this_chunk
        clock.cycle_hours_used += hours_this_chunk
        miles_remaining -= miles_this_chunk


# ---------- On-duty (pickup, dropoff) ----------

def _on_duty_phase(timeline: List[Segment], clock: ClockState, *,
                   hours: float, label: str, location: LatLng, stop_type: str):
    """Block of On Duty Not Driving. Auto-satisfies 30-min break if >= 0.5 hrs."""
    # Pre-flight: cycle check
    if clock.cycle_hours_used + hours > HOS.MAX_CYCLE_HRS - EPS:
        _insert_34hr_restart(timeline, clock)

    # Window check: if this on-duty would push past 14-hr window AND we're not done driving,
    # we'd want a sleeper first. But on-duty *non-driving* is allowed past 14hrs (just no
    # more driving). We allow it through to keep the schedule realistic.
    clock.last_location = location
    _emit_segment(
        timeline, clock,
        status='on_duty',
        hours=hours,
        label=label,
        location=location,
        is_stop_marker=True,
        stop_type=stop_type,
    )
    clock.cycle_hours_used += hours

    # Auto-satisfy 30-min break
    if hours >= HOS.MIN_BREAK_HRS - EPS:
        clock.drive_hours_since_break = 0.0


# ---------- Top-level entry point ----------

def plan_trip(inputs: TripInputs, leg1: Leg, leg2: Leg) -> Tuple[List[Segment], List[DailyLogData]]:
    """Build the full timeline and split it into daily logs."""
    timeline: List[Segment] = []
    clock = ClockState(
        now=inputs.start_datetime,
        cycle_hours_used=inputs.cycle_hours_used,
        last_location=inputs.current,
    )

    # Marker for trip start
    timeline.append(Segment(
        status='on_duty',  # placeholder — won't appear on log (zero duration)
        start=inputs.start_datetime,
        end=inputs.start_datetime,
        label=f"Start: {inputs.current.address or 'Trip start'}",
        location=inputs.current,
        miles_at_start=0.0,
        miles_at_end=0.0,
        is_stop_marker=True,
        stop_type='start',
    ))

    clock.shift_start = clock.now

    # Leg 1: current -> pickup
    _drive_phase(timeline, clock, leg1, label="En route to pickup")

    # Pickup: 1 hour On Duty
    _on_duty_phase(
        timeline, clock,
        hours=HOS.PICKUP_HRS,
        label=f"Pickup: {inputs.pickup.address or 'pickup location'}",
        location=inputs.pickup,
        stop_type='pickup',
    )

    # Leg 2: pickup -> dropoff
    _drive_phase(timeline, clock, leg2, label="En route to dropoff")

    # Dropoff: 1 hour On Duty
    _on_duty_phase(
        timeline, clock,
        hours=HOS.DROPOFF_HRS,
        label=f"Dropoff: {inputs.dropoff.address or 'dropoff location'}",
        location=inputs.dropoff,
        stop_type='dropoff',
    )

    # Pad final day with off-duty until calendar midnight (home tz)
    _pad_to_midnight(timeline, clock, inputs.home_tz)

    # The zero-duration trip-start marker is intentionally retained in the
    # returned timeline so the API view can persist it as a "Start" Stop on
    # the map. split_into_daily_logs() filters it out before computing day
    # totals, so it doesn't affect the log sheets.
    daily_logs = split_into_daily_logs(timeline, inputs.home_tz, inputs.start_datetime)
    return timeline, daily_logs


def _pad_to_midnight(timeline: List[Segment], clock: ClockState, home_tz: str):
    """After dropoff, emit Off Duty until next calendar midnight in home tz.

    This keeps the final day's totals summing to 24 hours.
    """
    tz = ZoneInfo(home_tz)
    local_now = clock.now.astimezone(tz)
    next_midnight_local = (local_now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    next_midnight_utc = next_midnight_local.astimezone(clock.now.tzinfo)
    pad_hours = (next_midnight_utc - clock.now).total_seconds() / 3600.0
    if pad_hours > EPS:
        _emit_segment(
            timeline, clock,
            status='off_duty',
            hours=pad_hours,
            label="End of trip — off duty",
            is_stop_marker=False,
        )


# ---------- Daily-log splitting ----------

def split_into_daily_logs(timeline: List[Segment], home_tz: str,
                          start_datetime: datetime) -> List[DailyLogData]:
    """Slice the continuous timeline into per-calendar-day log sheets.

    Splits any segment crossing midnight in the home terminal time zone.
    Pads the FIRST calendar day with Off Duty from 00:00 to start_datetime so
    that day totals always sum to 24 hours.
    """
    tz = ZoneInfo(home_tz)
    by_day: dict = {}

    # 1) Pre-pad the first day with Off Duty from midnight to start
    first_day_local_midnight = start_datetime.astimezone(tz).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    first_day_local_midnight_utc = first_day_local_midnight.astimezone(start_datetime.tzinfo)
    if start_datetime > first_day_local_midnight_utc:
        prepad = Segment(
            status='off_duty',
            start=first_day_local_midnight_utc,
            end=start_datetime,
            label="Off duty (pre-trip)",
            location=LatLng(0.0, 0.0),
            miles_at_start=0.0,
            miles_at_end=0.0,
            is_stop_marker=False,
        )
        _add_to_day_with_split(prepad, by_day, tz)

    # 2) Walk the timeline and split each segment at midnight crossings
    for seg in timeline:
        if seg.is_stop_marker and seg.stop_type == 'start':
            continue  # skip the zero-duration start marker
        _add_to_day_with_split(seg, by_day, tz)

    # 3) Build DailyLogData rows
    daily_logs: List[DailyLogData] = []
    for d in sorted(by_day.keys()):
        segments = by_day[d]
        # Sort segments within the day by start time
        segments.sort(key=lambda s: s.start)
        totals = {'off_duty': 0.0, 'sleeper': 0.0, 'driving': 0.0, 'on_duty': 0.0}
        for s in segments:
            totals[s.status] += s.duration_hours
        miles_today = sum(s.miles_driven for s in segments if s.status == 'driving')

        daily_logs.append(DailyLogData(
            date=d,
            segments=segments,
            total_miles_driving=round(miles_today, 1),
            total_off_duty_hrs=round(totals['off_duty'], 2),
            total_sleeper_hrs=round(totals['sleeper'], 2),
            total_driving_hrs=round(totals['driving'], 2),
            total_on_duty_hrs=round(totals['on_duty'], 2),
        ))
    return daily_logs


def _add_to_day_with_split(seg: Segment, by_day: dict, tz: ZoneInfo):
    """Add a segment to the by_day dict, splitting at midnight in home tz if needed.

    When splitting a driving segment, miles are distributed proportionally by time.
    """
    start_local = seg.start.astimezone(tz)
    end_local = seg.end.astimezone(tz)

    if start_local.date() == end_local.date():
        by_day.setdefault(start_local.date(), []).append(seg)
        return

    total_seconds = (seg.end - seg.start).total_seconds()
    total_miles = seg.miles_at_end - seg.miles_at_start

    cursor = seg.start
    cursor_local = start_local
    cursor_miles = seg.miles_at_start

    while cursor_local.date() < end_local.date():
        next_midnight_local = (cursor_local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        next_midnight_utc = next_midnight_local.astimezone(seg.start.tzinfo)
        piece_seconds = (next_midnight_utc - cursor).total_seconds()
        frac = piece_seconds / total_seconds if total_seconds > 0 else 0
        piece_miles_end = cursor_miles + total_miles * frac

        piece = Segment(
            status=seg.status,
            start=cursor,
            end=next_midnight_utc,
            label=seg.label,
            location=seg.location,
            miles_at_start=cursor_miles,
            miles_at_end=piece_miles_end,
            is_stop_marker=False,  # only the original carries the marker flag
            stop_type=None,
        )
        by_day.setdefault(cursor_local.date(), []).append(piece)
        cursor = next_midnight_utc
        cursor_local = next_midnight_local
        cursor_miles = piece_miles_end

    final_piece = Segment(
        status=seg.status,
        start=cursor,
        end=seg.end,
        label=seg.label,
        location=seg.location,
        miles_at_start=cursor_miles,
        miles_at_end=seg.miles_at_end,
        is_stop_marker=seg.is_stop_marker,
        stop_type=seg.stop_type,
    )
    # Only add final piece if it has meaningful duration
    if (seg.end - cursor).total_seconds() > 0.5:
        by_day.setdefault(end_local.date(), []).append(final_piece)
