from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from maintenance_tasks import runner
from maintenance_tasks.models import Run
from maintenance_tasks.task import UnknownTask
from tests.testapp import maintenance_tasks as tasks
from tests.testapp.models import Post

# on_commit callbacks only fire outside a test transaction.
pytestmark = pytest.mark.django_db(transaction=True)

Status = Run.Status


def make_posts(count):
    Post.objects.bulk_create(Post(title=f"Post number {i}") for i in range(count))


def test_run_processes_whole_queryset():
    make_posts(25)

    run = runner.start(tasks.BackfillSlugs.name)

    run.refresh_from_db()
    assert run.status == Status.SUCCEEDED
    assert (run.tick_count, run.tick_total, run.progress) == (25, 25, 100)
    assert not Post.objects.filter(slug="").exists()
    assert run.started_at and run.ended_at


def test_start_does_not_count_rows(settings):
    # Nothing executes on the dummy backend, so this shows start() itself
    # leaves counting to the job.
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}}
    make_posts(5)

    run = runner.start(tasks.BackfillSlugs.name)

    run.refresh_from_db()
    assert (run.status, run.tick_total) == (Status.ENQUEUED, None)


def test_resume_continues_after_cursor():
    make_posts(25)
    pks = list(Post.objects.order_by("pk").values_list("pk", flat=True))
    run = Run.objects.create(
        task_name=tasks.BackfillSlugs.name,
        status=Status.PAUSED,
        cursor=pks[9],
        tick_count=10,
        tick_total=25,
    )

    runner.resume(run)

    run.refresh_from_db()
    assert run.status == Status.SUCCEEDED
    assert run.tick_count == 25
    assert Post.objects.filter(slug="").count() == 10  # the first ten were skipped


def test_plain_list_collection():
    tasks.SumNumbers.seen = []

    run = runner.start(tasks.SumNumbers.name, {"size": 8})

    run.refresh_from_db()
    assert run.status == Status.SUCCEEDED
    assert tasks.SumNumbers.seen == list(range(8))
    assert run.cursor == 7


def test_pause_mid_run_then_resume():
    run = runner.start(
        tasks.RequestDuringRun.name, {"at": 25, "status": Status.PAUSING}
    )

    run.refresh_from_db()
    assert run.status == Status.PAUSED
    assert (run.cursor, run.tick_count) == (29, 30)  # stops at the batch boundary

    runner.resume(run)

    run.refresh_from_db()
    assert run.status == Status.SUCCEEDED
    assert run.tick_count == 100


def test_cancel_mid_run():
    run = runner.start(
        tasks.RequestDuringRun.name, {"at": 25, "status": Status.CANCELLING}
    )

    run.refresh_from_db()
    assert run.status == Status.CANCELLED
    assert run.tick_count == 30
    assert run.ended_at


def test_cancel_paused_run():
    run = Run.objects.create(task_name=tasks.SumNumbers.name, status=Status.PAUSED)

    runner.cancel(run)

    assert run.status == Status.CANCELLED


def test_error_records_details_and_keeps_cursor():
    run = runner.start(tasks.FailOnSeven.name)

    run.refresh_from_db()
    assert run.status == Status.ERRORED
    assert run.error_class == "builtins.ValueError"
    assert run.error_message == "seven is unlucky"
    assert "seven is unlucky" in run.backtrace
    assert (run.cursor, run.tick_count) == (6, 7)


def test_requeues_when_max_runtime_is_reached(settings):
    settings.MAINTENANCE_TASKS = {"MAX_RUNTIME": 0}
    make_posts(25)

    run = runner.start(tasks.BackfillSlugs.name)

    run.refresh_from_db()
    assert run.status == Status.SUCCEEDED
    assert run.tick_count == 25


def test_throttle_requeues_and_finishes():
    tasks.ThrottledOnce.calls = 0

    run = runner.start(tasks.ThrottledOnce.name)

    run.refresh_from_db()
    assert run.status == Status.SUCCEEDED
    assert run.tick_count == 10


