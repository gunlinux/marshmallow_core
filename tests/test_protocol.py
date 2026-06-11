"""Protocol/tag-sync contract tests between ``_compiler.py`` and ``src/lib.rs``.

The dump element tags, load element tags, and validator tags are bare integers
maintained in parallel in :mod:`marshmallow_core._compiler` and the Rust parser
(``parse_element`` / ``parse_load_element`` / ``parse_validator``).
``PROTOCOL_VERSION`` catches a *stale build*, but not a tag added or renumbered on
the Python side with no matching Rust arm committed in the same change — that
otherwise only surfaces via a missed equivalence case (ARCH.md A3).

These tests reflect the ``_*`` / ``_L_*`` / ``_V_*`` constants from the compiler
and round-trip a minimal payload for **every** tag through the extension's
constructors. A tag the Rust side doesn't handle raises ``ValueError: unknown …
tag N`` at construction, failing here. Each ``_MIN_*`` table below must therefore
stay exhaustive: a new compiler tag with no entry trips the completeness guard.
"""

import datetime as dt
import uuid

import pytest

import marshmallow_core._compiler as accel
from marshmallow import EXCLUDE, missing

# Skip the whole module when the extension isn't built (the constructors under
# test live in it); the equivalence suite already covers the pure path.
_core = accel._core
pytestmark = pytest.mark.skipif(_core is None, reason="compiled core unavailable")


# Dump tags are ``_*`` excluding the ``_L_*`` (load) and ``_V_*`` (validator) sets
# and ``_EXPECTED_PROTOCOL``; pick them out by their known names instead of prefix
# gymnastics so the intent is explicit.
_DUMP_TAGS = {
    getattr(accel, n): n
    for n in (
        "_PASSTHROUGH",
        "_STRING",
        "_INTEGER",
        "_FLOAT",
        "_NESTED",
        "_LIST",
        "_UUID",
        "_TEMPORAL",
        "_ENUM",
        "_DECIMAL",
        "_DICT",
        "_CONSTANT",
        "_TIMEDELTA",
        "_DICT_TYPED",
        "_TUPLE",
        "_PLUCK",
        "_IPADDR",
    )
}
_LOAD_TAGS = {v: k for k, v in vars(accel).items() if k.startswith("_L_")}
_VALIDATOR_TAGS = {v: k for k, v in vars(accel).items() if k.startswith("_V_")}


def _noop(*_a, **_k):
    return None


# An empty sub-serializer payload, valid for both Nested and Pluck.
_DUMP_SUBPAYLOAD = (_noop, [])
# (many, unknown_code, known_keys, specs); EXCLUDE so known_keys isn't consulted.
_LOAD_SUBPAYLOAD = (False, accel._UNKNOWN_CODES[EXCLUDE], frozenset(), [])

# Minimal valid dump element tuple per dump tag (value -> element).
_MIN_DUMP_ELEMENT = {
    accel._PASSTHROUGH: (accel._PASSTHROUGH,),
    accel._STRING: (accel._STRING,),
    accel._INTEGER: (accel._INTEGER, False),
    accel._FLOAT: (accel._FLOAT, False),
    accel._NESTED: (accel._NESTED, _DUMP_SUBPAYLOAD, False),
    accel._LIST: (accel._LIST, (accel._PASSTHROUGH,)),
    accel._UUID: (accel._UUID,),
    accel._TEMPORAL: (accel._TEMPORAL, None, "%Y"),
    accel._ENUM: (accel._ENUM, False, (accel._PASSTHROUGH,)),
    accel._DECIMAL: (accel._DECIMAL, _noop),
    accel._DICT: (accel._DICT,),
    accel._CONSTANT: (accel._CONSTANT, 42),
    accel._TIMEDELTA: (accel._TIMEDELTA, _noop),
    accel._DICT_TYPED: (accel._DICT_TYPED, None, None),
    accel._TUPLE: (accel._TUPLE, ((accel._PASSTHROUGH,),)),
    accel._PLUCK: (accel._PLUCK, _DUMP_SUBPAYLOAD, "k", False),
    accel._IPADDR: (accel._IPADDR,),
}

