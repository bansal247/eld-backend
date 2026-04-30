from django.contrib import admin

from .models import DailyLog, LogEntry, Stop, Trip


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    list_display = ['id', 'current_address', 'dropoff_address',
                    'total_distance_mi', 'created_at']
    readonly_fields = ['created_at']


@admin.register(Stop)
class StopAdmin(admin.ModelAdmin):
    list_display = ['trip', 'sequence', 'stop_type', 'label', 'arrival']
    list_filter = ['stop_type']


@admin.register(DailyLog)
class DailyLogAdmin(admin.ModelAdmin):
    list_display = ['trip', 'date', 'total_miles_driving',
                    'total_driving_hrs', 'total_on_duty_hrs']


@admin.register(LogEntry)
class LogEntryAdmin(admin.ModelAdmin):
    list_display = ['daily_log', 'status', 'start_time', 'end_time']
    list_filter = ['status']
