"""One desktop Manager per portable data directory."""
from contextlib import contextmanager
from pathlib import Path
from .config import file_lock


@contextmanager
def manager_instance(home):
    with file_lock(Path(home)/'prodcast-data'/'manager-ui.lock'):
        yield
