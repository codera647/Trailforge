"""Sandbox construction and boundary tests; actual Linux execution is a separate gate."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import os
import tempfile
import unittest

from trailforge.adapters.sandbox import DockerSandbox, SandboxDenied, SandboxLimits, tree_digest
from trailforge.core import digest


class SandboxPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='trailforge-sandbox-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.candidate, self.checks = self.root / 'candidate', self.root / 'checks'
        self.candidate.mkdir()
        self.checks.mkdir()
        (self.candidate / 'input.txt').write_text('original', encoding='utf-8')
        (self.checks / 'check.py').write_text('raise SystemExit(0)\n', encoding='utf-8')
        self.image = 'python@sha256:' + 'a' * 64

    def test_exact_tree_change_and_file_bound_are_observed(self):
        before = tree_digest(self.candidate)
        (self.candidate / 'input.txt').write_text('new', encoding='utf-8')
        self.assertNotEqual(tree_digest(self.candidate), before)
        with self.assertRaises(SandboxDenied):
            tree_digest(self.candidate, max_bytes=2)

    def test_command_has_fixed_isolation_and_no_credential_mount(self):
        sandbox = DockerSandbox(self.image)
        argv = sandbox.command(self.candidate, self.checks, ('python', '-I', '/checks/check.py'),
                               name='trailforge-' + 'a' * 32)
        for option in ('--network', '--read-only', '--cap-drop', '--security-opt', '--user', '--pids-limit', '--memory', '--memory-swap', '--cpus'):
            self.assertIn(option, argv)
        self.assertEqual(argv[argv.index('--network') + 1], 'none')
        self.assertEqual(argv[argv.index('--pull') + 1], 'never')
        self.assertEqual(argv.count('--mount'), 2)
        self.assertNotIn('--privileged', argv)
        self.assertNotIn('/var/run/docker.sock', str(argv))
        self.assertTrue(all('readonly' in argv[i+1] for i, arg in enumerate(argv) if arg == '--mount'))

    def test_mutable_images_overlapping_checks_and_unsafe_argv_are_denied(self):
        for image in ('python:latest', 'python:3.13', '--privileged', 'python@sha256:invalid'):
            with self.assertRaises(SandboxDenied):
                DockerSandbox(image)
        sandbox = DockerSandbox(self.image)
        with self.assertRaises(SandboxDenied):
            sandbox.command(self.candidate, self.candidate, ('python',), name='trailforge-' + 'a' * 32)
        with self.assertRaises(SandboxDenied):
            sandbox.command(self.candidate, self.checks, ('python\x00',), name='trailforge-' + 'a' * 32)
        with self.assertRaises(SandboxDenied):
            sandbox.command(self.candidate, self.checks, ('python',), name='another-container')

    def test_unavailable_runtime_cannot_fall_back_to_host_execution(self):
        sandbox = DockerSandbox(self.image)
        sandbox.executable = None
        with self.assertRaisesRegex(SandboxDenied, 'SANDBOX_UNAVAILABLE'):
            sandbox.run(self.candidate, self.checks, ('python',), goal_digest=digest({}),
                        candidate_digest=tree_digest(self.candidate), checks_digest=tree_digest(self.checks))

    def test_preflight_accepts_exact_docker_hub_familiar_digest(self):
        import json
        sandbox = DockerSandbox('docker.io/library/' + self.image, executable='docker')
        def control(*args):
            data = b'npipe://docker_engine' if args[0] == 'context' else b'linux'
            if args[0] == 'image':
                data = json.dumps([self.image]).encode()
            return SimpleNamespace(returncode=0, stdout=data)
        sandbox._control = control
        with patch.dict(os.environ, {'DOCKER_HOST': ''}):
            sandbox.preflight()

    def test_preflight_rejects_wrong_repository_digest_and_malformed_metadata(self):
        import json
        sandbox = DockerSandbox('docker.io/library/' + self.image, executable='docker')
        for raw in (json.dumps(['other@sha256:' + 'a' * 64]).encode(),
                    json.dumps(['registry.example/python@sha256:' + 'a' * 64]).encode(),
                    json.dumps(['python@sha256:' + 'b' * 64]).encode(),
                    json.dumps(['prefix-' + sandbox.image]).encode(), b'null', b'{}', b'bad json', b'\xff',
                    json.dumps([self.image] * 65).encode()):
            def control(*args):
                data = b'npipe://docker_engine' if args[0] == 'context' else b'linux'
                return SimpleNamespace(returncode=0, stdout=raw if args[0] == 'image' else data)
            sandbox._control = control
            with patch.dict(os.environ, {'DOCKER_HOST': ''}), self.assertRaisesRegex(SandboxDenied, 'PINNED_IMAGE_UNAVAILABLE'):
                sandbox.preflight()

    def test_empty_directory_entries_and_hardlinks_cannot_bypass_tree_limits(self):
        for index in range(3):
            (self.candidate / str(index)).mkdir()
        with self.assertRaises(SandboxDenied):
            tree_digest(self.candidate, max_files=2)
        os.link(self.candidate / 'input.txt', self.candidate / 'alias.txt')
        with self.assertRaises(SandboxDenied):
            tree_digest(self.candidate)

    def test_remote_engine_and_wrong_cleanup_owner_are_denied(self):
        sandbox = DockerSandbox(self.image, executable='docker')
        sandbox._control = lambda *args: SimpleNamespace(returncode=0, stdout=b'ssh://other-machine')
        with patch.dict(os.environ, {'DOCKER_HOST': ''}), self.assertRaisesRegex(SandboxDenied, 'LOCAL_ENGINE_REQUIRED'):
            sandbox.preflight()
        with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://localhost:2375'}), self.assertRaisesRegex(SandboxDenied, 'LOCAL_ENGINE_REQUIRED'):
            sandbox.preflight()
        commands = []
        def control(*args):
            commands.append(args)
            return SimpleNamespace(returncode=0, stdout=(('trailforge-' + 'a' * 32).encode() if args[0] == 'container' else b'unrelated-owner'))
        sandbox._control = control
        with self.assertRaisesRegex(SandboxDenied, 'CLEANUP_OWNER_MISMATCH'):
            sandbox._remove_owned('trailforge-' + 'a' * 32)
        self.assertEqual(len(commands), 2)
        self.assertFalse(any(command[0] == 'rm' for command in commands))

    def test_engine_inspection_failure_is_never_successful_cleanup(self):
        sandbox = DockerSandbox(self.image, executable='docker')
        sandbox._control = lambda *args: SimpleNamespace(returncode=1, stdout=b'')
        with self.assertRaisesRegex(SandboxDenied, 'CLEANUP_INSPECTION_FAILED'):
            sandbox.confirm_absent('trailforge-' + 'a' * 32)
        with self.assertRaisesRegex(SandboxDenied, 'CLEANUP_INSPECTION_FAILED'):
            sandbox._remove_owned('trailforge-' + 'a' * 32)

    def test_cleanup_confirms_absence_and_never_removes_an_absent_container(self):
        sandbox = DockerSandbox(self.image, executable='docker')
        name = 'trailforge-' + 'a' * 32
        present, commands = [True], []
        def control(*args):
            commands.append(args)
            if args[0] == 'rm':
                present[0] = False
                return SimpleNamespace(returncode=0, stdout=b'')
            return SimpleNamespace(returncode=0, stdout=name.encode() if present[0] else b'')
        sandbox._control = control
        sandbox._remove_owned(name)
        self.assertEqual(sum(command[0] == 'rm' for command in commands), 1)
        sandbox._remove_owned(name)
        self.assertEqual(sum(command[0] == 'rm' for command in commands), 1)
        self.assertTrue(sandbox.confirm_absent(name))

    def test_dot_segments_cannot_hide_overlapping_mounts(self):
        sandbox = DockerSandbox(self.image)
        with self.assertRaisesRegex(SandboxDenied, 'CHECK_BOUNDARY_DENIED'):
            sandbox.command(self.candidate / '..' / 'checks', self.checks, ('python',), name='trailforge-' + 'a' * 32)


if __name__ == '__main__':
    unittest.main()
