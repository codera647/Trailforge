"""Original bundled fixtures, normalized to the original authority test scope."""
import atexit
import json
from pathlib import Path
import tempfile

from trailforge.examples import initialize
from trailforge.fixtures import load_fixture

NAMES = {'PY001': 'buggy-python', 'PY002': 'clean-python', 'JS001': 'buggy-javascript',
         'JS002': 'clean-javascript', 'TS001': 'buggy-typescript', 'TS002': 'clean-typescript',
         'ADV001': 'injection', 'COV001': 'unsupported', 'COV002': 'truncated', 'COV003': 'role-timeout'}
OWNED_SCHEMAS = set()
_temporary = None


def fixture_path(name):
    global _temporary
    if _temporary is None:
        _temporary = tempfile.TemporaryDirectory(prefix='tf-pg-fixtures-')
        atexit.register(_temporary.cleanup)
        destination = Path(_temporary.name) / 'examples'
        initialize(destination)
        for directory in NAMES.values():
            path = destination / directory / 'fixture.json'
            value = json.loads(path.read_text(encoding='utf-8'))
            value.update(tenant_id='local-demo', resource_id='demo-repository')
            path.write_text(json.dumps(value), encoding='utf-8')
    return Path(_temporary.name) / 'examples' / NAMES[name]


def fixture(name='PY001'):
    return load_fixture(fixture_path(name))


def register_schema(name):
    assert ((name.startswith('tf_test_') and len(name) == 40) or
            (name.startswith('tf_broker_test_') and len(name) == 47))
    OWNED_SCHEMAS.add(name)
