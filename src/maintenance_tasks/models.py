from django.db import models
from django.db.models import Q


class Run(models.Model):
    class Status(models.TextChoices):
        ENQUEUED = "enqueued"
        RUNNING = "running"
        PAUSING = "pausing"
        PAUSED = "paused"
        CANCELLING = "cancelling"
        CANCELLED = "cancelled"
        SUCCEEDED = "succeeded"
        ERRORED = "errored"

    ACTIVE_STATUSES = [
        Status.ENQUEUED,
        Status.RUNNING,
        Status.PAUSING,
        Status.PAUSED,
        Status.CANCELLING,
    ]

    task_name = models.CharField(max_length=255, db_index=True)
    arguments = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=Status, default=Status.ENQUEUED, db_index=True
    )
    # Primary key (QuerySets) or position (other iterables) of the last
    # processed item.
    cursor = models.JSONField(null=True, blank=True)
    tick_count = models.PositiveBigIntegerField(default=0)
    tick_total = models.PositiveBigIntegerField(null=True, blank=True)
    # Bumped every time a new job is enqueued for this run. A job only acts
    # while its generation matches, so stale or duplicate jobs do nothing.
    generation = models.PositiveIntegerField(default=0)
    # Updated when a job is enqueued, starts, and after every batch. Used to
    # spot runs whose worker died.
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    error_class = models.CharField(max_length=255, blank=True)
    error_message = models.TextField(blank=True)
    backtrace = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["task_name"],
                condition=Q(
                    status__in=[
                        "enqueued",
                        "running",
                        "pausing",
                        "paused",
                        "cancelling",
                    ]
                ),
                name="maintenance_tasks_one_active_run_per_task",
            )
        ]

    def __str__(self):
        return f"{self.task_name} #{self.pk} ({self.status})"

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    @property
    def progress(self):
        """Percentage complete, or None if the total is unknown."""
        if not self.tick_total:
            return None
        return min(100, round(100 * self.tick_count / self.tick_total))
