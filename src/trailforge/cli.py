"""Standalone offline CLI. Host/provider adapters are explicit library composition."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

from . import __version__
from .adapters.mock import FixtureModel
from .controller import OfflineController
from .contracts import validate_checkpoint
from .core import ContractError, CONFIG, digest, fields, nonempty, source_path, strict_json
from .evidence import validate_output, verify_snapshot
from .examples import initialize
from .fixtures import _bounded_bytes, _safe_file, load_fixture
from .store import SQLiteRunStore
from .publication import DurableSimulationPublisher, GuardedPublisher, Intent


def inspect_run(store, run_id, *, include_source=False):
    record = store.load(run_id)
    validate_checkpoint(record)
    if record['snapshot'] is not None:
        verify_snapshot(record['snapshot'], run_id, record['binding'])
    if record['review'] is not None:
        if digest(record['review']['output']) != record['review']['digest']:
            raise ContractError('review digest mismatch')
    if record['validation'] is not None:
        expected = validate_output(record['review']['output'], record['snapshot'], run_id, record['binding'])
        if expected != record['validation']:
            raise ContractError('validation differs from recorded evidence')
    if record['draft'] is not None:
        if (OfflineController._draft(record) != record['draft']
                or record['outcome'] != record['draft']['workflow_outcome']):
            raise ContractError('draft differs from recorded evidence')
    visible = {key: value for key, value in record.items() if key not in ('snapshot', 'review', 'validation')}
    if include_source:
        visible = record
    visible = {**visible, 'events': store.events(run_id), 'provider_origin': 'LOCAL_MOCK',
               'guarantee_mode': 'TRUSTED_LOCAL_SQLITE', 'external_effect': 'NONE'}
    return visible


def evaluate_suite(directory):
    directory = Path(directory)
    if directory.is_symlink() or (hasattr(directory, 'is_junction') and directory.is_junction()):
        raise ContractError('suite root cannot be a link/junction')
    root = directory.resolve()
    try:
        suite = strict_json(_bounded_bytes(_safe_file(root, 'suite.json'), CONFIG['max_manifest_bytes']).decode('utf-8'))
    except UnicodeError:
        raise ContractError('suite must be UTF-8') from None
    fields(suite, {'schema_version', 'origin', 'cases'})
    if suite['schema_version'] != 'trailforge/smoke-suite/0.1' or suite['origin'] != 'ORIGINAL_SYNTHETIC_MOCK':
        raise ContractError('unsupported suite protocol/origin')
    cases = suite['cases']
    if type(cases) is not list or not 1 <= len(cases) <= 32:
        raise ContractError('invalid suite case count')
    results, seen = [], set()
    with TemporaryDirectory(prefix='trailforge-smoke-') as temporary:
        store = SQLiteRunStore(Path(temporary) / 'runs.sqlite3')
        for case in cases:
            fields(case, {'id', 'fixture', 'expected_outcome', 'expected_findings'})
            name = nonempty(case['id'])
            if name in seen or case['expected_outcome'] not in ('COMPLETE', 'PARTIAL'):
                raise ContractError('duplicate case or invalid expected outcome')
            seen.add(name)
            if type(case['expected_findings']) is not int or not 0 <= case['expected_findings'] <= CONFIG['max_findings']:
                raise ContractError('invalid expected finding count')
            relative = source_path(case['fixture'])
            manifest = _safe_file(root, relative + '/fixture.json')
            review_input = load_fixture(manifest.parent)
            controller = OfflineController(store, FixtureModel())
            run_id = controller.start(review_input)
            record = controller.advance(run_id, review_input)
            expected = (case['expected_outcome'], case['expected_findings'])
            observed = (record['outcome'], len(record['draft']['findings']))
            results.append({'id': name, 'status': 'PASS' if expected == observed else 'FAIL',
                            'outcome': record['outcome'], 'findings': observed[1],
                            'model_calls': record['model_calls']})
    return {'schema_version': 'trailforge/smoke-report/0.1',
            'status': 'PASS' if all(case['status'] == 'PASS' for case in results) else 'FAIL',
            'origin': 'ORIGINAL_SYNTHETIC_MOCK', 'model_quality_claim': False, 'cases': results}


def parser():
    root = argparse.ArgumentParser(description='Trailforge bounded execution: standalone offline Review')
    root.add_argument('--version', action='version', version=__version__)
    commands = root.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Create original offline examples in a new directory')
    init.add_argument('directory', nargs='?', default='trailforge-demo')
    for name in ('run', 'resume'):
        command = commands.add_parser(name, help='Run/resume a bounded offline fixture')
        command.add_argument('--fixture', required=True)
        command.add_argument('--store', default='trailforge.sqlite3')
        command.add_argument('--pause-after', choices=('snapshot', 'review', 'validation'))
        if name == 'resume':
            command.add_argument('--run-id', required=True)
    for name in ('inspect', 'export'):
        command = commands.add_parser(name, help='Inspect/export a verified local run')
        command.add_argument('--run-id', required=True)
        command.add_argument('--store', default='trailforge.sqlite3')
        command.add_argument('--include-source', action='store_true', help='Include full admitted source text')
        if name == 'export':
            command.add_argument('--output', required=True)
    evaluate = commands.add_parser('evaluate', help='Run a synthetic mock smoke suite; no model-quality claim')
    evaluate.add_argument('--suite', required=True)
    for name in ('approve', 'reconcile'):
        command = commands.add_parser(name, help='Exact approval/publication recovery with a durable local simulator only')
        command.add_argument('--run-id', required=True)
        command.add_argument('--store', default='trailforge.sqlite3')
        command.add_argument('--simulate', action='store_true', required=True,
                             help='Required: this CLI never performs real provider writes')
        if name == 'approve':
            command.add_argument('--actor', required=True, help='Local operator label; not verified customer identity')
            command.add_argument('--lose-response', action='store_true', help='Simulate acceptance followed by lost acknowledgement')
        else:
            command.add_argument('--operation-id', required=True)
    return root


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    args = parser().parse_args(argv)
    try:
        if args.command == 'init':
            result = initialize(args.directory)
        elif args.command == 'evaluate':
            result = evaluate_suite(args.suite)
        else:
            if args.command in ('resume', 'inspect', 'export', 'approve', 'reconcile') and not Path(args.store).is_file():
                raise ContractError('store does not exist')
            store = SQLiteRunStore(args.store)
            if args.command in ('run', 'resume'):
                review_input = load_fixture(args.fixture)
                controller = OfflineController(store, FixtureModel())
                run_id = controller.start(review_input) if args.command == 'run' else args.run_id
                print(json.dumps({'run_id': run_id, 'store': str(store.path)}, sort_keys=True), file=sys.stderr)
                record = controller.advance(run_id, review_input, args.pause_after)
                result = record['draft'] or {'run_id': run_id, 'state': record['state'], 'stage': record['stage'],
                                             'model_calls': record['model_calls'], 'paused': True}
            else:
                result = inspect_run(store, args.run_id, include_source=getattr(args, 'include_source', False))
                if args.command in ('approve', 'reconcile'):
                    if result['outcome'] != 'COMPLETE' or result['draft'] is None:
                        raise ContractError('only a fully validated draft can enter this simulator')
                    binding = result['binding']
                    intent = Intent(binding['tenant_id'], binding['resource_id'], digest(binding),
                                    binding['policy_digest'], binding['head_revision'], 'DRAFT', result['draft'])
                    adapter = DurableSimulationPublisher(str(store.path) + '.simulation.sqlite3',
                                                         lose_response=getattr(args, 'lose_response', False))
                    publisher = GuardedPublisher(store.path, tenant_id=binding['tenant_id'], resource_id=binding['resource_id'],
                                                 adapter=adapter, live_guard=lambda *unused: True)
                    if args.command == 'approve':
                        approval = publisher.approve(intent, actor=args.actor)
                        result = publisher.publish(intent, approval['approval_id'])
                    else:
                        result = publisher.reconcile(args.operation_id, intent)
                    result = {**result, 'external_effect': 'NONE', 'provider_origin': 'LOCAL_SIMULATION',
                              'identity_origin': 'LOCAL_OPERATOR_LABEL'}
                if args.command == 'export':
                    destination = Path(args.output)
                    with destination.open('x', encoding='utf-8') as stream:
                        json.dump(result, stream, indent=2, sort_keys=True, ensure_ascii=False)
                        stream.write('\n')
                    result = {'run_id': args.run_id, 'output': str(destination), 'export_digest': digest(result)}
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 1 if result.get('status') == 'FAIL' else 0
    except (ContractError, OSError, sqlite3.Error):
        # Do not print raw exception paths, source text or a provider response.
        print(json.dumps({'error': 'LOCAL_OPERATION_DENIED', 'command': args.command}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
