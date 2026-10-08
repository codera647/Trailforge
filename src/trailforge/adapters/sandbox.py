"""Explicit Linux-container check boundary. No local shell fallback."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import time
from uuid import uuid4

from ..artifacts import no_links
from ..contracts import hash_value, integer
from ..core import ContractError, digest, nonempty, source_path, strict_json


class SandboxDenied(ContractError):
    pass


def _image_identity(reference):
    """Normalize Docker Hub's familiar names, retaining exact repository/digest."""
    if (type(reference) is not str or len(reference) > 4096
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[a-f0-9]{64}', reference)):
        return None
    repository, checksum = reference.rsplit('@', 1)
    parts = repository.split('/')
    if any(not part or part in ('.', '..') for part in parts):
        return None
    if len(parts) == 1 or not ('.' in parts[0] or ':' in parts[0] or parts[0] == 'localhost'):
        parts.insert(0, 'docker.io')
    if parts[0] == 'index.docker.io':
        parts[0] = 'docker.io'
    if parts[0] == 'docker.io' and len(parts) == 2:
        parts.insert(1, 'library')
    # A requested tag does not change the immutable digest identity.
    parts[-1] = parts[-1].split(':', 1)[0]
    if not parts[-1]:
        return None
    return '/'.join(parts) + '@' + checksum


def tree_digest(directory, *, max_files=512, max_bytes=16777216):
    root = no_links(directory)
    if not root.is_dir():
        raise SandboxDenied('INVALID_WORKSPACE')
    manifest, total = {}, 0
    pending = [root]
    paths = []
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                no_links(path)
                paths.append(path)
                if len(paths) > max_files:
                    raise SandboxDenied('WORKSPACE_LIMIT')
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
    for path in sorted(paths):
        no_links(path)
        if path.is_dir():
            continue
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SandboxDenied('INVALID_WORKSPACE_FILE')
        if len(manifest) >= max_files:
            raise SandboxDenied('WORKSPACE_LIMIT')
        relative = source_path(path.relative_to(root).as_posix())
        checksum = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(65536):
                total += len(chunk)
                if total > max_bytes:
                    raise SandboxDenied('WORKSPACE_LIMIT')
                checksum.update(chunk)
        manifest[relative] = 'sha256:' + checksum.hexdigest()
    return digest(manifest)


@dataclass(frozen=True)
class SandboxLimits:
    seconds: int = 30
    memory_mb: int = 256
    pids: int = 64
    output_bytes: int = 1048576

    def validate(self):
        integer(self.seconds, 1, 300)
        integer(self.memory_mb, 64, 4096)
        integer(self.pids, 16, 512)
        integer(self.output_bytes, 1024, 4194304)


class DockerSandbox:
    def __init__(self, image, *, executable=None, limits=SandboxLimits()):
        if _image_identity(image) is None:
            raise SandboxDenied('PINNED_IMAGE_REQUIRED')
        limits.validate()
        self.image, self.limits = image, limits
        self.executable = executable or shutil.which('docker')

    def command(self, candidate, checks, argv, *, name):
        if type(argv) is not tuple or not 1 <= len(argv) <= 64 or any(type(arg) is not str or not arg or '\x00' in arg for arg in argv):
            raise SandboxDenied('INVALID_CHECK_COMMAND')
        if sum(len(arg.encode('utf-8')) for arg in argv) > 16384:
            raise SandboxDenied('INVALID_CHECK_COMMAND')
        if not re.fullmatch(r'trailforge-[a-f0-9]{32}', name):
            raise SandboxDenied('INVALID_CONTAINER_NAME')
        candidate, checks = no_links(candidate), no_links(checks)
        if (candidate.is_relative_to(checks) or checks.is_relative_to(candidate)
                or any(',' in str(path) or '\n' in str(path) for path in (candidate, checks))):
            raise SandboxDenied('CHECK_BOUNDARY_DENIED')
        return [self.executable or 'docker', 'run', '--name', name, '--label', 'org.trailforge.owner=' + name,
                '--pull', 'never', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--user', '65534:65534',
                '--pids-limit', str(self.limits.pids), '--memory', str(self.limits.memory_mb) + 'm',
                '--memory-swap', str(self.limits.memory_mb) + 'm', '--cpus', '1',
                '--ulimit', 'nofile=1024:1024', '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
                '--mount', 'type=bind,source=' + str(candidate) + ',target=/candidate,readonly',
                '--mount', 'type=bind,source=' + str(checks) + ',target=/checks,readonly',
                '--workdir', '/candidate', '--env', 'PYTHONDONTWRITEBYTECODE=1',
                '--entrypoint', argv[0], self.image, *argv[1:]]

    def _control(self, *args, timeout=10):
        # Host-selected Docker configuration is trusted; nothing is passed into
        # the container except the fixed environment flag in command().
        if self.executable is None:
            raise SandboxDenied('SANDBOX_UNAVAILABLE')
        try:
            return subprocess.run([self.executable, *args], capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise SandboxDenied('SANDBOX_UNAVAILABLE') from None

    def preflight(self):
        endpoint = os.environ.get('DOCKER_HOST')
        if endpoint and not endpoint.startswith(('unix://', 'npipe://')):
            raise SandboxDenied('LOCAL_ENGINE_REQUIRED')
        result = self._control('context', 'inspect', '--format', '{{.Endpoints.docker.Host}}')
        if result.returncode or not result.stdout.strip().startswith((b'unix://', b'npipe://')):
            raise SandboxDenied('LOCAL_ENGINE_REQUIRED')
        result = self._control('info', '--format', '{{.OSType}}')
        if result.returncode or result.stdout.strip() != b'linux':
            raise SandboxDenied('LINUX_ENGINE_REQUIRED')
        result = self._control('image', 'inspect', self.image, '--format', '{{json .RepoDigests}}')
        if result.returncode or len(result.stdout) > 65536:
            raise SandboxDenied('PINNED_IMAGE_UNAVAILABLE')
        try:
            references = strict_json(result.stdout.decode('utf-8'))
        except (ContractError, UnicodeError):
            raise SandboxDenied('PINNED_IMAGE_UNAVAILABLE') from None
        if (type(references) is not list or len(references) > 64
                or _image_identity(self.image) not in [_image_identity(item) for item in references]):
            raise SandboxDenied('PINNED_IMAGE_UNAVAILABLE')

    def _present(self, name):
        if not re.fullmatch(r'trailforge-[a-f0-9]{32}', name):
            raise SandboxDenied('INVALID_CONTAINER_NAME')
        listed = self._control('container', 'ls', '--all', '--filter', 'name=^/' + name + '$', '--format', '{{.Names}}')
        if listed.returncode:
            raise SandboxDenied('CLEANUP_INSPECTION_FAILED')
        names = listed.stdout.strip()
        if names not in (b'', name.encode('ascii')):
            raise SandboxDenied('CLEANUP_OWNER_MISMATCH')
        return bool(names)

    def confirm_absent(self, name):
        """Require a healthy engine's positive absence observation, not any CLI error."""
        if self._present(name):
            raise SandboxDenied('CLEANUP_FAILED')
        return True

    def _remove_owned(self, name):
        if not self._present(name):
            return
        inspected = self._control('inspect', '--format', '{{index .Config.Labels "org.trailforge.owner"}}', name)
        if inspected.returncode:
            raise SandboxDenied('CLEANUP_INSPECTION_FAILED')
        if inspected.stdout.strip() != name.encode('ascii'):
            raise SandboxDenied('CLEANUP_OWNER_MISMATCH')
        removed = self._control('rm', '-f', name)
        if removed.returncode:
            raise SandboxDenied('CLEANUP_FAILED')
        self.confirm_absent(name)

    @staticmethod
    def _kill_process(process):
        if process.poll() is not None:
            return
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, check=False)
        else:
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def run(self, candidate, checks, argv, *, goal_digest, candidate_digest, checks_digest):
        for value in (goal_digest, candidate_digest, checks_digest):
            hash_value(value)
        self.preflight()
        if tree_digest(candidate) != candidate_digest or tree_digest(checks) != checks_digest:
            raise SandboxDenied('WORKSPACE_CHANGED')
        name = 'trailforge-' + uuid4().hex
        args = self.command(candidate, checks, argv, name=name)
        started = time.monotonic()
        output, exceeded, lock = bytearray(), threading.Event(), threading.Lock()
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, creationflags=flags,
                                       start_new_session=os.name != 'nt')
        except OSError:
            raise SandboxDenied('SANDBOX_UNAVAILABLE') from None

        def drain():
            while chunk := process.stdout.read(4096):
                with lock:
                    room = self.limits.output_bytes - len(output)
                    output.extend(chunk[:max(room, 0)])
                    if len(chunk) > room:
                        exceeded.set()

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        status = 'EXECUTED'
        try:
            deadline = started + self.limits.seconds
            while process.poll() is None:
                if exceeded.is_set() or time.monotonic() >= deadline:
                    status = 'OUTPUT_LIMIT' if exceeded.is_set() else 'TIMEOUT'
                    self._remove_owned(name)
                    if process.poll() is None:
                        self._kill_process(process)
                    break
                time.sleep(0.05)
            process.wait(timeout=10)
            reader.join(timeout=10)
            if reader.is_alive():
                raise SandboxDenied('OUTPUT_DRAIN_FAILED')
            if exceeded.is_set():
                status = 'OUTPUT_LIMIT'
        finally:
            if process.poll() is None:
                self._kill_process(process)
                process.wait(timeout=10)
            try:
                self._remove_owned(name)
            finally:
                if process.stdout is not None:
                    process.stdout.close()
        if tree_digest(candidate) != candidate_digest or tree_digest(checks) != checks_digest:
            status = 'WORKSPACE_CHANGED'
        body = {'issuer': 'trailforge.docker_runner/0.1', 'goal_digest': goal_digest,
                'candidate_digest': candidate_digest, 'checks_digest': checks_digest,
                'image': self.image, 'command_digest': digest(list(argv)), 'status': status,
                'exit_code': process.returncode, 'output_digest': 'sha256:' + hashlib.sha256(output).hexdigest(),
                'elapsed_seconds': round(time.monotonic() - started, 3), 'output_bytes': len(output),
                'network': 'NONE', 'criterion_verdict': 'UNKNOWN'}
        # Execution and exit code are observations, not semantic goal completion.
        return {**body, 'receipt_id': digest(body), 'output': bytes(output).decode('utf-8', errors='replace')}
