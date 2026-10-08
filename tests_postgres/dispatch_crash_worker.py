"""Trusted test process: record mock queue acceptance then exit without cleanup."""
import json
import os
from pathlib import Path
import sys

from trailforge.adapters.postgres import PostgresRunStore

store = PostgresRunStore(os.environ["TRAILFORGE_DATABASE_URL"], "local-demo", "demo-repository",
                         schema=sys.argv[1], lease_seconds=5)
_, message = store.claim_dispatch()
Path(sys.argv[2]).write_text(json.dumps(message), encoding="utf-8")
os._exit(23)
