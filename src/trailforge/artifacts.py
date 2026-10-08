"""Content-addressed local artifacts with host-owned scope and bounded reads.

Scope checks assume an authenticated host supplies this store's identity. This
does not authenticate a customer or defend against a privileged filesystem writer.
"""
from pathlib import Path
import stat

from .contracts import hash_value, integer
from .core import ContractError, byte_digest, digest, fields, nonempty


def no_links(path):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink() or (hasattr(component, 'is_junction') and component.is_junction()):
            raise ContractError('artifact links/junctions denied')
    return path.resolve()


class LocalArtifactStore:
    def __init__(self, directory, *, tenant_id, resource_id, max_bytes=1048576):
        self.scope = (nonempty(tenant_id), nonempty(resource_id))
        integer(max_bytes, 1, 16777216)
        self.max_bytes = max_bytes
        self.root = no_links(directory)
        self.scope_digest = digest(list(self.scope))
        self.directory = self.root / self.scope_digest.removeprefix('sha256:')
        no_links(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _scope(self, scope):
        if type(scope) is not tuple or scope != self.scope:
            raise ContractError('artifact scope denied')

    def _read(self, path):
        no_links(path)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ContractError('artifact is not a single regular file')
        with path.open('rb') as stream:
            content = stream.read(self.max_bytes + 1)
        if len(content) > self.max_bytes:
            raise ContractError('artifact exceeds read bound')
        return content

    def put(self, scope, content, media_type):
        self._scope(scope)
        if type(content) is not bytes or len(content) > self.max_bytes:
            raise ContractError('artifact exceeds write bound')
        if len(nonempty(media_type)) > 255:
            raise ContractError('artifact media type exceeds bound')
        content_digest = byte_digest(content)
        path = self.directory / content_digest.removeprefix('sha256:')
        no_links(path)
        try:
            with path.open('xb') as stream:
                stream.write(content)
            path.chmod(0o600)
        except FileExistsError:
            if self._read(path) != content:
                raise ContractError('existing artifact content changed') from None
        receipt = {'issuer': 'trailforge.local_artifacts/0.1', 'scope_digest': self.scope_digest,
                   'content_digest': content_digest, 'size': len(content), 'media_type': media_type}
        return {**receipt, 'receipt_id': digest(receipt)}

    def get(self, scope, receipt):
        self._scope(scope)
        fields(receipt, {'issuer', 'scope_digest', 'content_digest', 'size', 'media_type', 'receipt_id'})
        if receipt['issuer'] != 'trailforge.local_artifacts/0.1' or receipt['scope_digest'] != self.scope_digest:
            raise ContractError('artifact receipt issuer/scope denied')
        body = {key: value for key, value in receipt.items() if key != 'receipt_id'}
        if digest(body) != receipt['receipt_id']:
            raise ContractError('artifact receipt changed')
        content_digest = hash_value(receipt['content_digest'])
        content = self._read(self.directory / content_digest.removeprefix('sha256:'))
        if byte_digest(content) != content_digest or len(content) != receipt['size']:
            raise ContractError('artifact content changed')
        return content
