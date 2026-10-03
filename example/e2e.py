"""End-to-end checks against a real db_worker process.

    python example/e2e.py

Needs the Postgres described in example/settings.py, migrated.
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "example.settings")
# Runs count as stalled after 2 s without a heartbeat, so recovery is quick.
os.environ["DMT_STALE_AFTER"] = "2"

import django  # noqa: E402

django.setup()

from django_tasks_db.models import DBTaskResult  # noqa: E402

from example.blog.maintenance_tasks import BackfillSlugs  # noqa: E402
from example.blog.models import Post  # noqa: E402
from maintenance_tasks import runner  # noqa: E402
from maintenance_tasks.models import Run  # noqa: E402

Status = Run.Status
LOGS = Path(tempfile.mkdtemp(prefix="dmt-e2e-"))
workers = []


def reset(posts):
    Run.objects.all().delete()
    DBTaskResult.objects.all().delete()
    Post.objects.all().delete()
    Post.objects.bulk_create(Post(title=f"Post number {i}") for i in range(posts))


def start_worker(max_runtime=1):
    log = open(LOGS / f"worker-{len(workers)}.log", "w")
    worker = subprocess.Popen(
        [sys.executable, str(ROOT / "example/manage.py"), "db_worker", "--interval", "0.1", "--no-reload"],
        env={**os.environ, "DMT_MAX_RUNTIME": str(max_runtime)},
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    workers.append(worker)
    return worker


def stop_worker(worker, sig=signal.SIGTERM):
    worker.send_signal(sig)
    worker.wait(timeout=30)


def wait_for(run, statuses, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run.refresh_from_db()
        if run.status in statuses:
            return run
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {statuses}, run is {run.status}")


def wait_until_processing(run, ticks=1, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run.refresh_from_db()
        if run.status == Status.RUNNING and run.tick_count >= ticks:
            return
        time.sleep(0.05)
    raise AssertionError("run never started processing")


def batch_jobs():
    return DBTaskResult.objects.filter(task_path__endswith="run_batch")


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f'  ({detail})' if detail != '' else ''}")
    if not condition:
        check.failed = True


check.failed = False


def scenario(func):
    print(f"\n{func.__doc__}")
    try:
        func()
    finally:
        for worker in workers:
            if worker.poll() is None:
                stop_worker(worker, signal.SIGKILL)
        workers.clear()
    return func


@scenario
def full_run():
    """Full run: 2,000 rows, 1 s max runtime, so the job must requeue itself"""
    reset(2000)
    start_worker()
    started = time.monotonic()
    run = runner.start(BackfillSlugs.name, {"delay": 0.001})
    wait_for(run, [Status.SUCCEEDED, Status.ERRORED])
    check("run succeeded", run.status == Status.SUCCEEDED, run.status)
    check("all rows processed", run.tick_count == 2000, run.tick_count)
    check("no slugs left empty", not Post.objects.filter(slug="").exists())
    jobs = batch_jobs().count()
    check("spread over several worker jobs", jobs >= 2, f"{jobs} jobs")
    check("no failed worker jobs", not batch_jobs().failed().exists())
    print(f"        took {time.monotonic() - started:.1f}s")


@scenario
def pause_and_resume():
    """Pause mid-run from another process, then resume"""
    reset(2000)
    start_worker()
    run = runner.start(BackfillSlugs.name, {"delay": 0.003})
    wait_until_processing(run, ticks=200)
    runner.pause(run)
    wait_for(run, [Status.PAUSED])
    paused_at = run.tick_count
    check("paused partway", 0 < paused_at < 2000, f"tick_count {paused_at}")
    check("paused at a batch boundary", paused_at % 100 == 0, paused_at)
    time.sleep(2)
    run.refresh_from_db()
    check("no progress while paused", run.tick_count == paused_at)
    check(
        "unprocessed rows match progress",
        Post.objects.filter(slug="").count() == 2000 - paused_at,
    )
    runner.resume(run)
    wait_for(run, [Status.SUCCEEDED, Status.ERRORED])
    check("resumed run succeeded", run.status == Status.SUCCEEDED, run.status)
    check("all rows processed once", run.tick_count == 2000, run.tick_count)


@scenario
def cancel():
    """Cancel mid-run"""
    reset(2000)
    start_worker()
    run = runner.start(BackfillSlugs.name, {"delay": 0.003})
    wait_until_processing(run, ticks=200)
    runner.cancel(run)
    wait_for(run, [Status.CANCELLED])
    check("cancelled partway", 0 < run.tick_count < 2000, run.tick_count)
    count = run.tick_count
    time.sleep(1.5)
    run.refresh_from_db()
    check("no work after cancel", run.tick_count == count)
    check("a new run can start", runner.start(BackfillSlugs.name).pk != run.pk)


@scenario
def throttle():
    """Throttle: back off with run_after while a flag file exists"""
    reset(500)
    flag = LOGS / "throttle"
    flag.touch()
    start_worker()
    run = runner.start(BackfillSlugs.name, {"throttle_file": str(flag)})
    time.sleep(3)
    run.refresh_from_db()
    check("no progress while throttled", run.tick_count == 0, run.tick_count)
    deferred = batch_jobs().filter(run_after__gt=django.utils.timezone.now()).exists()
    check("next job deferred with run_after", deferred)
    flag.unlink()
    wait_for(run, [Status.SUCCEEDED, Status.ERRORED])
    check("finishes after throttle lifts", run.status == Status.SUCCEEDED, run.status)
    check("all rows processed", run.tick_count == 500, run.tick_count)


@scenario
def error():
    """Error: process() raises partway"""
    reset(300)
    start_worker()
    run = runner.start(BackfillSlugs.name, {"fail_at": "Post number 150"})
    wait_for(run, [Status.SUCCEEDED, Status.ERRORED])
    check("run errored", run.status == Status.ERRORED, run.status)
    check("error recorded", "cannot slugify" in run.error_message, run.error_message)
    check("progress kept up to the failure", run.tick_count == 150, run.tick_count)
    first_failed = Post.objects.get(title="Post number 150")
    check("cursor is the last good row", run.cursor == first_failed.pk - 1, run.cursor)


@scenario
def graceful_worker_shutdown():
    """Deploy: SIGTERM the worker mid-run, a new worker picks it up"""
    reset(3000)
    first = start_worker(max_runtime=2)
    run = runner.start(BackfillSlugs.name, {"delay": 0.002})
    wait_until_processing(run)
    stop_worker(first, signal.SIGTERM)
    run.refresh_from_db()
    stopped_at = run.tick_count
    check(
        "first worker finished its job and requeued",
        run.status == Status.ENQUEUED and stopped_at > 0,
        f"{run.status} at {stopped_at}",
    )
    start_worker(max_runtime=2)
    wait_for(run, [Status.SUCCEEDED, Status.ERRORED])
    check("second worker finished it", run.status == Status.SUCCEEDED, run.status)
    check("every row processed exactly once", run.tick_count == 3000, run.tick_count)


@scenario
def worker_killed():
    """Crash: SIGKILL the worker mid-run, then recover"""
    reset(3000)
    first = start_worker(max_runtime=60)
    run = runner.start(BackfillSlugs.name, {"delay": 0.002})
    wait_until_processing(run, ticks=300)
    stop_worker(first, signal.SIGKILL)
    start_worker()
    time.sleep(1)
    run.refresh_from_db()
    check("stuck as running before recovery", run.status == Status.RUNNING, run.status)
    check("not stalled yet, so not recovered", runner.recover_stalled() == [])
    time.sleep(2.5)
    check("recovered once stalled", len(runner.recover_stalled()) == 1)
    wait_for(run, [Status.SUCCEEDED, Status.ERRORED])
    check("finished after recovery", run.status == Status.SUCCEEDED, run.status)
    # Rows done after the last checkpoint are saved but not counted, and the
    # recovered job skips them because they no longer match slug="".
    check("count within one batch", 2900 < run.tick_count <= 3000, run.tick_count)
    check("no slugs left empty", not Post.objects.filter(slug="").exists())


@scenario
def frozen_worker_comes_back():
    """Zombie: freeze a worker, recover the run, then unfreeze the old worker"""
    reset(3000)
    frozen = start_worker(max_runtime=60)
    run = runner.start(BackfillSlugs.name, {"delay": 0.002})
    wait_until_processing(run, ticks=300)
    frozen.send_signal(signal.SIGSTOP)
    time.sleep(2.5)
    check("recovered while frozen", len(runner.recover_stalled()) == 1)
    start_worker()
    wait_for(run, [Status.SUCCEEDED, Status.ERRORED])
    finished = Run.objects.values().get(pk=run.pk)
    frozen.send_signal(signal.SIGCONT)
    time.sleep(2)
    run.refresh_from_db()
    check("new worker finished it", run.status == Status.SUCCEEDED, run.status)
    check(
        "old worker did not touch the run",
        Run.objects.values().get(pk=run.pk) == finished,
    )
    check("no slugs left empty", not Post.objects.filter(slug="").exists())
    log = (LOGS / "worker-0.log").read_text()
    check("old worker noticed and stopped", "recovered by a newer job" in log)


print(f"\nWorker logs: {LOGS}")
sys.exit(1 if check.failed else 0)
