# django-maintenance-tasks

Pausable, resumable, throttled data backfills for Django, inspired by Shopify's
[`maintenance_tasks`](https://github.com/Shopify/maintenance_tasks) for Rails.

Backfilling millions of rows from a management command or a `RunPython`
migration is risky: it can lock tables, slow the site down, and if it crashes
halfway it starts again from zero. This package turns each backfill into a small
class that runs in the background in batches, saves its position as it goes, and
can be started, paused, resumed and cancelled from the Django admin.

It runs on Django's built-in [Tasks framework](https://docs.djangoproject.com/en/stable/topics/tasks/),
so it works with any task backend: the database backend from `django-tasks`, RQ,
Celery and others.

> Status: early scaffold. The API may change.

## Install

Requires Python 3.12+ and Django 6.0+. Not on PyPI yet, so install from GitHub:

```bash
pip install git+https://github.com/ajinkyapisal/django-maintenance-tasks
```

```python
INSTALLED_APPS = [
    # ...
    "maintenance_tasks",
]

# Any django.tasks backend that runs tasks in a worker.
TASKS = {"default": {"BACKEND": "..."}}
```

```bash
python manage.py migrate
```

## Write a task

Put tasks in `<app>/maintenance_tasks.py`. They are discovered automatically.

```python
from django.utils.text import slugify

from maintenance_tasks import MaintenanceTask
from blog.models import Post


class BackfillSlugs(MaintenanceTask):
    description = "Fill in missing post slugs."
    batch_size = 500

    def collection(self):
        return Post.objects.filter(slug="")

    def process(self, post):
        post.slug = slugify(post.title)
        post.save(update_fields=["slug"])
```

- `collection()` returns a QuerySet, a list or another iterable. QuerySets are
  walked in primary key order. Other iterables are walked by position, so they
  must return the same items in the same order every time.
- `process(item)` handles one item. It must be safe to run twice on the same
  item: after a crash, up to one batch can be processed again.
- `self.arguments` holds the JSON arguments the run was started with.
- `count()` gives the total for the progress bar. Override it if counting is
  slow.
- `throttle()` returns `True` to back off for `throttle_backoff` seconds, for
  example when database replicas are lagging.

## Run it

From the admin: **Maintenance tasks → Runs → Start a run**. Select runs to
pause, resume or cancel them. Starting and controlling runs needs the
`maintenance_tasks.add_run` permission.

From the command line:

```bash
python manage.py maintenance_tasks list
python manage.py maintenance_tasks run blog.maintenance_tasks.BackfillSlugs --arguments '{"batch": "2024-q1"}'
```

From code:

```python
from maintenance_tasks import runner

run = runner.start("blog.maintenance_tasks.BackfillSlugs")
runner.pause(run)
runner.resume(run)
runner.cancel(run)
```

## How it works

- Each run is a `Run` row with a status, a cursor and progress counts. Only one
  active run per task is allowed.
- A run is processed by a `django.tasks` job. After every batch it saves the
  cursor and checks whether a pause or cancel was requested.
- A job gives up its worker after `MAX_RUNTIME` seconds (default 120) and
  enqueues the next job, so long backfills never block a worker for hours:

  ```python
  MAINTENANCE_TASKS = {"MAX_RUNTIME": 120, "STALE_AFTER": 600}
  ```

- If a worker dies mid-run (killed, out of memory, machine lost), the run stays
  `running` with an old heartbeat. `recover_stalled()` puts it back on the
  queue from its last checkpoint. Run it from cron, or use the admin action:

  ```bash
  python manage.py maintenance_tasks recover
  ```

  A run counts as stalled after `STALE_AFTER` seconds without a heartbeat
  (default 600). Keep it well above the time one batch takes.
- Every job carries a generation number. Recovering a run starts a new
  generation, so if the old worker was only frozen and comes back, it stops at
  its next checkpoint instead of running alongside the new one. Duplicate
  deliveries of the same job are ignored too.
- If `process()` raises, the run is marked `errored` with the exception and
  traceback, and the cursor points at the last item that succeeded.
- Throttle backoff uses `run_after` when the backend supports deferred tasks.
  Otherwise the worker sleeps for the backoff time.

## Roadmap

- CSV upload as a collection
- Typed task parameters with a generated admin form
- Dry runs
- Retrying errored runs from the cursor
- Live progress in the admin
- Run history: who started what, with which arguments

## Development

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

`example/` is a demo project on Postgres with the `django-tasks-db` backend.
`example/e2e.py` runs real `db_worker` processes and checks pausing,
cancelling, throttling, errors, graceful shutdown, killed workers and frozen
workers:

```bash
docker run -d --name dmt-pg -e POSTGRES_HOST_AUTH_METHOD=trust \
    -e POSTGRES_DB=dmt_demo -p 55432:5432 postgres:17-alpine
.venv/bin/pip install django-tasks-db "psycopg[binary]"
.venv/bin/python example/manage.py migrate
.venv/bin/python example/e2e.py
```
