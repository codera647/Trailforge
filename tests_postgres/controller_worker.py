"""Trusted installed-package worker for separate-process PostgreSQL recovery."""
import json
import os
import sys
from trailforge.adapters.mock import FixtureModel
from trailforge.adapters.postgres import PostgresRunStore
from trailforge.controller import OfflineController
from trailforge.fixtures import load_fixture

schema, directory, action, *run_ids = sys.argv[1:]
store = PostgresRunStore(os.environ['TRAILFORGE_DATABASE_URL'], 'local-demo', 'demo-repository', schema=schema)
model = OfflineController(store, FixtureModel())
review_input = load_fixture(directory)
run_id = model.start(review_input) if action == 'pause' else run_ids[0]
record = model.advance(run_id, review_input, 'snapshot' if action == 'pause' else None)
print(json.dumps(record['draft'] or {'run_id': run_id, 'stage': record['stage']}))
