from django.utils.text import slugify

from maintenance_tasks import MaintenanceTask
from maintenance_tasks.models import Run
from tests.testapp.models import Post


class BackfillSlugs(MaintenanceTask):
    description = "Fill in missing post slugs."
    batch_size = 10

    def collection(self):
        return Post.objects.filter(slug="")

    def process(self, post):
        post.slug = slugify(post.title)
        post.save(update_fields=["slug"])


class SumNumbers(MaintenanceTask):
    """Walks a plain list, using the position as the cursor."""

    batch_size = 3
    seen = []

    def collection(self):
        return list(range(self.arguments.get("size", 10)))

    def process(self, number):
        self.seen.append(number)


class FailOnSeven(MaintenanceTask):
    batch_size = 5

    def collection(self):
        return list(range(10))

    def process(self, number):
        if number == 7:
            raise ValueError("seven is unlucky")


class RequestDuringRun(MaintenanceTask):
    """Requests a pause or cancel (arguments["status"]) while processing item
    arguments["at"], like a user clicking the button mid-run."""

    batch_size = 10

    def collection(self):
        return list(range(100))

    def process(self, number):
        if number == self.arguments["at"]:
            Run.objects.filter(task_name=self.name, status=Run.Status.RUNNING).update(
                status=self.arguments["status"]
            )


class ThrottledOnce(MaintenanceTask):
    batch_size = 5
    throttle_backoff = 0
    calls = 0

    def collection(self):
        return list(range(10))

    def process(self, number):
        pass

    def throttle(self):
        type(self).calls += 1
        return type(self).calls == 2
