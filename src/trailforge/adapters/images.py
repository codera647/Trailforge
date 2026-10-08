"""Read-only official Python image resolution; never pulls images or starts Docker."""
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..contracts import hash_value
from ..core import ContractError, strict_json


def select_image(metadata, *, tag, architecture):
    if architecture not in ('amd64', 'arm64') or type(tag) is not str or not re.fullmatch(r'3\.13(?:\.\d+)?-slim-bookworm', tag):
        raise ContractError('unsupported conformance image selection')
    if type(metadata) is not dict or type(metadata.get('images')) is not list or len(metadata['images']) > 64:
        raise ContractError('invalid image metadata')
    images = [item for item in metadata['images'] if type(item) is dict
              and item.get('os') == 'linux' and item.get('architecture') == architecture
              and item.get('status') == 'active']
    if len(images) != 1:
        raise ContractError('unique active Linux platform manifest required')
    checksum = hash_value(images[0].get('digest'))
    return {'schema_version': 'trailforge/image-selection/0.1',
            'image': 'docker.io/library/python@' + checksum, 'tag_at_resolution': tag,
            'architecture': architecture, 'os': 'linux',
            'source': 'https://hub.docker.com/v2/namespaces/library/repositories/python/tags/' + tag,
            'image_pulled': False, 'runtime_verified': False}


def resolve_image(*, tag='3.13-slim-bookworm', architecture='amd64'):
    # Validate before constructing the fixed official, anonymous metadata endpoint.
    if architecture not in ('amd64', 'arm64') or type(tag) is not str or not re.fullmatch(r'3\.13(?:\.\d+)?-slim-bookworm', tag):
        raise ContractError('unsupported conformance image selection')
    url = 'https://hub.docker.com/v2/namespaces/library/repositories/python/tags/' + tag
    try:
        with urlopen(Request(url, headers={'Accept': 'application/json', 'User-Agent': 'trailforge-conformance/0.1'}), timeout=10) as response:
            if response.geturl() != url or response.status != 200:
                raise ContractError('unexpected image metadata endpoint/status')
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ContractError('image metadata exceeds bound')
        metadata = strict_json(raw.decode('utf-8'))
    except (HTTPError, URLError, OSError, UnicodeError):
        raise ContractError('official image metadata unavailable') from None
    return select_image(metadata, tag=tag, architecture=architecture)
