import logging
import time
import traceback
from datetime import timedelta
from itertools import islice

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, QuerySet
from django.tasks import task
from django.utils import timezone

from maintenance_tasks.models import Run
from maintenance_tasks.task import get_task

logger = logging.getLogger(__name__)

Status = Run.Status


class ActiveRunExists(Exception):
    pass


class InvalidTransition(Exception):
    pass


def _setting(name, default):
    return getattr(settings, "MAINTENANCE_TASKS", {}).get(name, default)


def max_runtime():
    """Seconds a single batch job may run before it re-enqueues itself, so
    one backfill never holds a worker for hours."""
    return _setting("MAX_RUNTIME", 120)


def stale_after():
    """Seconds without a heartbeat after which a run counts as stalled, for
    example because its worker was killed. Keep it well above the time one
    batch takes."""
    return _setting("STALE_AFTER", 600)


def start(task_name, arguments=None):
    task_cls = get_task(task_name)
    arguments = arguments or {}
    try:
        with transaction.atomic():
            run = Run.objects.create(
                task_name=task_name,
                arguments=arguments,
                tick_total=task_cls(arguments).count(),
                heartbeat_at=timezone.now(),
            )
    except IntegrityError:
        raise ActiveRunExists(f"{task_name} already has an active run.") from None
    _enqueue(run.pk, run.generation)
    return run


def pause(run):
    _transition(run, [Status.ENQUEUED, Status.RUNNING], Status.PAUSING)


def resume(run):
    _transition(run, [Status.PAUSED], Status.ENQUEUED, new_job=True)
    _enqueue(run.pk, run.generation)


def cancel(run):
    # A paused run has no job in the queue to notice the request, so finish
    # cancelling it here.
    if Run.objects.filter(pk=run.pk, status=Status.PAUSED).update(
        status=Status.CANCELLED, ended_at=timezone.now()
    ):
        run.refresh_from_db()
        return
    _transition(
        run, [Status.ENQUEUED, Status.RUNNING, Status.PAUSING], Status.CANCELLING
    )


def recover_stalled(runs=None):
    """
    Recover runs whose job has gone missing, e.g. because the worker was
    killed or the queue lost the job. Running and enqueued runs are put back
    on the queue from their saved cursor; pending pauses and cancels are
    completed. Returns the recovered runs.

    Each recovery starts a new generation, so if the old job is in fact still
    alive, it stops at its next checkpoint instead of running twice.
    """
    runs = Run.objects.all() if runs is None else runs
    stalled = runs.filter(
        status__in=[Status.ENQUEUED, Status.RUNNING, Status.PAUSING, Status.CANCELLING],
        heartbeat_at__lt=timezone.now() - timedelta(seconds=stale_after()),
    )
    recovered = []
    for run in stalled:
        now = timezone.now()
        if run.status in (Status.ENQUEUED, Status.RUNNING):
            changes = {"status": Status.ENQUEUED, "heartbeat_at": now}
        elif run.status == Status.PAUSING:
            changes = {"status": Status.PAUSED}
        else:
            changes = {"status": Status.CANCELLED, "ended_at": now}
        if not Run.objects.filter(
            pk=run.pk, status=run.status, generation=run.generation
        ).update(generation=F("generation") + 1, **changes):
            continue  # It changed under us, so it is not stalled.
        run.refresh_from_db()
        logger.warning("Recovered stalled maintenance task run %s", run.pk)
        if run.status == Status.ENQUEUED:
            _enqueue(run.pk, run.generation)
        recovered.append(run)
    return recovered


def _transition(run, from_statuses, to_status, new_job=False):
    changes = {"status": to_status}
    if new_job:
        changes.update(generation=F("generation") + 1, heartbeat_at=timezone.now())
    updated = Run.objects.filter(pk=run.pk, status__in=from_statuses).update(**changes)
    run.refresh_from_db()
    if not updated:
        raise InvalidTransition(f"Cannot move {run} to {to_status}.")


