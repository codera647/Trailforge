"""Resolve an immutable official image reference without installing/pulling it."""
import argparse
import json
from pathlib import Path

from trailforge.adapters.images import resolve_image
from trailforge.core import ContractError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', default='3.13-slim-bookworm')
    parser.add_argument('--architecture', choices=('amd64', 'arm64'), default='amd64')
    parser.add_argument('--output')
    args = parser.parse_args()
    try:
        result = resolve_image(tag=args.tag, architecture=args.architecture)
        if args.output:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
        return 0
    except (ContractError, OSError):
        print(json.dumps({'status': 'FAIL', 'error_code': 'IMAGE_SELECTION_UNAVAILABLE'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
