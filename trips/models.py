from django.db import models


class Trip(models.Model):
    """One planned trip — created once via the planning endpoint."""
    # Inputs (what the user submitted)
    current_address = models.CharField(max_length=500)
    current_lat = models.FloatField()
    current_lng = models.FloatField()

    pickup_address = models.CharField(max_length=500)
    pickup_lat = models.FloatField()
    pickup_lng = models.FloatField()

    dropoff_address = models.CharField(max_length=500)
    dropoff_lat = models.FloatField()
    dropoff_lng = models.FloatField()

    cycle_hours_used = models.FloatField()
    start_datetime = models.DateTimeField()
    home_tz = models.CharField(max_length=64, default="America/New_York")

    # Computed summary
    total_distance_mi = models.FloatField()
    total_drive_hours = models.FloatField()
    total_trip_hours = models.FloatField()  # wall-clock duration
    end_datetime = models.DateTimeField()

    # Route geometry (GeoJSON-style coords [[lng,lat], ...] for each leg)
    route_geometry = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Trip {self.pk}: {self.current_address} -> {self.dropoff_address}"


class Stop(models.Model):
    STOP_TYPE_CHOICES = [
        ('start', 'Start'),
        ('pickup', 'Pickup'),
        ('dropoff', 'Dropoff'),
        ('fuel', 'Fuel'),
        ('rest_30min', '30-Min Break'),
        ('sleeper_10hr', '10-Hr Sleeper'),
        ('reset_34hr', '34-Hr Restart'),
    ]
    trip = models.ForeignKey(Trip, related_name='stops', on_delete=models.CASCADE)
    sequence = models.IntegerField()
    stop_type = models.CharField(max_length=20, choices=STOP_TYPE_CHOICES)
    lat = models.FloatField()
    lng = models.FloatField()
    label = models.CharField(max_length=200)
    arrival = models.DateTimeField()
    departure = models.DateTimeField()
    miles_at_stop = models.FloatField(default=0.0)

    class Meta:
        ordering = ['sequence']


class DailyLog(models.Model):
    trip = models.ForeignKey(Trip, related_name='logs', on_delete=models.CASCADE)
    date = models.DateField()
    total_miles_driving = models.FloatField()
    total_off_duty_hrs = models.FloatField()
    total_sleeper_hrs = models.FloatField()
    total_driving_hrs = models.FloatField()
    total_on_duty_hrs = models.FloatField()

    class Meta:
        ordering = ['date']
        unique_together = [('trip', 'date')]


class LogEntry(models.Model):
    STATUS_CHOICES = [
        ('off_duty', 'Off Duty'),
        ('sleeper', 'Sleeper Berth'),
        ('driving', 'Driving'),
        ('on_duty', 'On Duty (Not Driving)'),
    ]
    daily_log = models.ForeignKey(DailyLog, related_name='entries', on_delete=models.CASCADE)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    remark = models.CharField(max_length=200, blank=True)
    location = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['start_time']
