"""Unit tests for the HOS scheduler.

Verifies HOS rule compliance for representative trip scenarios from short
single-shift trips through cross-country trips requiring 34-hour restarts.
"""
from datetime import datetime, timedelta, timezone
from unittest import TestCase

from trips.services.hos_scheduler import (
    HOS,
    Leg,
    LatLng,
    TripInputs,
    plan_trip,
)


def _start_dt():
    """Standard start datetime: 2026-04-29 06:00 EDT."""
    return datetime(2026, 4, 29, 6, 0, 0, tzinfo=timezone(timedelta(hours=-4)))


def _make_inputs(*, total_miles_leg1=50, total_miles_leg2=200, cycle_used=0):
    return TripInputs(
        current=LatLng(40.7128, -74.0060, "New York, NY"),
        pickup=LatLng(40.4406, -79.9959, "Pittsburgh, PA"),
        dropoff=LatLng(39.9612, -82.9988, "Columbus, OH"),
        cycle_hours_used=cycle_used,
        start_datetime=_start_dt(),
        home_tz="America/New_York",
    ), Leg(
        distance_miles=total_miles_leg1,
        duration_hours=total_miles_leg1 / 55.0,
        geometry=[[-74.00, 40.71], [-79.99, 40.44]],
    ), Leg(
        distance_miles=total_miles_leg2,
        duration_hours=total_miles_leg2 / 55.0,
        geometry=[[-79.99, 40.44], [-82.99, 39.96]],
    )


