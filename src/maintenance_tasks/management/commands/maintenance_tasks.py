import json

from django.core.management.base import BaseCommand, CommandError

from maintenance_tasks import runner
from maintenance_tasks.task import UnknownTask, all_tasks


class Command(BaseCommand):
    help = "List maintenance tasks or start a run."

    def add_arguments(self, parser):
        subcommands = parser.add_subparsers(dest="subcommand", required=True)
        subcommands.add_parser("list", help="List registered tasks.")
        subcommands.add_parser(
            "recover",
            help="Re-enqueue runs whose worker died. Safe to run from cron.",
        )
        run = subcommands.add_parser("run", help="Start a run of a task.")
        run.add_argument("task_name")
        run.add_argument(
            "--arguments",
            default="{}",
            help="JSON object, available to the task as self.arguments.",
        )

    def handle(self, *args, subcommand, **options):
        if subcommand == "list":
            for cls in all_tasks():
                line = cls.name
                if cls.description:
                    line += f"  {cls.description}"
                self.stdout.write(line)
            return
        if subcommand == "recover":
            for run in runner.recover_stalled():
                self.stdout.write(f"Recovered {run}.")
            return

        try:
            arguments = json.loads(options["arguments"])
        except json.JSONDecodeError as error:
            raise CommandError(f"--arguments is not valid JSON: {error}")
        if not isinstance(arguments, dict):
            raise CommandError("--arguments must be a JSON object.")
        try:
            run = runner.start(options["task_name"], arguments)
        except (UnknownTask, runner.ActiveRunExists) as error:
            raise CommandError(str(error))
        self.stdout.write(f"Started {run}.")
