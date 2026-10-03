from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.utils.html import format_html
from django.template.response import TemplateResponse
from django.urls import path, reverse

from maintenance_tasks import runner
from maintenance_tasks.models import Run
from maintenance_tasks.task import all_tasks


class StartRunForm(forms.Form):
    task_name = forms.ChoiceField(label="Task")
    arguments = forms.JSONField(
        required=False,
        initial=dict,
        help_text="JSON object, available to the task as self.arguments.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["task_name"].choices = [
            (cls.name, cls.name) for cls in all_tasks()
        ]

    def clean_arguments(self):
        arguments = self.cleaned_data["arguments"] or {}
        if not isinstance(arguments, dict):
            raise forms.ValidationError("Enter a JSON object.")
        return arguments


@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    change_list_template = "admin/maintenance_tasks/run/change_list.html"
    list_display = [
        "id",
        "task_name",
        "status",
        "progress_display",
        "started_by",
        "created_at",
        "ended_at",
    ]
    list_filter = ["status", "task_name"]
    search_fields = ["task_name"]
    actions = ["pause_runs", "resume_runs", "cancel_runs", "recover_runs"]
    readonly_fields = [field.name for field in Run._meta.fields] + ["progress_display"]

    def has_add_permission(self, request):
        # Runs are started from the "Start a run" page, not the add form.
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_manage_permission(self, request):
        # Starting, pausing, resuming and cancelling all use the "add run"
        # permission, since the add form itself is disabled.
        return request.user.has_perm("maintenance_tasks.add_run")

    @admin.display(description="Progress")
    def progress_display(self, run):
        if run.progress is None:
            return run.tick_count
        return format_html(
            '<span style="white-space: nowrap"><progress value="{}" max="100" style="width: 8em; vertical-align: middle">'
            "</progress> {} / {} ({}%)</span>",
            run.progress,
            run.tick_count,
            run.tick_total,
            run.progress,
        )

    def get_urls(self):
        return [
            path(
                "start/",
                self.admin_site.admin_view(self.start_view),
                name="maintenance_tasks_run_start",
            ),
        ] + super().get_urls()

    def start_view(self, request):
        if not self.has_manage_permission(request):
            raise PermissionDenied
        form = StartRunForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                run = runner.start(
                    form.cleaned_data["task_name"],
                    form.cleaned_data["arguments"],
                    started_by=request.user,
                )
            except runner.ActiveRunExists as error:
                form.add_error("task_name", str(error))
            else:
                self.message_user(request, f"Started {run}.", messages.SUCCESS)
                return redirect(reverse("admin:maintenance_tasks_run_changelist"))
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Start a maintenance task",
            "form": form,
        }
        return TemplateResponse(request, "admin/maintenance_tasks/run/start.html", context)

    def _apply(self, request, queryset, action, verb):
        done = 0
        for run in queryset:
            try:
                action(run)
            except runner.InvalidTransition as error:
                self.message_user(request, str(error), messages.WARNING)
            else:
                done += 1
        if done:
            self.message_user(request, f"{verb} {done} run(s).", messages.SUCCESS)

    @admin.action(description="Pause selected runs", permissions=["manage"])
    def pause_runs(self, request, queryset):
        self._apply(request, queryset, runner.pause, "Paused")

    @admin.action(description="Resume selected runs", permissions=["manage"])
    def resume_runs(self, request, queryset):
        self._apply(request, queryset, runner.resume, "Resumed")

    @admin.action(description="Cancel selected runs", permissions=["manage"])
    def cancel_runs(self, request, queryset):
        self._apply(request, queryset, runner.cancel, "Cancelled")

    @admin.action(description="Recover selected stalled runs", permissions=["manage"])
    def recover_runs(self, request, queryset):
        recovered = runner.recover_stalled(queryset)
        self.message_user(
            request,
            f"Recovered {len(recovered)} stalled run(s)."
            if recovered
            else "None of the selected runs are stalled.",
            messages.SUCCESS if recovered else messages.WARNING,
        )
