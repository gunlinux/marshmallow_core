"""Optional Rust acceleration for the ``dump`` (serialization) path.

This module compiles a bound :class:`~marshmallow.Schema` into a Rust-backed
``DumpSerializer`` that replaces the per-object Python ``_serialize`` loop. It is
strictly optional: if the ``_marshmallow_core`` extension is not installed (or is
disabled via ``MARSHMALLOW_NO_ACCEL``), :func:`build_dump_serializer` returns
``None`` and marshmallow uses its pure-Python path unchanged.

A schema is compiled into a recursive *payload* (see the wire format below).
Every field becomes either a *native* serializer (formatted entirely in Rust) or
a *callback* that defers to the Python ``Field.serialize`` method, so accelerated
output is identical to pure-Python output. Natively supported:

* scalars (exact type): ``Raw``, ``Boolean``, ``String``, ``Integer``, ``Float``;
* ``Nested`` (exact type) whose inner schema is itself compilable and has no
  ``pre_dump``/``post_dump`` hooks — the nested schema is recursed in Rust;
* ``List`` (exact type) whose element field is natively supported.

Everything else (``Method``, ``Function``, ``DateTime``, ``UUID``, ``Email``,
``Enum``, callable ``dump_default``, a schema overriding ``get_attribute``, ...)
falls back to the callback path.

Wire format passed to ``DumpSerializer(payload, missing)``:

* ``payload`` = ``(accessor, [field_spec, ...])`` — one schema level.
* ``field_spec`` native   = ``(False, output_key, key, dump_default, element)``.
* ``field_spec`` callback = ``(True, output_key, attr_name, field)``.
* ``element`` scalar = ``(kind:int, as_string:bool)`` with ``kind`` in 0..=3.
* ``element`` nested = ``(4, payload, many:bool)``.
* ``element`` list   = ``(5, element)``.
"""

from __future__ import annotations

import datetime as dt
import os
import typing
import uuid

from marshmallow import fields as ma_fields
from marshmallow import validate as ma_validate

# ``EXCLUDE``/``INCLUDE``/``RAISE``/``missing`` live in ``marshmallow.constants``
# on marshmallow 4.x but only at the top level on 3.x; the top-level names work
# on both, so import them there to support marshmallow >= 3.23.
from marshmallow import EXCLUDE, INCLUDE, RAISE, missing
from marshmallow.decorators import (
    POST_DUMP,
    POST_LOAD,
    PRE_DUMP,
    PRE_LOAD,
    VALIDATES,
    VALIDATES_SCHEMA,
)

if typing.TYPE_CHECKING:
    from marshmallow.schema import Schema

try:
    from marshmallow_core import _core
except ImportError:  # pragma: no cover - extension is optional
    _core = None  # type: ignore[assignment]

#: Wire-format/ABI version this module speaks. Must match the extension's
#: ``PROTOCOL_VERSION``; a mismatch (a stale compiled ``_marshmallow_core``
#: paired with newer ``marshmallow``, or vice versa) disables the core so we
#: never hand mismatched payloads to a build that would misread the tags.
_EXPECTED_PROTOCOL = 10


class _NoFallbackError(Exception):
    """Placeholder so ``except AccelFallback`` is valid when the ext is absent."""


#: Raised by the Rust load core to ask the caller to use the pure-Python path.
AccelFallback: type[BaseException] = (
    _core.AccelFallback if _core is not None else _NoFallbackError
)

# Dump element tags. Must match the tags in crates/marshmallow-core/src/lib.rs.
_PASSTHROUGH = 0
_STRING = 1
_INTEGER = 2
_FLOAT = 3
_NESTED = 4
_LIST = 5
_UUID = 6
_TEMPORAL = 7
_ENUM = 8
_DECIMAL = 9
_DICT = 10
_CONSTANT = 11
_TIMEDELTA = 12

