"""Trusted test worker; hard-exit after durable mock admission, bypassing finally."""

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent

from trailforge.fixtures import load_fixture
from trailforge.adapters.mock import FixtureModel
from trailforge.adapters.postgres import PostgresRunStore
from trailforge.controller import OfflineController


class HardCrash(FixtureModel):
    def review(self, snapshot, configured_output):
        os._exit(23)


if __name__ == "__main__":
    schema, run_id, fixture_directory = sys.argv[1:]
    store = PostgresRunStore(os.environ["TRAILFORGE_DATABASE_URL"], "local-demo", "demo-repository",
                             schema=schema, lease_seconds=5)
    review_input = load_fixture(fixture_directory)
    OfflineController(store, HardCrash()).advance(run_id, review_input)
