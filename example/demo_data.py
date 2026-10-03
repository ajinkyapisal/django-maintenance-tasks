"""Fill the demo database with runs in different states, for trying the admin.

    python example/demo_data.py

Creates a local-only superuser (username "admin", password "admin"), then uses
a real db_worker to produce succeeded, errored, paused and running runs. The
last run keeps running for about ten minutes so the admin shows live progress.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "example.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django_tasks_db.models import DBTaskResult  # noqa: E402

from example.blog import maintenance_tasks as tasks  # noqa: E402
from example.blog.models import Post  # noqa: E402
from maintenance_tasks import runner  # noqa: E402
from maintenance_tasks.models import Run  # noqa: E402

Status = Run.Status


def wait_until(run, condition, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run.refresh_from_db()
        if condition(run):
            return
        time.sleep(0.1)
    raise TimeoutError(f"{run} did not reach the expected state")


def wait_for(run, statuses):
    wait_until(run, lambda r: r.status in statuses)


def wait_for_ticks(run, ticks):
    wait_until(run, lambda r: r.tick_count >= ticks)


User = get_user_model()
admin, created = User.objects.get_or_create(
    username="admin", defaults={"is_staff": True, "is_superuser": True}
)
if created:
    admin.set_password("admin")
    admin.save()

Run.objects.all().delete()
DBTaskResult.objects.all().delete()
Post.objects.all().delete()
Post.objects.bulk_create(
    Post(title=f"  post number {i} about django  ") for i in range(5000)
)

worker = subprocess.Popen(
    [sys.executable, str(ROOT / "example/manage.py"), "db_worker", "--interval", "0.1", "--no-reload"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    run = runner.start(tasks.BackfillSlugs.name, started_by=admin)
    wait_for(run, [Status.SUCCEEDED])
    print(f"{run} done")

    run = runner.start(
        tasks.RecomputeSlugs.name, {"fail_at": "post number 3172 about django"}, admin
    )
    wait_for(run, [Status.ERRORED])
    print(f"{run} done")

    run = runner.start(tasks.NormalizeTitles.name, {"delay": 0.002}, admin)
    wait_for_ticks(run, 1800)
    runner.pause(run)
    wait_for(run, [Status.PAUSED])
    print(f"{run} done")

    run = runner.start(tasks.RecomputeSlugs.name, {"delay": 0.1}, admin)
    wait_for_ticks(run, 300)
    print(f"{run} is running; the worker keeps going in the background")
except BaseException:
    worker.kill()
    raise
print(f"Worker pid {worker.pid}. Stop it with: kill {worker.pid}")
