from django.urls import path

from . import views

urlpatterns = [
    path('geocode/', views.geocode_view, name='geocode'),
    path('trips/plan/', views.plan_trip_view, name='trip-plan'),
    path('trips/<int:pk>/', views.TripDetailView.as_view(), name='trip-detail'),
]