# Load element tags (a distinct tag space from the dump tags above).
_L_PASSTHROUGH = 0
_L_STRING = 1
_L_INTEGER = 2
_L_FLOAT = 3
_L_NESTED = 4
_L_LIST = 5
_L_ENUM = 6
_L_UUID = 7
_L_TEMPORAL = 8
_L_DECIMAL = 9
_L_DICT = 10
_L_CONSTANT = 11
_L_BOOLEAN = 12
_L_INTEGER_STRICT = 13
_L_DICT_TYPED = 14
_L_TUPLE = 15
_L_PLUCK = 16
_L_TIMEDELTA = 17

# Native load validator tags (a distinct tag space; see ``_build_validator``).
_V_RANGE = 0
_V_LENGTH = 1
_V_ONEOF = 2

# ``OneOf`` is only compiled when its ``choices`` is one of these stable, re-
# iterable containers; a consumable iterator would behave differently on the
# second ``load``, so it stays on the (always-correct) callback path.
_ONEOF_CONTAINERS = (list, tuple, set, frozenset, dict, range, str)

# Unknown-key handling codes passed to the Rust load core.
_UNKNOWN_CODES = {RAISE: 0, EXCLUDE: 1, INCLUDE: 2}

# Schema-level hooks that make a schema ineligible for the native load path.
_LOAD_HOOKS = (PRE_LOAD, POST_LOAD, VALIDATES, VALIDATES_SCHEMA)

# Exact scalar field type -> element kind. Subclasses deliberately fall back to
# the callback path so we never second-guess an overridden ``_serialize``.
# ``Email``/``Url`` are included because their ``_serialize`` is exactly
# ``String._serialize`` (validation only happens on load).
_SCALAR_KINDS: dict[type, int] = {
    ma_fields.Raw: _PASSTHROUGH,
    ma_fields.Boolean: _PASSTHROUGH,
    ma_fields.String: _STRING,
    ma_fields.Email: _STRING,
    ma_fields.Url: _STRING,
    ma_fields.Integer: _INTEGER,
    ma_fields.Float: _FLOAT,
}

# Temporal field types whose ``_serialize`` is exactly ``_TemporalField._serialize``.
_TEMPORAL_TYPES: frozenset[type] = frozenset(
    {
        ma_fields.DateTime,
        ma_fields.NaiveDateTime,
        ma_fields.AwareDateTime,
        ma_fields.Date,
        ma_fields.Time,
    }
)

# Temporal types whose ``_deserialize`` is exactly ``_TemporalField._deserialize``.
# ``NaiveDateTime``/``AwareDateTime`` are excluded: they override ``_deserialize``
# with extra timezone handling, so they stay on the callback path.
_LOAD_TEMPORAL_TYPES: frozenset[type] = frozenset(
    {
        ma_fields.DateTime,
        ma_fields.Date,
        ma_fields.Time,
    }
)


def is_available() -> bool:
    """Return whether the Rust acceleration extension is importable and enabled.

    A protocol-version mismatch between this module and the compiled extension
    disables acceleration (the pure-Python path is always correct).

    Note: this is consulted by ``build_*`` the first time a schema is dumped/
    loaded and the result is cached on the schema, so toggling
    ``MARSHMALLOW_NO_ACCEL`` after that first use has no effect for that schema.
    """
    return (
        _core is not None
        and getattr(_core, "PROTOCOL_VERSION", None) == _EXPECTED_PROTOCOL
        and not os.environ.get("MARSHMALLOW_NO_ACCEL")
    )


def _overrides_native_dump(schema: typing.Any) -> bool:
    """True if ``schema`` overrides ``dump``/``_serialize`` directly.

    The native nested *dump* path recurses in Rust, reproducing only marshmallow's
    stock ``_serialize``; a schema that customizes its dump entry points outside
    the ``pre_dump``/``post_dump`` hook system (overriding ``dump`` or
    ``_serialize``) would be bypassed by it. Stock subclasses inherit ``Schema``'s
    (core-patched) ``_serialize``, so the identity checks pass for them and only
    genuine overrides are caught. The dump core has no ``AccelFallback``, so when
    in doubt this defers to the callback path.
    """
    from marshmallow.schema import Schema as _Schema

    cls = type(schema)
    return cls.dump is not _Schema.dump or cls._serialize is not _Schema._serialize


