"""Domain contracts for the configurable agent team (T01).

Pure standard library, no I/O: these modules only describe and validate data.
"""
from .schema import (  # noqa: F401
    SCHEMA_VERSION, ContractError, export_json_schema, kinds, validate, validate_bundle,
)
from .ids import new_id, is_id  # noqa: F401
