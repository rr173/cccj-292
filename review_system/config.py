"""Runtime configuration.

``DB_PATH`` is mutable module-level state so the application factory (and the
test suite, which gives every test a fresh database) can retarget the store
without rebuilding router closures.
"""

import os

# Default location for the runnable server; tests override via create_app(db_path=...).
DB_PATH: str = os.environ.get("REVIEW_DB_PATH", os.path.join("data", "review.db"))
