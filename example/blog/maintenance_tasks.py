import time
from pathlib import Path

from django.utils.text import slugify

from example.blog.models import Post
from maintenance_tasks import MaintenanceTask


class BackfillSlugs(MaintenanceTask):
    """Fills in missing slugs.

    Arguments:
        delay: seconds to sleep per post, to make the run slow enough to watch.
        throttle_file: while this file exists, the task backs off.
        fail_at: raise when processing the post with this title.
    """

    description = "Fill in missing post slugs."
    batch_size = 100
    throttle_backoff = 2

    def collection(self):
        return Post.objects.filter(slug="")

    def process(self, post):
        if post.title == self.arguments.get("fail_at"):
            raise ValueError(f"cannot slugify {post.title!r}")
        time.sleep(self.arguments.get("delay", 0))
        post.slug = slugify(post.title)
        post.save(update_fields=["slug"])

    def throttle(self):
        path = self.arguments.get("throttle_file")
        return bool(path) and Path(path).exists()


class NormalizeTitles(MaintenanceTask):
    """Strips and title-cases every post title. Argument: delay."""

    description = "Strip whitespace and title-case post titles."
    batch_size = 100

    def collection(self):
        return Post.objects.all()

    def process(self, post):
        time.sleep(self.arguments.get("delay", 0))
        post.title = post.title.strip().title()
        post.save(update_fields=["title"])


class RecomputeSlugs(MaintenanceTask):
    """Recomputes every slug. Arguments: delay, fail_at (a post title)."""

    description = "Recompute slugs for all posts."
    batch_size = 100

    def collection(self):
        return Post.objects.all()

    def process(self, post):
        if post.title.strip().lower() == str(self.arguments.get("fail_at", "")).lower():
            raise ValueError(f"cannot slugify {post.title!r}")
        time.sleep(self.arguments.get("delay", 0))
        post.slug = slugify(post.title)
        post.save(update_fields=["slug"])
