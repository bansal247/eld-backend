from rest_framework import serializers

from .models import DailyLog, LogEntry, Stop, Trip


# ---------- Input serializer ----------

class LatLngSerializer(serializers.Serializer):
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lng = serializers.FloatField(min_value=-180, max_value=180)
    address = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class TripPlanRequestSerializer(serializers.Serializer):
    current = LatLngSerializer()
    pickup = LatLngSerializer()
    dropoff = LatLngSerializer()
    cycle_hours_used = serializers.FloatField(min_value=0, max_value=70)
    # Either provide an aware datetime, or a naive local string interpreted in
    # the home tz the backend derives from the current location.
    start_datetime = serializers.DateTimeField(required=False)
    start_datetime_local = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs.get('start_datetime') and not attrs.get('start_datetime_local'):
            raise serializers.ValidationError(
                "Either start_datetime (tz-aware) or start_datetime_local "
                "(naive, interpreted in home tz) is required."
            )
        return attrs


# ---------- Output serializers ----------

class LogEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = LogEntry
        fields = ['status', 'start_time', 'end_time', 'remark', 'location']


class DailyLogSerializer(serializers.ModelSerializer):
    entries = LogEntrySerializer(many=True, read_only=True)
    total_off_duty_mins = serializers.SerializerMethodField()
    total_sleeper_mins  = serializers.SerializerMethodField()
    total_driving_mins  = serializers.SerializerMethodField()
    total_on_duty_mins  = serializers.SerializerMethodField()

    class Meta:
        model = DailyLog
        fields = [
            'date',
            'total_miles_driving',
            'total_off_duty_mins',
            'total_sleeper_mins',
            'total_driving_mins',
            'total_on_duty_mins',
            'entries',
        ]

    def get_total_off_duty_mins(self, obj): return round(obj.total_off_duty_hrs * 60)
    def get_total_sleeper_mins(self, obj):  return round(obj.total_sleeper_hrs  * 60)
    def get_total_driving_mins(self, obj):  return round(obj.total_driving_hrs  * 60)
    def get_total_on_duty_mins(self, obj):  return round(obj.total_on_duty_hrs  * 60)


class StopSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stop
        fields = [
            'sequence', 'stop_type', 'lat', 'lng', 'label',
            'arrival', 'departure', 'miles_at_stop',
        ]


class TripDetailSerializer(serializers.ModelSerializer):
    stops = StopSerializer(many=True, read_only=True)
    daily_logs = DailyLogSerializer(many=True, source='logs', read_only=True)
    total_drive_mins = serializers.SerializerMethodField()
    total_trip_mins  = serializers.SerializerMethodField()

    class Meta:
        model = Trip
        fields = [
            'id',
            'current_address', 'current_lat', 'current_lng',
            'pickup_address', 'pickup_lat', 'pickup_lng',
            'dropoff_address', 'dropoff_lat', 'dropoff_lng',
            'cycle_hours_used', 'start_datetime', 'end_datetime', 'home_tz',
            'total_distance_mi', 'total_drive_mins', 'total_trip_mins',
            'route_geometry',
            'stops',
            'daily_logs',
            'created_at',
        ]

    def get_total_drive_mins(self, obj): return round(obj.total_drive_hours * 60)
    def get_total_trip_mins(self, obj):  return round(obj.total_trip_hours  * 60)
