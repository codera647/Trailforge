"""Bounded exact local retrieval with host permission and immutable revision binding."""
from pathlib import Path

from ..adapters.sandbox import tree_digest
from ..contracts import integer
from ..core import ContractError, byte_digest, digest, nonempty, source_path
from ..fixtures import _safe_file, _bounded_bytes


class LocalRetrieval:
    def __init__(self, directory, *, scope, revision, paths, live_guard):
        if type(scope) is not tuple or len(scope) != 2 or not callable(live_guard):
            raise ContractError('host retrieval scope and live permission required')
        self.scope = tuple(nonempty(value) for value in scope)
        self.revision = nonempty(revision)
        if type(paths) is not tuple or not 1 <= len(paths) <= 32:
            raise ContractError('explicit retrieval paths required')
        self.paths = tuple(source_path(path) for path in paths)
        if len(set(self.paths)) != len(self.paths):
            raise ContractError('duplicate retrieval paths')
        self.root = Path(directory)
        self.guard = live_guard
        self.snapshot_digest = tree_digest(self.root)

    def _permit(self, scope, revision):
        if type(scope) is not tuple or scope != self.scope or revision != self.revision:
            raise ContractError('retrieval scope/revision denied')
        try:
            allowed = self.guard(scope, revision)
        except Exception:
            raise ContractError('retrieval authority denied') from None
        if allowed is not True or tree_digest(self.root) != self.snapshot_digest:
            raise ContractError('retrieval permission or snapshot changed')

    def retrieve(self, scope, revision, query, limit=4):
        integer(limit, 1, 8)
        query = nonempty(query)
        if len(query.encode('utf-8')) > 256:
            raise ContractError('retrieval query exceeds bound')
        self._permit(scope, revision)
        results, total = [], 0
        for relative in sorted(self.paths):
            content = _bounded_bytes(_safe_file(self.root.resolve(), relative), 65536)
            total += len(content)
            if total > 262144:
                raise ContractError('retrieval scan exceeds bound')
            try:
                text = content.decode('utf-8')
            except UnicodeError:
                raise ContractError('retrieval only supports UTF-8 text') from None
            if query.casefold() in text.casefold():
                body = {'path': relative, 'revision': revision, 'scope_digest': digest(list(scope)),
                        'snapshot_digest': self.snapshot_digest, 'content_digest': byte_digest(content),
                        'text': text, 'trust': 'UNTRUSTED_SOURCE_DATA', 'retrieval': 'EXACT_LITERAL'}
                results.append({**body, 'receipt_id': digest(body)})
            if len(results) == limit:
                break
        self._permit(scope, revision)
        return results
