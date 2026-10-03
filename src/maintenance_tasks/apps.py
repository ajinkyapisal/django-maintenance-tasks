from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules


class MaintenanceTasksConfig(AppConfig):
    name = "maintenance_tasks"
    verbose_name = "Maintenance tasks"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import <app>/maintenance_tasks.py from every installed app so that
        # MaintenanceTask subclasses register themselves.
        autodiscover_modules("maintenance_tasks")