def test_recover_stalled_running_run():
    make_posts(25)
    pks = list(Post.objects.order_by("pk").values_list("pk", flat=True))
    # A run whose worker was killed after its first batch.
    run = Run.objects.create(
        task_name=tasks.BackfillSlugs.name,
        status=Status.RUNNING,
        cursor=pks[9],
        tick_count=10,
        tick_total=25,
        generation=3,
        heartbeat_at=timezone.now() - timedelta(hours=1),
    )

    assert runner.recover_stalled() == [run]

    run.refresh_from_db()
    assert run.status == Status.SUCCEEDED
    assert run.tick_count == 25
    assert run.generation == 4


def test_recover_ignores_healthy_runs():
    Run.objects.create(
        task_name=tasks.SumNumbers.name,
        status=Status.RUNNING,
        heartbeat_at=timezone.now(),
    )

    assert runner.recover_stalled() == []


def test_recover_completes_pending_pause_and_cancel():
    old = timezone.now() - timedelta(hours=1)
    pausing = Run.objects.create(
        task_name=tasks.SumNumbers.name, status=Status.PAUSING, heartbeat_at=old
    )
    cancelling = Run.objects.create(
        task_name=tasks.FailOnSeven.name, status=Status.CANCELLING, heartbeat_at=old
    )

    runner.recover_stalled()

    pausing.refresh_from_db()
    cancelling.refresh_from_db()
    assert pausing.status == Status.PAUSED
    assert cancelling.status == Status.CANCELLED


def test_stale_job_does_nothing():
    # The job queued for generation 0 is replaced by a newer one.
    run = Run.objects.create(
        task_name=tasks.SumNumbers.name, status=Status.ENQUEUED, generation=1
    )

    runner.run_batch.call(run.pk, 0)

    run.refresh_from_db()
    assert (run.status, run.tick_count) == (Status.ENQUEUED, 0)


def test_duplicate_job_runs_once():
    tasks.SumNumbers.seen = []
    run = runner.start(tasks.SumNumbers.name, {"size": 5})

    runner.run_batch.call(run.pk, run.generation)  # delivered a second time

    run.refresh_from_db()
    assert run.tick_count == 5
    assert tasks.SumNumbers.seen == list(range(5))


def test_zombie_job_stops_at_next_checkpoint():
    run = Run.objects.create(task_name=tasks.SumNumbers.name, generation=0)
    executor = runner.Executor(run, generation=0)
    Run.objects.filter(pk=run.pk).update(status=Status.RUNNING)
    # Recovery hands the run to a newer job while this one is mid-batch.
    Run.objects.filter(pk=run.pk).update(generation=1)

    with pytest.raises(runner.LostOwnership):
        executor._checkpoint(3)

    run.refresh_from_db()
    assert run.tick_count == 0


def test_only_one_active_run_per_task():
    Run.objects.create(task_name=tasks.SumNumbers.name, status=Status.PAUSED)

    with pytest.raises(runner.ActiveRunExists):
        runner.start(tasks.SumNumbers.name)


def test_invalid_transition():
    run = Run.objects.create(task_name=tasks.SumNumbers.name, status=Status.SUCCEEDED)

    with pytest.raises(runner.InvalidTransition):
        runner.pause(run)


def test_unknown_task():
    with pytest.raises(UnknownTask):
        runner.start("nope.Missing")


def test_management_command(capsys):
    call_command("maintenance_tasks", "list")
    assert tasks.BackfillSlugs.name in capsys.readouterr().out

    make_posts(3)
    call_command("maintenance_tasks", "run", tasks.BackfillSlugs.name)

    assert Run.objects.get().status == Status.SUCCEEDED

    Run.objects.update(
        status=Status.RUNNING, heartbeat_at=timezone.now() - timedelta(hours=1)
    )
    call_command("maintenance_tasks", "recover")
    assert "Recovered" in capsys.readouterr().out


def test_admin_start_and_changelist(admin_client):
    make_posts(3)

    response = admin_client.post(
        "/admin/maintenance_tasks/run/start/",
        {"task_name": tasks.BackfillSlugs.name, "arguments": "{}"},
    )

    assert response.status_code == 302
    run = Run.objects.get()
    assert run.status == Status.SUCCEEDED
    assert run.started_by.username == "admin"
    changelist = admin_client.get("/admin/maintenance_tasks/run/")
    assert b"Start a run" in changelist.content
    assert b"3 / 3 (100%)" in changelist.content
