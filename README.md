# ELD Trip Planner — Backend

Django REST API that plans property-carrying truck trips and generates
ELD-compliant daily log sheets per FMCSA Hours of Service rules
(49 CFR Part 395).

## Tech stack

- Django 4.2 + Django REST Framework
- SQLite (development) / PostgreSQL (production)
- OpenRouteService (truck routing, free tier)
- Nominatim (OpenStreetMap geocoding, free)
- Whitenoise + Gunicorn for production

## Setup

```bash
cd backend
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then optionally set ORS_API_KEY
python manage.py migrate
python manage.py runserver
```

The API runs on http://localhost:8000.

### OpenRouteService API key (recommended)

Get a free key at https://openrouteservice.org/dev/. The free tier allows
2,000 directions requests per day, more than enough for development.

Without a key, the app falls back to a great-circle distance estimate
(road miles ≈ 1.3 × straight-line, average speed 60 mph). This is good
enough for testing the scheduler and UI but won't reflect real road
distances.

## API endpoints

### `POST /api/trips/plan/`

Plans a trip and returns route geometry, stops, and daily logs.

**Request:**

```json
{
  "current": {"lat": 40.71, "lng": -74.00, "address": "New York, NY"},
  "pickup":  {"lat": 41.88, "lng": -87.63, "address": "Chicago, IL"},
  "dropoff": {"lat": 34.05, "lng": -118.24, "address": "Los Angeles, CA"},
  "cycle_hours_used": 20,
  "start_datetime_local": "2026-04-29T06:00"
}
```

`start_datetime_local` is a naive ISO datetime (no timezone) interpreted in
the home tz the backend derives from the current location's longitude.
This is the common case — the user enters wall-clock time at the driver's
home terminal. Alternatively, send `start_datetime` as a tz-aware ISO
datetime if you want to be explicit.

**Response:** Trip object with nested `stops` and `daily_logs`. See
`trips/serializers.py` for the full shape. Returns HTTP 201 on success.

### `GET /api/geocode/?q=<query>`

Proxies to Nominatim with 7-day caching. Returns up to 5 matches:

```json
[
  {"display_name": "New York, NY, USA", "lat": 40.71, "lng": -74.00}
]
```

Respects Nominatim's 1 req/sec rate limit via a global lock.

### `GET /api/trips/<id>/`

Retrieves a previously planned trip with all stops and logs.

## HOS rules implemented

| Rule | Citation |
| --- | --- |
| 11-hour driving limit per shift | § 395.3(a)(3) |
| 14-hour driving window per shift | § 395.3(a)(2) |
| 30-minute break after 8 cumulative driving hours | § 395.3(a)(3)(ii) |
| 10-hour off-duty reset between shifts | § 395.3(a)(1) |
| 70-hour / 8-day rolling cycle | § 395.3(b)(2) |
| 34-hour restart | § 395.3(c) |

The 30-minute break is satisfied automatically by qualifying pickup,
dropoff, or fuel stops, per FMCSA guidance — the scheduler does not
insert a redundant separate break in those cases.

## Simplifying assumptions

To keep scope tractable, the following are intentionally not modeled:

1. **Split sleeper berth** (§ 395.1(g)) — only single 10-hour resets.
2. **Adverse driving conditions** (§ 395.1(b)(1)) — per assignment.
3. **Short-haul exceptions** (§ 395.1(e), § 395.1(o)).
4. **Personal conveyance** and **yard moves**.
5. **Team driving** / co-driver passenger-seat provisions.
6. **Pickup, dropoff, fuel durations** are fixed (1 h, 1 h, 30 min).
7. **Routing duration** taken at face value from OpenRouteService.
8. **Home time zone** is derived from the current location's longitude
   using a coarse US band heuristic. Production would use `timezonefinder`.
9. Each trip is planned as a **standalone shift** beginning from a fully
   rested state (10+ hours off duty before trip start).
10. `cycle_hours_used` is a **manual input** per assignment specification;
    in production it would be auto-calculated from rolling 8-day history.

## Project structure

```
backend/
├── eld_planner/              Django project
│   ├── settings.py
│   ├── urls.py
│   ├── wsgi.py
│   └── asgi.py
└── trips/                    App
    ├── models.py             Trip, Stop, DailyLog, LogEntry
    ├── views.py              API endpoints
    ├── serializers.py        Request/response shapes
    ├── urls.py
    ├── admin.py
    ├── services/
    │   ├── hos_scheduler.py  Pure scheduler logic (no Django imports)
    │   ├── routing.py        OpenRouteService wrapper + fallback
    │   ├── geocoding.py      Nominatim wrapper with cache + rate limit
    │   └── timezone.py       Home tz from US longitude bands
    ├── tests/
    │   └── test_hos_scheduler.py    Six scenario tests
    └── migrations/
```

## Testing

```bash
python manage.py test trips
```

The scheduler tests in `trips/tests/test_hos_scheduler.py` cover:
- Short single-shift trip (no breaks/sleepers needed)
- Pickup auto-satisfies the 30-minute break
- 11-hour drive limit triggers a sleeper reset
- Multi-day trip with midnight-crossing sleepers
- Cycle exhaustion triggers a 34-hour restart
- Fuel stops at 1000-mile intervals
- All required stop types (start, pickup, dropoff) are present

Each test asserts that day totals sum to exactly 24 hours and that no
shift violates the 11-hour or 14-hour limits.

## Database

Default is SQLite (zero-config for development). For PostgreSQL, set:

```
DB_NAME=eld_planner
DB_USER=...
DB_PASSWORD=...
DB_HOST=localhost
DB_PORT=5432
DATABASE_URL=postgres://user:pass@host:5432/dbname
```

(The presence of `DATABASE_URL` switches Django to PostgreSQL.)

## Admin interface

The Django admin is available at `/admin/`. Create a superuser:

```bash
python manage.py createsuperuser
```

The four models — Trip, Stop, DailyLog, LogEntry — are registered with
useful list views and filters.

## Deployment

Set environment variables: `SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS`,
`ORS_API_KEY`, `DATABASE_URL`, `CORS_ALLOWED_ORIGINS`. Run:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
gunicorn eld_planner.wsgi:application
```