def _build_element(field: typing.Any, stack: tuple[type, ...]) -> tuple | None:
    """Build the value->output ``element`` for ``field``, or ``None`` to fall back.

    The element mirrors ``field._serialize`` (the transform applied to a value),
    independent of attribute access. ``stack`` carries the schema classes already
    being compiled, so self-referential schemas fall back instead of recursing
    forever at compile time.
    """
    ftype = type(field)
    kind = _SCALAR_KINDS.get(ftype)
    if kind is not None:
        return (kind, bool(getattr(field, "as_string", False)))
    if ftype is ma_fields.Nested:
        # ``field.schema`` resolves the inner schema (applying only/exclude/many).
        inner_schema = field.schema
        if (
            inner_schema._hooks[PRE_DUMP]
            or inner_schema._hooks[POST_DUMP]
            or _overrides_native_dump(inner_schema)
        ):
            return None  # schema.dump != _serialize; must use the callback path
        payload = _build_payload(inner_schema, stack)
        if payload is None:
            return None
        many = bool(inner_schema.many or field.many)
        return (_NESTED, payload, many)
    if ftype is ma_fields.List:
        inner_element = _build_element(field.inner, stack)
        if inner_element is None:
            return None
        return (_LIST, inner_element)
    if ftype is ma_fields.UUID:
        return (_UUID,)
    if ftype in _TEMPORAL_TYPES:
        # ``field.format`` is resolved to a concrete string once the field is
        # bound to its schema (which has happened by the first dump).
        data_format = field.format or field.DEFAULT_FORMAT
        if not isinstance(data_format, str):
            return None
        func = field.SERIALIZATION_FUNCS.get(data_format)
        return (_TEMPORAL, func, data_format)
    if ftype is ma_fields.Enum:
        # Enum serializes value.value / value.name through an inner field.
        inner_element = _build_element(field.field, stack)
        if inner_element is None:
            return None
        return (_ENUM, bool(field.by_value), inner_element)
    if ftype is ma_fields.Decimal:
        # Decimal formatting (quantize/places/rounding) is intrinsically Python;
        # hand the field's own ``_serialize`` to the core (provably identical).
        return (_DECIMAL, field._serialize)
    if ftype is ma_fields.Dict:
        # Only the plain dict-copy case (``dict(value)``) is native; per-key/value
        # field serialization falls back to the callback path.
        if (
            field.key_field is None
            and field.value_field is None
            and field.mapping_type is dict
        ):
            return (_DICT,)
        return None
    if ftype is ma_fields.Constant:
        return (_CONSTANT, field.constant)
    if ftype is ma_fields.TimeDelta:
        # ``TimeDelta._serialize`` (timedelta -> float division) is intrinsically
        # Python and precision-sensitive; hand the field's own method to the core
        # (provably identical, like ``Decimal``).
        return (_TIMEDELTA, field._serialize)
    return None


def _build_payload(schema: Schema, stack: tuple[type, ...] = ()) -> tuple | None:
    """Build the ``(accessor, specs)`` payload for one schema level, or ``None``."""
    if schema.dict_class is not dict:
        return None  # the Rust side always builds plain dicts
    cls = type(schema)
    if cls in stack:
        return None  # self-referential schema: break the compile-time recursion
    stack = (*stack, cls)
    from marshmallow.schema import Schema as _Schema

    # A custom ``get_attribute`` is incompatible with native attribute access, so
    # force every field through the callback path (which honours the accessor).
    force_callback = type(schema).get_attribute is not _Schema.get_attribute

    specs = []
    for attr_name, field in schema.dump_fields.items():
        output_key = field.data_key if field.data_key is not None else attr_name
        dump_default = field.dump_default
        element = None
        if not force_callback and not (
            dump_default is not missing and callable(dump_default)
        ):
            element = _build_element(field, stack)
        if element is not None:
            check_key = field.attribute if field.attribute is not None else attr_name
            specs.append((False, output_key, check_key, dump_default, element))
        else:
            # Rust calls ``field.serialize(attr_name, obj, accessor)``.
            specs.append((True, output_key, attr_name, field))

    return (schema.get_attribute, specs)