def _enqueue(run_id, generation, delay=0):
    def enqueue():
        job = run_batch
        if delay:
            if job.get_backend().supports_defer:
                job = job.using(run_after=timezone.now() + timedelta(seconds=delay))
            else:
                time.sleep(delay)
        job.enqueue(run_id, generation)

    # The worker must not pick up the job before the Run row is committed.
    transaction.on_commit(enqueue)


@task()
def run_batch(run_id, generation):
    Executor(Run.objects.get(pk=run_id), generation).execute()


class LostOwnership(Exception):
    """The run was recovered and handed to a newer job."""


class Executor:
    def __init__(self, run, generation):
        self.run = run
        self.generation = generation
        self.task = get_task(run.task_name)(run.arguments)

    def _mine(self, **filters):
        """This run, only while this job still owns it."""
        return Run.objects.filter(pk=self.run.pk, generation=self.generation, **filters)

    def execute(self):
        run = self.run
        if not self._mine(status=Status.ENQUEUED).update(
            status=Status.RUNNING,
            started_at=run.started_at or timezone.now(),
            heartbeat_at=timezone.now(),
        ):
            # A stale or duplicate job, or paused or cancelled while waiting
            # in the queue.
            self._stop_if_requested()
            return

        deadline = time.monotonic() + max_runtime()
        pending = 0
        try:
            if self.task.throttle():
                self._requeue(self.task.throttle_backoff)
                return
            for cursor, item in self._iterate():
                self.task.process(item)
                run.cursor = cursor
                pending += 1
                if pending < self.task.batch_size:
                    continue
                self._checkpoint(pending)
                pending = 0
                if self._stop_if_requested():
                    return
                if self.task.throttle():
                    self._requeue(self.task.throttle_backoff)
                    return
                if time.monotonic() >= deadline:
                    self._requeue()
                    return
        except LostOwnership:
            logger.warning("Run %s was recovered by a newer job; stopping.", run.pk)
            return
        except Exception as error:
            logger.exception("Maintenance task run %s errored", run.pk)
            self._mine().update(
                status=Status.ERRORED,
                cursor=run.cursor,
                tick_count=F("tick_count") + pending,
                error_class=f"{type(error).__module__}.{type(error).__qualname__}",
                error_message=str(error),
                backtrace="".join(traceback.format_exception(error)),
                ended_at=timezone.now(),
            )
            return

        self._mine().update(
            status=Status.SUCCEEDED,
            cursor=run.cursor,
            tick_count=F("tick_count") + pending,
            ended_at=timezone.now(),
        )

    def _iterate(self):
        """Yield (cursor, item) pairs for the items after the saved cursor."""
        collection = self.task.collection()
        cursor = self.run.cursor
        if isinstance(collection, QuerySet):
            collection = collection.order_by("pk")
            while True:
                page = collection if cursor is None else collection.filter(pk__gt=cursor)
                items = list(page[: self.task.batch_size])
                if not items:
                    return
                for item in items:
                    cursor = item.pk if isinstance(item.pk, int) else str(item.pk)
                    yield cursor, item
        else:
            start = 0 if cursor is None else cursor + 1
            yield from enumerate(islice(collection, start, None), start=start)

    def _checkpoint(self, ticks):
        if not self._mine().update(
            cursor=self.run.cursor,
            tick_count=F("tick_count") + ticks,
            heartbeat_at=timezone.now(),
        ):
            raise LostOwnership

    def _stop_if_requested(self):
        """Finish a pause or cancel requested from outside. Returns True if
        the run should stop."""
        if self._mine(status=Status.PAUSING).update(status=Status.PAUSED):
            return True
        return bool(
            self._mine(status=Status.CANCELLING).update(
                status=Status.CANCELLED, ended_at=timezone.now()
            )
        )

    def _requeue(self, delay=0):
        if self._mine(status=Status.RUNNING).update(
            status=Status.ENQUEUED,
            generation=F("generation") + 1,
            heartbeat_at=timezone.now(),
        ):
            _enqueue(self.run.pk, self.generation + 1, delay)
        else:
            self._stop_if_requested()
