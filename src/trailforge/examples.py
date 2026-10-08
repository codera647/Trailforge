"""Original bundled offline examples. No network, provider key or code execution."""
from importlib.resources import files
from pathlib import Path

from .core import ContractError


def initialize(directory):
    destination = Path(directory)
    # Exclusivity prevents replacing files in an existing adopter project.
    destination.mkdir(parents=True, exist_ok=False)
    source = files('trailforge').joinpath('data/examples')

    def copy_tree(origin, target):
        for item in origin.iterdir():
            if item.name == '__pycache__' or item.name.endswith(('.pyc', '.pyo')):
                continue
            path = target / item.name
            if item.is_dir():
                path.mkdir()
                copy_tree(item, path)
            else:
                with path.open('xb') as stream:
                    stream.write(item.read_bytes())

    try:
        copy_tree(source, destination)
    except OSError:
        raise ContractError('example initialization incomplete; choose a new directory') from None
    return {'directory': str(destination.resolve()), 'provider_origin': 'LOCAL_MOCK',
            'next': 'trailforge run --fixture ' + str(destination / 'buggy-python')}