def build_dump_serializer(schema: Schema) -> typing.Any | None:
    """Compile ``schema`` into a Rust ``DumpSerializer``, or ``None`` to fall back.

    Returns ``None`` (use the pure-Python path) when the extension is unavailable
    or the schema uses a feature the fast path does not model.

    Unlike the load path, the dump core has **no ``AccelFallback``** — once a
    field is compiled native it cannot defer to Python mid-serialization. Only
    add a field type here when its native ``Element`` is provably identical to
    ``Field._serialize`` for *every* input, and cover it with a ``_dump_both``
    equivalence test.
    """
    if not is_available():
        return None
    payload = _build_payload(schema)
    if payload is None:
        return None
    assert _core is not None  # narrowed by is_available()
    return _core.DumpSerializer(payload, missing)


def build_dump_json_serializer(schema: Schema) -> typing.Any | None:
    """Compile ``schema`` into a serializer whose ``run_json`` fuses ``dump`` +
    ``json.dumps``, or ``None`` to fall back.

    Uses the same payload as :func:`build_dump_serializer`; the JSON writer
    raises ``AccelFallback`` for any value it cannot reproduce byte-for-byte, so
    no extra compile-time gating is needed beyond the dump payload itself. The
    caller (:mod:`marshmallow_core._patch`) only invokes this for hook-free
    schemas dumped through the stdlib ``json`` render module.
    """
    if not is_available():
        return None
    payload = _build_payload(schema)
    if payload is None:
        return None
    assert _core is not None  # narrowed by is_available()
    return _core.DumpSerializer(payload, missing)


# ---- Load (deserialization) acceleration -----------------------------------
#
# Mirrors the dump builders but for the ``load`` path. A native field encodes
# enough of ``Field.deserialize`` (required/allow_none/load_default + the value
# transform) that the Rust core can reproduce the *valid-input* result exactly;
# anything else is a callback that defers to ``field.deserialize``. The Rust core
# raises :data:`AccelFallback` the moment it sees an error/edge case, so all
# error reporting stays on the unchanged pure-Python path.


def _field_pre_post(field: typing.Any) -> typing.Any:
    """The field's own ``pre_load``/``post_load`` processors, or a falsy value.

    Field-level ``pre_load``/``post_load`` are a marshmallow 4.x feature; 3.x
    fields have no such attributes, so probe with ``getattr`` to support both.
    """
    return getattr(field, "pre_load", None) or getattr(field, "post_load", None)


def _has_field_processors(field: typing.Any) -> bool:
    """True if a field has validators or its own pre/post-load processors.

    Used for *inner* fields (e.g. a ``List``'s element) whose ``deserialize`` the
    core would have to call to honour validators/hooks; those still force the
    enclosing field onto the callback path. The *top-level* field path compiles
    recognized validators natively (see :func:`_compile_validators`) and only
    defers for ``pre_load``/``post_load``.
    """
    return bool(field.validators or _field_pre_post(field))


def _has_load_pre_post(field: typing.Any) -> bool:
    """True if a field has its own ``pre_load``/``post_load`` processors.

    Unlike validators (which run *after* ``_deserialize`` and only decide
    pass/fail, so the core can model them natively and fall back on failure),
    field-level ``pre_load``/``post_load`` transform the value and must run in
    Python — such a field always stays a callback.
    """
    return bool(_field_pre_post(field))


