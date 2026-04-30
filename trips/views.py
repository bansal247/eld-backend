"""API views for the ELD trip planner."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.generics import RetrieveAPIView
from rest_framework.response import Response

from .models import DailyLog, LogEntry, Stop, Trip
from .serializers import TripDetailSerializer, TripPlanRequestSerializer
from .services import geocoding
from .services.fuel_stations import get_fuel_stations_on_leg
from .services.hos_scheduler import LatLng, TripInputs, plan_trip
from .services.routing import get_route
from .services.timezone import tz_for_us_coords


@api_view(['GET'])
def geocode_view(request):
    """GET /api/geocode/?q=<query>  -> [{display_name, lat, lng}, ...]"""
    q = request.query_params.get('q', '').strip()
    if len(q) < 3:
        return Response([])
    return Response(geocoding.geocode(q, limit=5))


@api_view(['POST'])
def plan_trip_view(request):
    """POST /api/trips/plan/ -> full Trip object with stops and daily logs."""
    serializer = TripPlanRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    current = data['current']
    pickup = data['pickup']
    dropoff = data['dropoff']

    # Derive home tz from current location (US longitude bands)
    home_tz_name = tz_for_us_coords(current['lat'], current['lng'])
    home_tz = ZoneInfo(home_tz_name)

    # Resolve start_datetime: prefer naive-local interpreted in home_tz; fall back
    # to provided tz-aware datetime.
    naive_local = data.get('start_datetime_local')
    if naive_local:
        try:
            # Accept "YYYY-MM-DDTHH:MM" or with seconds
            naive_dt = datetime.fromisoformat(naive_local)
        except ValueError:
            return Response(
                {'detail': 'start_datetime_local must be ISO 8601 (YYYY-MM-DDTHH:MM).'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if naive_dt.tzinfo is not None:
            # If user supplied a tz, normalize to home tz
            start_datetime = naive_dt.astimezone(home_tz)
        else:
            start_datetime = naive_dt.replace(tzinfo=home_tz)
    else:
        start_datetime = data['start_datetime']

    # Get the two route legs from ORS (or great-circle fallback) — run in parallel
    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(get_route, current['lat'], current['lng'], pickup['lat'], pickup['lng'])
        f2 = executor.submit(get_route, pickup['lat'], pickup['lng'], dropoff['lat'], dropoff['lng'])
        leg1, leg2 = f1.result(), f2.result()

    # Attach real fuel station locations from Overpass API — run in parallel
    # (best-effort; falls back to [] so the scheduler uses the 1000-mile mileage rule)
    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(get_fuel_stations_on_leg, leg1.geometry)
        f2 = executor.submit(get_fuel_stations_on_leg, leg2.geometry)
        leg1.fuel_stations, leg2.fuel_stations = f1.result(), f2.result()

    # Run the scheduler
    inputs = TripInputs(
        current=LatLng(current['lat'], current['lng'], current.get('address', '')),
        pickup=LatLng(pickup['lat'], pickup['lng'], pickup.get('address', '')),
        dropoff=LatLng(dropoff['lat'], dropoff['lng'], dropoff.get('address', '')),
        cycle_hours_used=data['cycle_hours_used'],
        start_datetime=start_datetime,
        home_tz=home_tz_name,
    )
    timeline, daily_logs_data = plan_trip(inputs, leg1, leg2)

    # The actual trip end is the dropoff segment, not the off-duty padding to
    # midnight that follows it.
    dropoff_seg = next(
        (s for s in reversed(timeline) if s.is_stop_marker and s.stop_type == 'dropoff'),
        None,
    )
    end_datetime = dropoff_seg.end if dropoff_seg else (timeline[-1].end if timeline else start_datetime)
    total_trip_hours = round((end_datetime - start_datetime).total_seconds() / 3600.0, 2)

    # Persist
    with transaction.atomic():
        trip = Trip.objects.create(
            current_address=current.get('address', '') or f"{current['lat']:.4f},{current['lng']:.4f}",
            current_lat=current['lat'],
            current_lng=current['lng'],
            pickup_address=pickup.get('address', '') or f"{pickup['lat']:.4f},{pickup['lng']:.4f}",
            pickup_lat=pickup['lat'],
            pickup_lng=pickup['lng'],
            dropoff_address=dropoff.get('address', '') or f"{dropoff['lat']:.4f},{dropoff['lng']:.4f}",
            dropoff_lat=dropoff['lat'],
            dropoff_lng=dropoff['lng'],
            cycle_hours_used=data['cycle_hours_used'],
            start_datetime=start_datetime,
            home_tz=home_tz_name,
            total_distance_mi=round(leg1.distance_miles + leg2.distance_miles, 1),
            total_drive_hours=round(leg1.duration_hours + leg2.duration_hours, 2),
            total_trip_hours=total_trip_hours,
            end_datetime=end_datetime,
            route_geometry={
                'leg1': leg1.geometry,
                'leg2': leg2.geometry,
                'leg1_mi': round(leg1.distance_miles, 1),
                'leg2_mi': round(leg2.distance_miles, 1),
            },
        )

        # Create Stop rows for each stop-marker segment (with sequence and arrival/departure)
        seq = 0
        # Group stop markers by stop_type+location to compute arrival/departure
        # Simpler: for each stop-marker segment, the stop covers exactly that segment.
        for seg in timeline:
            if not seg.is_stop_marker:
                continue
            Stop.objects.create(
                trip=trip,
                sequence=seq,
                stop_type=seg.stop_type,
                lat=seg.location.lat,
                lng=seg.location.lng,
                label=seg.label,
                arrival=seg.start,
                departure=seg.end if seg.end > seg.start else seg.start + timedelta(seconds=1),
                miles_at_stop=round(seg.miles_at_end, 1),
            )
            seq += 1

        # Create DailyLog + LogEntry rows
        for day in daily_logs_data:
            log = DailyLog.objects.create(
                trip=trip,
                date=day.date,
                total_miles_driving=day.total_miles_driving,
                total_off_duty_hrs=day.total_off_duty_hrs,
                total_sleeper_hrs=day.total_sleeper_hrs,
                total_driving_hrs=day.total_driving_hrs,
                total_on_duty_hrs=day.total_on_duty_hrs,
            )
            for s in day.segments:
                # Build a sensible remark: the segment label, optionally with city info
                remark = s.label or ""
                location = ""
                if s.location and (s.location.lat or s.location.lng):
                    location = f"{s.location.lat:.4f}, {s.location.lng:.4f}"
                LogEntry.objects.create(
                    daily_log=log,
                    status=s.status,
                    start_time=s.start,
                    end_time=s.end,
                    remark=remark[:200],
                    location=location[:200],
                )

    return Response(
        TripDetailSerializer(trip).data,
        status=status.HTTP_201_CREATED,
    )


class TripDetailView(RetrieveAPIView):
    """GET /api/trips/<pk>/  -> full Trip object."""
    queryset = Trip.objects.prefetch_related('stops', 'logs__entries').all()
    serializer_class = TripDetailSerializer
