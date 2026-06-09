"""Coverage probe: which fields of a schema compile *native* vs *callback*.

The core accelerates a field only when the compiler can model its
``_serialize`` / ``_deserialize`` natively; everything else defers to a Python
callback. This probe inspects a bound schema and reports, per field and per
direction (dump/load), whether it compiled native, and flags whole-schema
conditions that force the *entire* schema onto the pure-Python path even when
its individual fields are native (e.g. a custom ``dict_class`` or load hooks).

Run it directly to see the four benchmark schemas::

    python -m performance.analyze_paths
"""

from __future__ import annotations

import typing

from marshmallow import Schema

from marshmallow_core import _compiler

# Reverse tag -> name maps for human-readable output.
_DUMP_KIND = {
    _compiler._PASSTHROUGH: "passthrough",
    _compiler._STRING: "string",
    _compiler._INTEGER: "integer",
    _compiler._FLOAT: "float",
    _compiler._NESTED: "nested",
    _compiler._LIST: "list",
    _compiler._UUID: "uuid",
    _compiler._TEMPORAL: "temporal",
    _compiler._ENUM: "enum",
    _compiler._DECIMAL: "decimal",
    _compiler._DICT: "dict",
    _compiler._CONSTANT: "constant",
}
_LOAD_KIND = {
    _compiler._L_PASSTHROUGH: "passthrough",
    _compiler._L_STRING: "string",
    _compiler._L_INTEGER: "integer",
    _compiler._L_FLOAT: "float",
    _compiler._L_NESTED: "nested",
    _compiler._L_LIST: "list",
    _compiler._L_ENUM: "enum",
    _compiler._L_UUID: "uuid",
    _compiler._L_TEMPORAL: "temporal",
    _compiler._L_DECIMAL: "decimal",
    _compiler._L_DICT: "dict",
    _compiler._L_CONSTANT: "constant",
    _compiler._L_BOOLEAN: "boolean",
}


class FieldReport(typing.NamedTuple):
    name: str
    native: bool
    kind: str  # element-kind name, or "callback"


class SchemaReport(typing.NamedTuple):
    whole_dump_native: bool  # the whole-schema dump payload compiled
    whole_load_native: bool  # the whole-schema load payload compiled
    dump_fields: list[FieldReport]
    load_fields: list[FieldReport]


def _dump_field_report(schema: Schema) -> list[FieldReport]:
    out = []
    for attr_name, field in schema.dump_fields.items():
        element = _compiler._build_element(field, ())
        if element is None:
            out.append(FieldReport(attr_name, False, "callback"))
        else:
            out.append(FieldReport(attr_name, True, _DUMP_KIND.get(element[0], "?")))
    return out


def _load_field_report(schema: Schema) -> list[FieldReport]:
    out = []
    for attr_name, field in schema.load_fields.items():
        # Mirror the per-field guards in ``_build_load_payload``: pre/post-load
        # always defers; validators defer only if any is unrecognized.
        load_default = field.load_default
        callable_default = load_default is not _compiler.missing and callable(
            load_default
        )
        element = None
        if (
            not _compiler._has_load_pre_post(field)
            and not callable_default
            and _compiler._compile_validators(field.validators) is not None
        ):
            element = _compiler._build_load_element(field, ())
        if element is None:
            out.append(FieldReport(attr_name, False, "callback"))
        else:
            out.append(FieldReport(attr_name, True, _LOAD_KIND.get(element[0], "?")))
    return out


def analyze(schema: Schema) -> SchemaReport:
    """Return a per-field native/callback report for ``schema``."""
    dump_payload = _compiler._build_payload(schema)
    load_payload = _compiler._build_load_payload(
        schema, many=bool(schema.many), is_root=True
    )
    return SchemaReport(
        whole_dump_native=dump_payload is not None,
        whole_load_native=load_payload is not None,
        dump_fields=_dump_field_report(schema),
        load_fields=_load_field_report(schema),
    )


def _format(name: str, report: SchemaReport) -> str:
    lines = [f"=== {name} ==="]
    for direction, whole, fields in (
        ("dump", report.whole_dump_native, report.dump_fields),
        ("load", report.whole_load_native, report.load_fields),
    ):
        native = sum(1 for f in fields if f.native)
        whole_note = "" if whole else "  (whole-schema FALLBACK)"
        lines.append(f"  {direction}: {native}/{len(fields)} native{whole_note}")
        for f in fields:
            mark = f.kind if f.native else "callback"
            lines.append(f"    {'+' if f.native else '-'} {f.name:<14} {mark}")
    return "\n".join(lines)


def main() -> None:
    from performance.schemas import CASES

    for name, (schema_cls, _sample) in CASES.items():
        print(_format(name, analyze(schema_cls())))
        print()


if __name__ == "__main__":
    main()