def _build_validator(validator: typing.Any) -> tuple | None:
    """Compile a single ``validate`` validator into a spec tuple, or ``None``.

    Only ``Range``/``Length``/``OneOf`` (exact types) are modelled. The native
    core reproduces only each validator's *pass/fail decision*; on failure it
    raises :data:`AccelFallback` so Python re-runs the validator and emits the
    exact (possibly custom) error message. Custom messages therefore need no
    modelling here.
    """
    vtype = type(validator)
    if vtype is ma_validate.Range:
        return (
            _V_RANGE,
            validator.min,
            validator.max,
            bool(validator.min_inclusive),
            bool(validator.max_inclusive),
        )
    if vtype is ma_validate.Length:
        return (_V_LENGTH, validator.min, validator.max, validator.equal)
    if vtype is ma_validate.OneOf:
        if not isinstance(validator.choices, _ONEOF_CONTAINERS):
            return None  # consumable/odd choices -> defer to Python
        return (_V_ONEOF, validator.choices)
    return None


def _compile_validators(validators: typing.Any) -> list[tuple] | None:
    """Compile a field's validator list, or ``None`` if any is unrecognized.

    Returns an empty list for a field with no validators. ``None`` means the
    field must use the callback path (Python runs the unrecognized validator).
    """
    specs = []
    for validator in validators:
        spec = _build_validator(validator)
        if spec is None:
            return None
        specs.append(spec)
    return specs


def _overrides_native_load(schema: typing.Any) -> bool:
    """True if ``schema`` customizes a load entry point the native path can't see.

    The Rust nested-load path reproduces only marshmallow's *stock* per-field
    deserialize (``Schema._deserialize``). A schema that overrides
    ``load``/``_do_load``/``_deserialize`` directly — rather than via the
    ``pre_load``/``post_load``/``validates`` hook system — would be silently
    bypassed by it. ``marshmallow_dataclass``, for example, overrides ``load`` to
    instantiate a dataclass, leaving ``_hooks`` empty; compiling such a
    ``Nested`` natively would emit a plain ``dict`` instead of the instance.

    Stock subclasses inherit ``Schema``'s (core-patched) ``_do_load``, so the
    identity checks pass for them and only genuine overrides force the callback
    path (where ``Nested.deserialize`` invokes the real ``load``).
    """
    from marshmallow.schema import Schema as _Schema

    cls = type(schema)
    return (
        cls.load is not _Schema.load
        or cls._do_load is not _Schema._do_load
        or cls._deserialize is not _Schema._deserialize
    )


