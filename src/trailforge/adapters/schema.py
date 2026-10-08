"""Optional JSON Schema shape checks. Semantic/issuer/store gates still apply."""

from importlib.resources import files
import json

from jsonschema import Draft202012Validator, FormatChecker

from ..core import ContractError

CATALOG = json.loads(files("trailforge").joinpath("schemas/contracts-v0.1.json").read_text(encoding="utf-8"))


def validate_record(kind: str, value) -> None:
    if kind not in CATALOG["$defs"]:
        raise ContractError("unknown record kind")
    schema = {**CATALOG, "$ref": "#/$defs/" + kind}
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value))
    if errors:
        # Record payloads may contain private source text: report the path, not values.
        path = "/".join(str(part) for part in errors[0].absolute_path)
        raise ContractError("invalid " + kind + " record at " + (path or "root"))
