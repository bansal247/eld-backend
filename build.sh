#!/usr/bin/env bash
set -e

pip install -r requirements.txt

python manage.py migrate --no-input

python manage.py collectstatic --no-input --clear

# Create superuser from env vars; silently skips if the username already exists.
# Set DJANGO_SUPERUSER_USERNAME, DJANGO_SUPERUSER_EMAIL, DJANGO_SUPERUSER_PASSWORD
# in your Vercel environment variables (or .env for local runs).
python manage.py shell << 'PYEOF'
from django.contrib.auth import get_user_model
import os

User = get_user_model()
username = os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin')
email    = os.environ.get('DJANGO_SUPERUSER_EMAIL', '')
password = os.environ.get('DJANGO_SUPERUSER_PASSWORD')

if not password:
    print('DJANGO_SUPERUSER_PASSWORD not set — skipping superuser creation.')
elif User.objects.filter(username=username).exists():
    print(f'Superuser "{username}" already exists — skipped.')
else:
    User.objects.create_superuser(username, email, password)
    print(f'Superuser "{username}" created.')
PYEOF
