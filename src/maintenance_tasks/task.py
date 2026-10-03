from django.db.models import QuerySet

_registry: dict[str, type["MaintenanceTask"]] = {}


class UnknownTask(LookupError):
    pass


class MaintenanceTask:
    """
    Base class for a data backfill.

    Subclass it in <app>/maintenance_tasks.py and implement collection() and
    process(). The runner walks the collection in batches and saves a cursor
    after each one, so a run can be paused, resumed and survive restarts.

    QuerySet collections are walked in primary key order, whatever ordering
    the QuerySet has. Any other iterable is walked by position, so it must
    return the same items in the same order every time it is called.
    """

    # Set abstract = True on a subclass that should not be registered.
    abstract = True
    name: str
    description = ""
    batch_size = 100
    throttle_backoff = 30  # seconds to wait when throttle() returns True

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("abstract", False):
            return
        cls.name = f"{cls.__module__}.{cls.__qualname__}"
        _registry[cls.name] = cls

    def __init__(self, arguments=None):
        self.arguments = arguments or {}

    def collection(self):
        """Return the items to process: a QuerySet, list or other iterable."""
        raise NotImplementedError

    def process(self, item):
        """Process a single item from the collection."""
        raise NotImplementedError

    def count(self):
        """Return the total number of items, or None if unknown."""
        collection = self.collection()
        if isinstance(collection, QuerySet):
            return collection.count()
        try:
            return len(collection)
        except TypeError:
            return None

    def throttle(self):
        """Return True to pause for throttle_backoff seconds, e.g. when the
        database is under load. Checked between batches."""
        return False


def get_task(name):
    try:
        return _registry[name]
    except KeyError:
        raise UnknownTask(f"No maintenance task named {name!r}.") from None


def all_tasks():
    return sorted(_registry.values(), key=lambda cls: cls.name)
