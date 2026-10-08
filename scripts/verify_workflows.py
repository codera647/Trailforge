"""Strict read-only CI contract for both repository and standalone exports.

Workflow files use JSON, a YAML subset, to permit dependency-free inspection.
This is repo-owned configuration checking, not independently protected acceptance.
"""
from copy import deepcopy
import argparse
import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
CHECKOUT = 'actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd'
PYTHON = 'actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c'
UPLOAD = 'actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f'
POSTGRES = 'postgres@sha256:fc973eb97c9fd04bfa1840e0f510719a584ccb3be8debfe6a4144637a9dfe8cf'
IMAGE = 'docker.io/library/python@sha256:f040863673aea2570c3ff6a5c3fb4c673a016cbc5375005ad145915922b6b78a'
DSN = 'postgresql://trailforge_ci:ephemeral-ci-only@127.0.0.1:5432/trailforge_ci'


def workflow(monorepo=False):
    def steps(python='3.13.15'):
        return [{'uses': CHECKOUT, 'with': {'persist-credentials': False}},
                {'uses': PYTHON, 'with': {'python-version': python}},
                {'run': 'python -m pip install -r requirements-dev.lock'},
                {'run': 'python scripts/verify_workflows.py' + (' --monorepo' if monorepo else '')},
                {'run': 'python scripts/verify_local.py'}]
    def upload(name, files):
        prefix = 'packages/trailforge/' if monorepo else ''
        return {'if': 'always()', 'uses': UPLOAD, 'with': {
            'name': name, 'path': '\n'.join(prefix + '.local/verification/' + f for f in files),
            'include-hidden-files': True, 'if-no-files-found': 'warn', 'retention-days': 7}}
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    base_files = ['report.json', 'standalone-suite.json', f'trailforge-{version}-py3-none-any.whl', f'trailforge-{version}.tar.gz']
    portable = {'strategy': {'fail-fast': False, 'matrix': {'os': ['ubuntu-24.04', 'windows-2022'], 'python': ['3.11', '3.13.15']}},
                'runs-on': '${{ matrix.os }}', 'timeout-minutes': 15,
                'steps': steps('${{ matrix.python }}') + [
                    {'if': "matrix.os == 'ubuntu-24.04' && matrix.python == '3.13.15'", 'run': 'python scripts/verify_optional.py'},
                    upload('portable-${{ matrix.os }}-${{ matrix.python }}', base_files + ['optional-report.json', 'optional-suite.json'])]}
    postgres = {'runs-on': 'ubuntu-24.04', 'timeout-minutes': 20,
                'services': {'postgres': {'image': POSTGRES,
                    'env': {'POSTGRES_USER': 'trailforge_ci', 'POSTGRES_PASSWORD': 'ephemeral-ci-only', 'POSTGRES_DB': 'trailforge_ci'},
                    'ports': ['5432:5432'],
                    'options': '--health-cmd "pg_isready -U trailforge_ci -d trailforge_ci" --health-interval 5s --health-timeout 5s --health-retries 12'}},
                'steps': steps() + [{'run': 'python scripts/verify_postgres.py', 'env': {'TRAILFORGE_DATABASE_URL': DSN}},
                                    upload('postgres-installed', base_files + ['postgres-report.json', 'postgres-suite.json'])]}
    sandbox = {'runs-on': 'ubuntu-24.04', 'timeout-minutes': 15,
               'steps': steps() + [{'run': 'docker pull ' + IMAGE},
                                   {'run': 'python scripts/verify_linux.py --image ' + IMAGE},
                                   upload('linux-installed', base_files + ['sandbox-report.json'])]}
    if monorepo:
        for job in (portable, postgres, sandbox):
            job['defaults'] = {'run': {'working-directory': 'packages/trailforge'}}
    return {'name': 'Trailforge conformance', 'on': {'pull_request': None, 'push': None, 'workflow_dispatch': None},
            'permissions': {'contents': 'read'},
            'concurrency': {'group': 'trailforge-${{ github.workflow }}-${{ github.ref }}', 'cancel-in-progress': True},
            'jobs': {'portable': portable, 'postgres': postgres, 'sandbox': sandbox}}


def validate(candidate, monorepo=False):
    if candidate != workflow(monorepo):
        raise ValueError('CI violates the admitted read-only conformance contract')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--monorepo', action='store_true')
    args = parser.parse_args()
    paths = [(ROOT / '.github/workflows/conformance.yml', False)]
    if args.monorepo:
        paths.append((ROOT.parents[1] / '.github/workflows/trailforge.yml', True))
    mutations = [
        lambda w: w['on'].update(pull_request_target=None),
        lambda w: w['permissions'].update(contents='write'),
        lambda w: w['jobs']['portable']['steps'][0]['with'].update({'persist-credentials': True}),
        lambda w: w['jobs']['portable']['steps'][1].update(uses='actions/setup-python@main'),
        lambda w: w['jobs']['postgres']['steps'][-2]['env'].update(CLOUD_TOKEN='${{ secrets.CLOUD_TOKEN }}'),
        lambda w: w['jobs']['sandbox']['steps'].append({'run': 'deploy'}),
        lambda w: w['jobs']['postgres']['steps'][-1]['with'].update(path='.local/**'),
        lambda w: w['jobs']['portable'].update({'continue-on-error': True}),
        lambda w: w['jobs']['postgres']['services']['postgres'].update(image='postgres:latest'),
    ]
    for path, monorepo in paths:
        candidate = json.loads(path.read_text(encoding='utf-8'))
        validate(candidate, monorepo)
        for mutate in mutations:
            invalid = deepcopy(candidate)
            mutate(invalid)
            try:
                validate(invalid, monorepo)
            except ValueError:
                continue
            raise RuntimeError('unsafe CI mutation admitted')
    print(json.dumps({'status': 'PASS', 'workflows': len(paths), 'negative_cases': len(paths) * len(mutations),
                      'independent_acceptance': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