# Minimal valid load element tuple per load tag (value -> element).
_MIN_LOAD_ELEMENT = {
    accel._L_PASSTHROUGH: (accel._L_PASSTHROUGH,),
    accel._L_STRING: (accel._L_STRING,),
    accel._L_INTEGER: (accel._L_INTEGER,),
    accel._L_FLOAT: (accel._L_FLOAT, False),
    accel._L_NESTED: (accel._L_NESTED, _LOAD_SUBPAYLOAD),
    accel._L_LIST: (accel._L_LIST, (accel._L_PASSTHROUGH,), False),
    accel._L_ENUM: (accel._L_ENUM, object(), False, (accel._L_PASSTHROUGH,)),
    accel._L_UUID: (accel._L_UUID, uuid.UUID),
    accel._L_TEMPORAL: (accel._L_TEMPORAL, dt.datetime, _noop),
    accel._L_DECIMAL: (accel._L_DECIMAL, _noop),
    accel._L_DICT: (accel._L_DICT,),
    accel._L_CONSTANT: (accel._L_CONSTANT, 42),
    accel._L_BOOLEAN: (accel._L_BOOLEAN, {True}, {False}),
    accel._L_INTEGER_STRICT: (accel._L_INTEGER_STRICT,),
    accel._L_DICT_TYPED: (accel._L_DICT_TYPED, None, [], None, []),
    accel._L_TUPLE: (accel._L_TUPLE, ((accel._L_PASSTHROUGH,),)),
    accel._L_PLUCK: (accel._L_PLUCK, _LOAD_SUBPAYLOAD, "k", False),
    accel._L_TIMEDELTA: (accel._L_TIMEDELTA, _noop),
    accel._L_DATETIME_AWARENESS: (accel._L_DATETIME_AWARENESS, _noop),
    accel._L_IPADDR: (accel._L_IPADDR, _noop),
    accel._L_NESTED_POST_LOAD: (accel._L_NESTED_POST_LOAD, _LOAD_SUBPAYLOAD, _noop),
}

# Minimal valid validator spec per validator tag (value -> spec).
_MIN_VALIDATOR = {
    accel._V_RANGE: (accel._V_RANGE, None, None, True, True),
    accel._V_LENGTH: (accel._V_LENGTH, None, None, None),
    accel._V_ONEOF: (accel._V_ONEOF, [1, 2]),
    accel._V_EQUAL: (accel._V_EQUAL, 1),
    accel._V_NONEOF: (accel._V_NONEOF, [1]),
    accel._V_CONTAINSONLY: (accel._V_CONTAINSONLY, [1]),
    accel._V_PYTHON: (accel._V_PYTHON, _noop),
}


def _build_dump(element):
    spec = (False, "k", "k", missing, element)
    _core.DumpSerializer((_noop, [spec]), missing)


def _build_load(element, validators=()):
    spec = (False, "k", "k", "k", missing, False, False, element, list(validators))
    _core.LoadDeserializer((False, 1, frozenset({"k"}), [spec]), missing)


def test_protocol_version_matches_extension():
    assert accel._EXPECTED_PROTOCOL == _core.PROTOCOL_VERSION


@pytest.mark.parametrize("tag", sorted(_DUMP_TAGS), ids=lambda t: _DUMP_TAGS[t])
def test_every_dump_tag_is_parsed(tag):
    assert tag in _MIN_DUMP_ELEMENT, f"no minimal payload for dump tag {tag}"
    _build_dump(_MIN_DUMP_ELEMENT[tag])  # raises if Rust has no arm for ``tag``


@pytest.mark.parametrize("tag", sorted(_LOAD_TAGS), ids=lambda t: _LOAD_TAGS[t])
def test_every_load_tag_is_parsed(tag):
    assert tag in _MIN_LOAD_ELEMENT, f"no minimal payload for load tag {tag}"
    _build_load(_MIN_LOAD_ELEMENT[tag])


@pytest.mark.parametrize(
    "tag", sorted(_VALIDATOR_TAGS), ids=lambda t: _VALIDATOR_TAGS[t]
)
def test_every_validator_tag_is_parsed(tag):
    assert tag in _MIN_VALIDATOR, f"no minimal payload for validator tag {tag}"
    # Attach the validator to a passthrough native field so the Rust side parses
    # it via ``parse_validator``.
    _build_load((accel._L_PASSTHROUGH,), validators=[_MIN_VALIDATOR[tag]])


def test_tag_tables_are_exhaustive():
    """Every compiler tag constant has a minimal-payload entry (and vice versa).

    Guards against a tag added to ``_compiler.py`` without a corresponding entry
    here — which would otherwise let an un-round-tripped tag slip by untested.
    """
    assert set(_MIN_DUMP_ELEMENT) == set(_DUMP_TAGS)
    assert set(_MIN_LOAD_ELEMENT) == set(_LOAD_TAGS)
    assert set(_MIN_VALIDATOR) == set(_VALIDATOR_TAGS)


def test_an_unknown_tag_is_rejected():
    """Sanity: a tag with no Rust arm raises, so the round-trips above are real."""
    with pytest.raises(ValueError, match="unknown"):
        _build_dump((99,))
    with pytest.raises(ValueError, match="unknown"):
        _build_load((99,))