class HOSSchedulerTests(TestCase):

    def assertDayTotalsSumTo24(self, daily_logs):
        for day in daily_logs:
            total = (day.total_off_duty_hrs + day.total_sleeper_hrs
                     + day.total_driving_hrs + day.total_on_duty_hrs)
            self.assertAlmostEqual(total, 24.0, delta=0.05,
                msg=f"Day {day.date} totals = {total}, expected 24")

    def assertNoShiftViolatesRules(self, timeline):
        """Walk timeline and verify no shift exceeds 11hr drive or 14hr window."""
        shift_drive_hrs = 0.0
        shift_start = None
        for seg in timeline:
            if seg.stop_type in ('sleeper_10hr', 'reset_34hr'):
                shift_drive_hrs = 0.0
                shift_start = None
                continue
            if seg.is_stop_marker and seg.stop_type == 'start':
                continue
            if shift_start is None:
                shift_start = seg.start
            if seg.status == 'driving':
                shift_drive_hrs += seg.duration_hours
                self.assertLessEqual(shift_drive_hrs, HOS.MAX_DRIVE_PER_SHIFT_HRS + 0.05,
                    msg=f"11-hr drive limit violated: {shift_drive_hrs}")
            window_used = (seg.end - shift_start).total_seconds() / 3600.0
            if seg.status == 'driving':
                self.assertLessEqual(window_used, HOS.MAX_WINDOW_PER_SHIFT_HRS + 0.05,
                    msg=f"14-hr window violated by driving segment: {window_used}")

    # ---------- Scenario 1: short trip ----------

    def test_short_trip_no_breaks_or_sleeper(self):
        inputs, leg1, leg2 = _make_inputs(total_miles_leg1=50, total_miles_leg2=200)
        timeline, daily_logs = plan_trip(inputs, leg1, leg2)
        self.assertDayTotalsSumTo24(daily_logs)
        sleepers = [s for s in timeline if s.stop_type == 'sleeper_10hr']
        self.assertEqual(len(sleepers), 0)
        self.assertEqual(len(daily_logs), 1)
        self.assertNoShiftViolatesRules(timeline)

    # ---------- Scenario 2: pickup satisfies break ----------

    def test_pickup_satisfies_30min_break(self):
        # ~11 hrs of driving total. Pickup happens after 2 hrs, so the 30-min
        # break clock resets there. No standalone 30-min break should appear.
        inputs, leg1, leg2 = _make_inputs(total_miles_leg1=110, total_miles_leg2=440)
        timeline, daily_logs = plan_trip(inputs, leg1, leg2)
        self.assertDayTotalsSumTo24(daily_logs)
        # The fuel stop (also satisfies break) is OK. We just check no rest_30min was needed
        # before the fuel stop. Loose check: count rest_30min stops.
        rest_breaks = [s for s in timeline if s.stop_type == 'rest_30min']
        # In this scenario, fuel at mile 1000 doesn't apply (only 550 mi), so the 30-min
        # break is satisfied only by pickup. No standalone break needed.
        self.assertEqual(len(rest_breaks), 0)

    # ---------- Scenario 3: 11-hr drive limit triggers sleeper ----------

    def test_long_trip_triggers_sleeper(self):
        # 1100 mi total - requires multi-day shifts
        inputs = TripInputs(
            current=LatLng(40.71, -74.00, "New York, NY"),
            pickup=LatLng(40.71, -74.00, "Same"),
            dropoff=LatLng(34.05, -118.24, "Los Angeles, CA"),
            cycle_hours_used=0,
            start_datetime=_start_dt(),
            home_tz="America/New_York",
        )
        leg1 = Leg(0, 0, [[-74.00, 40.71]])
        leg2 = Leg(1100, 20.0, [[-74.00, 40.71], [-118.24, 34.05]])

        timeline, daily_logs = plan_trip(inputs, leg1, leg2)
        self.assertDayTotalsSumTo24(daily_logs)

        sleepers = [s for s in timeline if s.stop_type == 'sleeper_10hr']
        self.assertGreaterEqual(len(sleepers), 1)
        self.assertNoShiftViolatesRules(timeline)

    # ---------- Scenario 4: cycle exhaustion triggers 34-hr restart ----------

    def test_cycle_exhaustion_triggers_restart(self):
        inputs = TripInputs(
            current=LatLng(40.71, -74.00, "NYC"),
            pickup=LatLng(40.71, -74.00, "Same"),
            dropoff=LatLng(34.05, -118.24, "LA"),
            cycle_hours_used=20,  # already 20 hrs into 70-hr cycle
            start_datetime=_start_dt(),
            home_tz="America/New_York",
        )
        leg1 = Leg(0, 0, [[-74.00, 40.71]])
        leg2 = Leg(2800, 51.0, [[-74.00, 40.71], [-118.24, 34.05]])

        timeline, daily_logs = plan_trip(inputs, leg1, leg2)
        self.assertDayTotalsSumTo24(daily_logs)

        restarts = [s for s in timeline if s.stop_type == 'reset_34hr']
        self.assertGreaterEqual(len(restarts), 1)

    # ---------- Scenario 5: fuel stop placement ----------

    def test_fuel_stop_at_1000_miles(self):
        inputs = TripInputs(
            current=LatLng(40.71, -74.00, "NYC"),
            pickup=LatLng(40.71, -74.00, "Same"),
            dropoff=LatLng(34.05, -118.24, "LA"),
            cycle_hours_used=0,
            start_datetime=_start_dt(),
            home_tz="America/New_York",
        )
        leg1 = Leg(0, 0, [[-74.00, 40.71]])
        leg2 = Leg(2800, 51.0, [[-74.00, 40.71], [-118.24, 34.05]])

        timeline, daily_logs = plan_trip(inputs, leg1, leg2)
        fuel_stops = [s for s in timeline if s.stop_type == 'fuel']
        # 2800 miles requires at least 2 fuel stops (every 1000 mi)
        self.assertGreaterEqual(len(fuel_stops), 2)

    # ---------- Scenario 6: required stop types are present ----------

    def test_required_stops_present(self):
        inputs, leg1, leg2 = _make_inputs()
        timeline, _ = plan_trip(inputs, leg1, leg2)
        types = [s.stop_type for s in timeline if s.is_stop_marker]
        self.assertIn('start', types)
        self.assertIn('pickup', types)
        self.assertIn('dropoff', types)