def _build_load_element(field: typing.Any, stack: tuple[type, ...]) -> tuple | None:
    """Build the value->value load ``element`` for ``field``, or ``None``."""
    ftype = type(field)
    if ftype is ma_fields.Raw:
        return (_L_PASSTHROUGH,)
    if ftype is ma_fields.Boolean:
        # ``Boolean._deserialize``: ``value in truthy -> True``, ``in falsy ->
        # False``, else (or a ``TypeError`` from an unhashable value) ``invalid``.
        # The empty-``truthy`` variant (``bool(value)``) is rare; defer it. Pass
        # the field's own sets so a customised ``truthy``/``falsy`` is honoured.
        if not field.truthy:
            return None
        return (_L_BOOLEAN, field.truthy, field.falsy)
    if ftype is ma_fields.String:
        return (_L_STRING,)
    if ftype is ma_fields.Integer:
        if field.strict:
            # ``strict`` accepts only ``numbers.Integral``. Model the common
            # exact-``int`` case natively; an ``Integral`` subclass (e.g. numpy)
            # or an invalid value defers so Python coerces / raises exactly.
            return (_L_INTEGER_STRICT,)
        return (_L_INTEGER,)
    if ftype is ma_fields.Float:
        return (_L_FLOAT, bool(field.allow_nan))
    if ftype is ma_fields.Pluck:
        # ``Pluck._deserialize`` wraps the scalar as ``{data_key: value}`` (per
        # item when ``many``) and loads it through the inner ``only=(field_name,)``
        # schema. Build a single-record payload and iterate ourselves for ``many``;
        # the core falls back on a non-collection ``many`` input or any error.
        inner_schema = field.schema
        unknown = field.unknown if field.unknown is not None else inner_schema.unknown
        payload = _build_load_payload(
            inner_schema, many=False, unknown=unknown, stack=stack
        )
        if payload is None:
            return None
        return (_L_PLUCK, payload, field._field_data_key, bool(field.many))
    if ftype is ma_fields.Nested:
        inner_schema = field.schema
        many = bool(inner_schema.many or field.many)
        # ``Nested._load`` loads with ``unknown=field.unknown`` when set, else the
        # inner schema's own ``unknown``.
        unknown = field.unknown if field.unknown is not None else inner_schema.unknown
        payload = _build_load_payload(
            inner_schema, many=many, unknown=unknown, stack=stack
        )
        if payload is None:
            return None
        return (_L_NESTED, payload)
    if ftype is ma_fields.List:
        inner = field.inner
        if _has_field_processors(inner):
            return None  # inner.deserialize would run validators/processors
        inner_element = _build_load_element(inner, stack)
        if inner_element is None:
            return None
        return (_L_LIST, inner_element, bool(inner.allow_none))
    if ftype is ma_fields.Enum:
        # Enum deserializes ``value`` through ``field.field`` then maps it to a
        # member via ``enum(val)``/``enum[val]``. ``by_value`` may be a Field,
        # which is truthy -> the by-value path, matching ``if self.by_value``.
        inner_element = _build_load_element(field.field, stack)
        if inner_element is None:
            return None
        return (_L_ENUM, field.enum, bool(field.by_value), inner_element)
    if ftype is ma_fields.UUID:
        # ``UUID._validated``: pass through an existing UUID, else ``uuid.UUID(value)``
        # (with the 16-byte ``bytes=`` special case). Any error -> fall back.
        return (_L_UUID, uuid.UUID)
    if ftype in _LOAD_TEMPORAL_TYPES:
        # ``_TemporalField._deserialize`` passes through an existing instance, else
        # applies ``DESERIALIZATION_FUNCS[format]``. A custom strptime format (no
        # registered func) is left to the callback path.
        data_format = field.format or field.DEFAULT_FORMAT
        func = field.DESERIALIZATION_FUNCS.get(data_format)
        if func is None:
            return None
        internal_type = getattr(dt, field.OBJ_TYPE)
        return (_L_TEMPORAL, internal_type, func)
    if ftype is ma_fields.Decimal:
        # ``Decimal._deserialize`` (``_validated``) is intrinsically Python; hand
        # it to the core, which turns any ``ValidationError`` into a fallback.
        return (_L_DECIMAL, field._deserialize)
    if ftype is ma_fields.Dict:
        if field.mapping_type is not dict:
            return None
        if field.key_field is None and field.value_field is None:
            return (_L_DICT,)  # plain dict-copy
        # Typed Dict: apply the key/value fields per entry on the happy path.
        # Require the inner fields carry no processors/validators (so their
        # ``deserialize`` is just ``_deserialize`` for a present, non-None value);
        # the core falls back on a non-dict input, a ``None`` key/value, or any
        # per-entry error so Python accumulates the exact error structure.
        key_field, value_field = field.key_field, field.value_field
        if (key_field is not None and _has_field_processors(key_field)) or (
            value_field is not None and _has_field_processors(value_field)
        ):
            return None
        key_el = (
            _build_load_element(key_field, stack) if key_field is not None else None
        )
        val_el = (
            _build_load_element(value_field, stack)
            if value_field is not None
            else None
        )
        if (key_field is not None and key_el is None) or (
            value_field is not None and val_el is None
        ):
            return None
        return (_L_DICT_TYPED, key_el, val_el)
    if ftype is ma_fields.Constant:
        return (_L_CONSTANT, field.constant)
    if ftype is ma_fields.Tuple:
        # Fixed-length tuple of fields. Compile each position to a native element
        # (no processors, so ``deserialize`` is ``_deserialize`` for a present,
        # non-None value); the core falls back on a non-sequence input, a length
        # mismatch (``zip(strict=True)``), a ``None`` element, or any per-element
        # error so Python re-runs with the exact ``invalid``/length messages.
        elements = []
        for inner in field.tuple_fields:
            if _has_field_processors(inner):
                return None
            inner_element = _build_load_element(inner, stack)
            if inner_element is None:
                return None
            elements.append(inner_element)
        return (_L_TUPLE, tuple(elements))
    if ftype is ma_fields.TimeDelta:
        # ``TimeDelta._deserialize`` (float -> timedelta, with rounding) is
        # intrinsically Python; hand the field's own method to the core, which
        # turns any ``ValidationError`` into a fallback (like ``Decimal``).
        return (_L_TIMEDELTA, field._deserialize)
    return None


