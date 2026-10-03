"""Demo project that runs maintenance tasks on a real worker.

Uses Postgres and the django-tasks-db backend:

    docker run -d --name dmt-pg -e POSTGRES_HOST_AUTH_METHOD=trust \
        -e POSTGRES_DB=dmt_demo -p 55432:5432 postgres:17-alpine
    pip install django-tasks-db "psycopg[binary]"
    python example/manage.py migrate
    python example/manage.py db_worker
"""

import os

SECRET_KEY = "demo"
DEBUG = True
USE_TZ = True
ROOT_URLCONF = "example.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django_tasks_db",
    "maintenance_tasks",
    "example.blog",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DMT_DB", "dmt_demo"),
        "HOST": os.environ.get("DMT_DB_HOST", "localhost"),
        "PORT": os.environ.get("DMT_DB_PORT", "55432"),
        "USER": os.environ.get("DMT_DB_USER", "postgres"),
    }
}

TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend"}}

MAINTENANCE_TASKS = {
    "MAX_RUNTIME": float(os.environ.get("DMT_MAX_RUNTIME", 120)),
    "STALE_AFTER": float(os.environ.get("DMT_STALE_AFTER", 600)),
}