def _build_load_payload(
    schema: Schema,
    *,
    many: bool,
    unknown: str | None = None,
    stack: tuple[type, ...] = (),
    is_root: bool = False,
) -> tuple | None:
    """Build ``(many, unknown_code, known_keys, specs)`` for one schema level.

    ``unknown`` overrides the schema's own option (used by ``Nested`` fields,
    which can pass their own ``unknown`` down). ``None`` means use the schema's.
    ``is_root`` marks the schema whose ``load`` is being compiled: its ``pre_load``
    /``post_load``/``validates``/``validates_schema`` hooks are run by ``_do_load``
    *around* the accelerated per-field deserialize, so they don't disqualify it.
    A *nested* schema gets no such wrapper, so its hooks still force the callback
    path (``Nested.deserialize`` -> ``inner.load`` runs them in Python).
    """
    if schema.dict_class is not dict:
        return None  # the Rust side always builds plain dicts
    if not is_root and (
        any(schema._hooks[hook] for hook in _LOAD_HOOKS)
        or _overrides_native_load(schema)
    ):
        return None  # nested load != _deserialize once hooks/an override are involved
    if not is_root and schema.partial:
        # The root's ``partial`` is threaded as a runtime flag to ``run``; a nested
        # schema's *own* ``partial`` option isn't captured by that flag (it only
        # applies when the parent isn't partial), so defer to the Python path.
        return None
    unknown_code = _UNKNOWN_CODES.get(schema.unknown if unknown is None else unknown)
    if unknown_code is None:
        return None  # an unrecognized ``unknown`` option; defer
    cls = type(schema)
    if cls in stack:
        return None  # self-referential schema: break compile-time recursion
    stack = (*stack, cls)

    specs = []
    known_keys = []
    for attr_name, field in schema.load_fields.items():
        data_key = field.data_key if field.data_key is not None else attr_name
        out_key = field.attribute if field.attribute is not None else attr_name
        known_keys.append(data_key)
        # A dotted ``out_key`` (e.g. ``attribute="a.b"``) is a nested write; the
        # core reproduces ``marshmallow.utils.set_value`` (build nested dicts).
        element = None
        validators: list[tuple] = []
        load_default = field.load_default
        callable_default = load_default is not missing and callable(load_default)
        if not _has_load_pre_post(field) and not callable_default:
            # Recognized validators compile natively; any other defers the field.
            compiled = _compile_validators(field.validators)
            if compiled is not None:
                element = _build_load_element(field, stack)
                validators = compiled
        if element is not None:
            specs.append(
                (
                    False,
                    data_key,
                    out_key,
                    attr_name,  # used for the ``attr_name in partial`` skip check
                    load_default,
                    bool(field.required),
                    bool(field.allow_none),
                    element,
                    validators,
                )
            )
        else:
            # Rust calls ``field.deserialize(value, attr_name, data)``.
            specs.append((True, data_key, attr_name, out_key, field))

    return (bool(many), unknown_code, frozenset(known_keys), specs)


def build_load_deserializer(schema: Schema) -> typing.Any | None:
    """Compile ``schema`` into a Rust ``LoadDeserializer``, or ``None`` to fall back.

    The returned object's ``run(data, many)`` reproduces ``load`` for valid input
    and raises :data:`AccelFallback` for anything off the happy path.
    """
    if not is_available():
        return None
    payload = _build_load_payload(schema, many=bool(schema.many), is_root=True)
    if payload is None:
        return None
    assert _core is not None  # narrowed by is_available()
    return _core.LoadDeserializer(payload, missing)
