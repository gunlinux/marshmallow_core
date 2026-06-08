"""Equivalence tests for the Rust dump/load core against stock marshmallow.

An autouse fixture installs the core (``marshmallow_core.install()``) for every
test. Each case asserts that, for a range of schemas, ``Schema.dump`` and
``Schema.load`` produce identical output (and identical errors) whether or not
the core is active: the ``_*_both`` helpers run once accelerated, then
``monkeypatch`` the ``build_*`` compiler entry points to ``None`` to force the
pure-Python path, and compare. When the compiled extension is unavailable the
builders return ``None`` anyway and both branches exercise pure Python (still a
valid, if redundant, equivalence check).
"""

import datetime as dt
import decimal
import enum
import uuid

import pytest

import marshmallow_core
import marshmallow_core._compiler as accel
from marshmallow import (
    EXCLUDE,
    INCLUDE,
    RAISE,
    Schema,
    ValidationError,
    fields,
    post_dump,
    post_load,
    validate,
    validates,
)


@pytest.fixture(autouse=True)
def _install_core():
    """Patch the Rust core into marshmallow.Schema for every equivalence test.

    With the core installed, the *accelerated* branch in each ``_*_both`` helper
    actually runs the core, while the helper's ``monkeypatch`` of
    ``build_*_serializer`` forces the *pure* branch back onto Python.
    """
    marshmallow_core.install()
    yield
    marshmallow_core.uninstall()


class Color(enum.Enum):
    RED = "r"
    GREEN = "g"


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _dump_both(schema_factory, obj, *, many=False, monkeypatch):
    """Return (accelerated, pure_python) dumps of ``obj``."""

    accelerated = schema_factory().dump(obj, many=many)

    # Force the pure-Python path by making the builder return None.
    monkeypatch.setattr(accel, "build_dump_serializer", lambda schema: None)
    pure = schema_factory().dump(obj, many=many)
    return accelerated, pure


class FlatSchema(Schema):
    i = fields.Integer()
    f = fields.Float()
    s = fields.String()
    b = fields.Boolean()
    r = fields.Raw()


class ConfiguredSchema(Schema):
    renamed = fields.Integer(data_key="RENAMED")
    as_str = fields.Integer(as_string=True)
    f_as_str = fields.Float(as_string=True)
    deep = fields.String(attribute="nested.value")
    defaulted = fields.Integer(dump_default=42)
    nullable = fields.String(allow_none=True)


class CallbackSchema(Schema):
    when = fields.DateTime()
    uid = fields.UUID()
    email = fields.Email()
    computed = fields.Method("compute")

    def compute(self, obj):
        return obj["i"] * 2


@pytest.mark.parametrize(
    ("factory", "obj"),
    [
        (FlatSchema, {"i": 3, "f": 1.5, "s": "hi", "b": True, "r": [1, 2]}),
        (FlatSchema, Obj(i=3, f=1.5, s="hi", b=False, r={"k": "v"})),
        (FlatSchema, {"i": None, "f": None, "s": None, "b": None, "r": None}),
        (FlatSchema, {"s": b"bytestring", "i": 7}),  # bytes -> decode; missing skipped
        (
            ConfiguredSchema,
            {
                "renamed": 1,
                "as_str": 9,
                "f_as_str": 2.5,
                "nested": {"value": "z"},
                "nullable": None,
            },
        ),
        (ConfiguredSchema, {"renamed": 1}),  # most fields missing
        (
            CallbackSchema,
            {
                "when": dt.datetime(2020, 1, 2, 3, 4, 5),
                "uid": "12345678-1234-5678-1234-567812345678",
                "email": "a@b.com",
                "i": 21,
            },
        ),
    ],
)
def test_dump_equivalence(factory, obj, monkeypatch):
    accelerated, pure = _dump_both(factory, obj, monkeypatch=monkeypatch)
    assert accelerated == pure


class TemporalEnumSchema(Schema):
    email = fields.Email()
    url = fields.Url()
    uid = fields.UUID()
    iso = fields.DateTime()
    rfc = fields.DateTime(format="rfc")
    ts = fields.DateTime(format="timestamp")
    strf = fields.DateTime(format="%Y/%m/%d")
    day = fields.Date()
    clock = fields.Time()
    role_name = fields.Enum(Color)
    role_value = fields.Enum(Color, by_value=True)
    role_field = fields.Enum(Color, by_value=fields.String())


_WHEN = dt.datetime(2020, 1, 2, 3, 4, 5)
_TEMPORAL_OBJ = {
    "email": "a@b.com",
    "url": "https://x.io/p",
    "uid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
    "iso": _WHEN,
    "rfc": _WHEN,
    "ts": _WHEN,
    "strf": _WHEN,
    "day": dt.date(2020, 1, 2),
    "clock": dt.time(3, 4, 5),
    "role_name": Color.RED,
    "role_value": Color.GREEN,
    "role_field": Color.RED,
}


@pytest.mark.parametrize(
    "obj",
    [_TEMPORAL_OBJ, dict.fromkeys(_TEMPORAL_OBJ)],
    ids=["values", "all-none"],
)
def test_temporal_enum_uuid_equivalence(obj, monkeypatch):
    accelerated, pure = _dump_both(TemporalEnumSchema, obj, monkeypatch=monkeypatch)
    assert accelerated == pure


def test_temporal_enum_is_native():

    schema = TemporalEnumSchema()
    schema.dump(_TEMPORAL_OBJ)
    assert accel.is_available() == (vars(schema).get("_mc_dump_serializer") is not None)


def test_dump_many_equivalence(monkeypatch):
    objs = [Obj(i=n, f=float(n), s=str(n), b=bool(n % 2), r=n) for n in range(5)]
    accelerated, pure = _dump_both(
        lambda: FlatSchema(many=True), objs, many=True, monkeypatch=monkeypatch
    )
    assert accelerated == pure


def test_post_dump_hook_still_runs(monkeypatch):
    class HookSchema(Schema):
        i = fields.Integer()

        @post_dump
        def add(self, data, **kwargs):
            data["doubled"] = data["i"] * 2
            return data

    accelerated, pure = _dump_both(HookSchema, {"i": 5}, monkeypatch=monkeypatch)
    assert accelerated == pure == {"i": 5, "doubled": 10}


class Coord(Schema):
    lat = fields.Float()
    lng = fields.Float()


class Address(Schema):
    city = fields.String()
    coordinates = fields.Nested(Coord)


class Person(Schema):
    name = fields.String()
    age = fields.Integer()
    address = fields.Nested(Address)
    tags = fields.List(fields.String())
    scores = fields.List(fields.Integer())


class Container(Schema):
    people = fields.List(fields.Nested(Person))
    total = fields.Integer()


def test_deep_nested_and_list_equivalence(monkeypatch):
    obj = {
        "people": [
            {
                "name": "Foo",
                "age": 30,
                "address": {"city": "X", "coordinates": {"lat": 1.0, "lng": 2.0}},
                "tags": ["a", "b"],
                "scores": [1, 2, 3],
            },
            {
                "name": "Bar",
                "age": 41,
                "address": None,  # nested None -> None
                "tags": [],
                "scores": None,  # list None -> None
            },
        ],
        "total": 2,
    }
    accelerated, pure = _dump_both(Container, obj, monkeypatch=monkeypatch)
    assert accelerated == pure


def test_nested_recursion_is_native():
    """The deep nested schema should actually compile to a native serializer."""

    schema = Container()
    schema.dump({"people": [], "total": 0})  # trigger lazy build
    assert accel.is_available() == (vars(schema).get("_mc_dump_serializer") is not None)


def test_nested_schema_with_post_dump_falls_back(monkeypatch):
    class Inner(Schema):
        x = fields.Integer()

        @post_dump
        def tag(self, data, **kwargs):
            data["tagged"] = True
            return data

    class Outer(Schema):
        inner = fields.Nested(Inner)

    obj = {"inner": {"x": 5}}
    accelerated, pure = _dump_both(Outer, obj, monkeypatch=monkeypatch)
    assert accelerated == pure == {"inner": {"x": 5, "tagged": True}}


def test_self_referential_schema_equivalence(monkeypatch):
    class Node(Schema):
        value = fields.Integer()
        children = fields.List(fields.Nested(lambda: Node()))

    obj = {"value": 1, "children": [{"value": 2, "children": []}]}
    accelerated, pure = _dump_both(Node, obj, monkeypatch=monkeypatch)
    assert accelerated == pure


def test_nested_instance_only_equivalence(monkeypatch):
    class Inner(Schema):
        a = fields.String()
        b = fields.String()

    class Outer(Schema):
        title = fields.String()
        inner = fields.Nested(Inner(), only=("a",))

    obj = {"title": "t", "inner": {"a": "x", "b": "y"}}
    accelerated, pure = _dump_both(Outer, obj, monkeypatch=monkeypatch)
    assert accelerated == pure == {"title": "t", "inner": {"a": "x"}}


# ---- Load (deserialization) accelerator ------------------------------------


def _load_both(schema_factory, data, *, many=False, monkeypatch, **kwargs):
    """Return (accelerated, pure_python) loads of ``data``."""

    accelerated = schema_factory().load(data, many=many, **kwargs)

    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    pure = schema_factory().load(data, many=many, **kwargs)
    return accelerated, pure


class LoadFlatSchema(Schema):
    i = fields.Integer()
    f = fields.Float()
    s = fields.String()
    r = fields.Raw()


@pytest.mark.parametrize(
    ("factory", "data"),
    [
        (LoadFlatSchema, {"i": 3, "f": 1.5, "s": "hi", "r": [1, 2]}),
        (LoadFlatSchema, {"i": "7", "f": "2.5", "s": "x"}),  # str-coerced numbers
        (LoadFlatSchema, {"s": b"bytestring"}),  # bytes -> decode
        (LoadFlatSchema, {}),  # all missing, nothing required
    ],
)
def test_load_equivalence(factory, data, monkeypatch):
    accelerated, pure = _load_both(factory, data, monkeypatch=monkeypatch)
    assert accelerated == pure


class LoadConfiguredSchema(Schema):
    renamed = fields.Integer(data_key="RENAMED")
    defaulted = fields.Integer(load_default=42)
    nullable = fields.String(allow_none=True)
    required = fields.String(required=True)


@pytest.mark.parametrize(
    "data",
    [
        {"RENAMED": 1, "nullable": None, "required": "ok"},
        {"RENAMED": 2, "defaulted": 9, "nullable": "set", "required": "ok"},
    ],
)
def test_load_configured_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(LoadConfiguredSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


def test_load_many_equivalence(monkeypatch):
    data = [{"i": n, "f": float(n), "s": str(n)} for n in range(5)]
    accelerated, pure = _load_both(
        lambda: LoadFlatSchema(many=True), data, many=True, monkeypatch=monkeypatch
    )
    assert accelerated == pure


class LoadPerson(Schema):
    name = fields.String()
    age = fields.Integer()
    address = fields.Nested(Address)
    tags = fields.List(fields.String())
    scores = fields.List(fields.Integer())


class LoadContainer(Schema):
    people = fields.List(fields.Nested(LoadPerson))
    total = fields.Integer()


def test_load_deep_nested_and_list_equivalence(monkeypatch):
    data = {
        "people": [
            {
                "name": "Foo",
                "age": 30,
                "address": {"city": "X", "coordinates": {"lat": 1.0, "lng": 2.0}},
                "tags": ["a", "b"],
                "scores": [1, 2, 3],
            },
            {"name": "Bar", "age": "41", "tags": [], "scores": [4]},
        ],
        "total": 2,
    }
    accelerated, pure = _load_both(LoadContainer, data, monkeypatch=monkeypatch)
    assert accelerated == pure


class LoadEnumSchema(Schema):
    by_name = fields.Enum(Color)
    by_value = fields.Enum(Color, by_value=True)
    by_field = fields.Enum(Color, by_value=fields.String())


@pytest.mark.parametrize(
    "data",
    [
        {"by_name": "RED", "by_value": "g", "by_field": "r"},
        {"by_name": "GREEN"},  # partial input, rest missing
    ],
)
def test_load_enum_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(LoadEnumSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "data",
    [
        {"by_name": "NOPE"},  # bad member name
        {"by_value": "xxx"},  # bad member value
    ],
)
def test_load_enum_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        LoadEnumSchema().load(data)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        LoadEnumSchema().load(data)
    assert acc_exc.value.messages == py_exc.value.messages


def test_load_enum_is_native():
    schema = LoadEnumSchema()
    schema.load({"by_name": "RED"})  # trigger lazy build
    assert accel.is_available() == (vars(schema).get("_mc_load_deserializer") is not None)


def test_load_nested_is_native():

    schema = LoadContainer()
    schema.load({"people": [], "total": 0})  # trigger lazy build
    assert accel.is_available() == (vars(schema).get("_mc_load_deserializer") is not None)


@pytest.mark.parametrize(
    "data",
    [
        {"i": "not-an-int"},  # coercion failure
        {"i": True},  # bool rejected as invalid integer
        {"f": float("nan")},  # nan rejected
        {"s": 123},  # not a string
        {"unknown": 1},  # unknown key under RAISE (default)
        {"i": None},  # null for a non-nullable field
    ],
)
def test_load_errors_match_python(data, monkeypatch):
    """On invalid input the accelerator must defer to identical Python errors."""
    with pytest.raises(ValidationError) as acc_exc:
        LoadFlatSchema().load(data)

    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        LoadFlatSchema().load(data)

    assert acc_exc.value.messages == py_exc.value.messages


@pytest.mark.parametrize("unknown", [RAISE, EXCLUDE, INCLUDE])
def test_load_unknown_equivalence(unknown, monkeypatch):
    class S(Schema):
        i = fields.Integer()

    data = {"i": 1, "extra": "x"}
    if unknown == RAISE:
        with pytest.raises(ValidationError):
            S(unknown=unknown).load(data)
        return
    accelerated, pure = _load_both(
        lambda: S(unknown=unknown), data, monkeypatch=monkeypatch
    )
    assert accelerated == pure


def test_load_validator_field_falls_back(monkeypatch):
    """A field with a validator must defer to Python (which runs the validator)."""

    class S(Schema):
        age = fields.Integer(validate=validate.Range(min=0))

    accelerated, pure = _load_both(S, {"age": 5}, monkeypatch=monkeypatch)
    assert accelerated == pure == {"age": 5}
    with pytest.raises(ValidationError):
        S().load({"age": -1})


# ---- native validators (Range / Length / OneOf) --------------------------


class ValidatorSchema(Schema):
    age = fields.Integer(validate=validate.Range(min=0, max=150))
    name = fields.String(validate=validate.Length(min=1, max=8))
    code = fields.String(validate=validate.Length(equal=3))
    role = fields.String(validate=validate.OneOf(["admin", "user", "guest"]))
    score = fields.Float(
        validate=validate.Range(min=0.0, max=100.0, max_inclusive=False)
    )
    tags = fields.List(fields.Integer(), validate=validate.Length(min=1))
    # two validators on one field -> both must pass
    pin = fields.Integer(validate=[validate.Range(min=1000), validate.Range(max=9999)])


_VALIDATOR_OK = {
    "age": 30,
    "name": "Foo",
    "code": "abc",
    "role": "user",
    "score": 50.0,
    "tags": [1, 2],
    "pin": 1234,
}


@pytest.mark.parametrize(
    "data",
    [
        _VALIDATOR_OK,
        {"age": 0, "score": 0.0},  # inclusive min boundary OK
        {"name": "12345678", "code": "xyz"},  # max-length boundary OK
        {},  # nothing present, nothing required
    ],
)
def test_load_validators_valid_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(ValidatorSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "data",
    [
        {"age": -1},  # below Range min
        {"age": 151},  # above Range max
        {"name": ""},  # below Length min
        {"name": "123456789"},  # above Length max
        {"code": "ab"},  # not equal length
        {"role": "root"},  # not in OneOf choices
        {"score": 100.0},  # max not inclusive -> fails at the bound
        {"tags": []},  # list Length min fails
        {"pin": 999},  # first Range fails
        {"pin": 10000},  # second Range fails
    ],
)
def test_load_validators_error_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        ValidatorSchema().load(data)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        ValidatorSchema().load(data)
    assert acc_exc.value.messages == py_exc.value.messages


def test_load_validators_is_native():
    schema = ValidatorSchema()
    schema.load({"age": 1})  # trigger lazy build
    assert accel.is_available() == (
        vars(schema).get("_mc_load_deserializer") is not None
    )


def test_load_unrecognized_validator_falls_back(monkeypatch):
    """A validator the core does not model keeps the field on the Python path."""

    def not_thirteen(value):
        if value == 13:
            raise ValidationError("unlucky")

    class S(Schema):
        x = fields.Integer(validate=not_thirteen)

    accelerated, pure = _load_both(S, {"x": 5}, monkeypatch=monkeypatch)
    assert accelerated == pure == {"x": 5}
    with pytest.raises(ValidationError):
        S().load({"x": 13})


def test_load_oneof_custom_choices_equivalence(monkeypatch):
    """OneOf over a set/tuple/range still matches stock."""

    class S(Schema):
        a = fields.String(validate=validate.OneOf({"x", "y"}))
        b = fields.Integer(validate=validate.OneOf(range(0, 10)))
        c = fields.Integer(validate=validate.OneOf((1, 2, 3)))

    accelerated, pure = _load_both(
        S, {"a": "x", "b": 5, "c": 2}, monkeypatch=monkeypatch
    )
    assert accelerated == pure


def test_load_post_load_hook_runs(monkeypatch):
    class S(Schema):
        i = fields.Integer()

        @post_load
        def add(self, data, **kwargs):
            data["doubled"] = data["i"] * 2
            return data

    accelerated, pure = _load_both(S, {"i": 5}, monkeypatch=monkeypatch)
    assert accelerated == pure == {"i": 5, "doubled": 10}


def test_load_schema_validator_runs(monkeypatch):
    class S(Schema):
        a = fields.Integer()
        b = fields.Integer()

        @validates("a")
        def check_a(self, value, **kwargs):
            if value < 0:
                raise ValidationError("must be non-negative")

    accelerated, pure = _load_both(S, {"a": 1, "b": 2}, monkeypatch=monkeypatch)
    assert accelerated == pure == {"a": 1, "b": 2}
    with pytest.raises(ValidationError):
        S().load({"a": -1, "b": 2})


def test_load_callback_base_exception_not_swallowed():
    """A ``BaseException`` (not ``Exception``) from a callback field must
    propagate unchanged, not be swallowed as ``AccelFallback`` and the field
    silently retried on the pure-Python path."""
    calls = []

    class Boom(fields.Field):
        def _deserialize(self, value, attr, data, **kwargs):
            calls.append(value)
            raise KeyboardInterrupt

    class S(Schema):
        ok = fields.Integer()  # native
        boom = Boom()  # callback

    with pytest.raises(KeyboardInterrupt):
        S().load({"ok": 1, "boom": 2})
    # Run exactly once: the accelerator propagates instead of converting to
    # AccelFallback (the old behaviour retried the whole load -> 2 calls).
    assert len(calls) == 1


# ---- native temporal / UUID on load --------------------------------------


class LoadTemporalSchema(Schema):
    iso = fields.DateTime()  # native
    rfc = fields.DateTime(format="rfc")  # native
    ts = fields.DateTime(format="timestamp")  # native
    strf = fields.DateTime(format="%Y/%m/%d")  # custom format -> callback
    day = fields.Date()  # native
    clock = fields.Time()  # native
    uid = fields.UUID()  # native
    naive = fields.NaiveDateTime()  # overrides _deserialize -> callback


@pytest.mark.parametrize(
    "data",
    [
        {
            "iso": "2020-01-02T03:04:05",
            "rfc": "Thu, 02 Jan 2020 03:04:05 +0000",
            "ts": 1577934245,
            "strf": "2020/01/02",
            "day": "2020-01-02",
            "clock": "03:04:05",
            "uid": "12345678-1234-5678-1234-567812345678",
            "naive": "2020-01-02T03:04:05",
        },
        {"day": "2020-01-02"},  # most fields missing
        {"uid": uuid.UUID("12345678-1234-5678-1234-567812345678")},  # passthrough
    ],
)
def test_load_temporal_uuid_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(LoadTemporalSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "data",
    [
        {"iso": "not-a-date"},  # bad ISO datetime
        {"day": "31-13-2020"},  # bad date
        {"clock": "99:99"},  # bad time
        {"uid": "not-a-uuid"},  # bad UUID
        {"ts": "abc"},  # bad timestamp
    ],
)
def test_load_temporal_uuid_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        LoadTemporalSchema().load(data)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        LoadTemporalSchema().load(data)
    assert acc_exc.value.messages == py_exc.value.messages


def test_load_temporal_uuid_is_native():
    schema = LoadTemporalSchema()
    schema.load({"day": "2020-01-02"})  # trigger lazy build
    assert accel.is_available() == (vars(schema).get("_mc_load_deserializer") is not None)


# ---- new native field types: Decimal / Dict / Constant -------------------


class DecimalDumpSchema(Schema):
    plain = fields.Decimal()
    as_str = fields.Decimal(as_string=True)
    placed = fields.Decimal(places=2)
    placed_str = fields.Decimal(places=2, as_string=True)
    nan_ok = fields.Decimal(allow_nan=True)


@pytest.mark.parametrize(
    "obj",
    [
        {
            "plain": decimal.Decimal("3.14159"),
            "as_str": decimal.Decimal("2.5"),
            "placed": decimal.Decimal("1.239"),
            "placed_str": decimal.Decimal("9.005"),
            "nan_ok": decimal.Decimal("1.0"),
        },
        {"plain": 5, "as_str": "2.50", "placed": 1.5},  # mixed input types
        dict.fromkeys(["plain", "as_str", "placed", "placed_str", "nan_ok"]),  # None
        {},  # all missing
    ],
)
def test_dump_decimal_equivalence(obj, monkeypatch):
    accelerated, pure = _dump_both(DecimalDumpSchema, obj, monkeypatch=monkeypatch)
    assert accelerated == pure


def test_dump_decimal_nan_equivalence(monkeypatch):
    # ``NaN != NaN``, so compare structurally rather than with ``==``.
    obj = {"nan_ok": decimal.Decimal("NaN")}
    accelerated, pure = _dump_both(DecimalDumpSchema, obj, monkeypatch=monkeypatch)
    assert accelerated["nan_ok"].is_nan() and pure["nan_ok"].is_nan()


class DecimalLoadSchema(Schema):
    plain = fields.Decimal()
    placed = fields.Decimal(places=2)
    nan_ok = fields.Decimal(allow_nan=True)
    ranged = fields.Decimal(validate=validate.Range(min=0))


@pytest.mark.parametrize(
    "data",
    [
        {"plain": "3.14", "placed": "1.239", "ranged": "5"},
        {"plain": 42},
        {},  # all missing
    ],
)
def test_load_decimal_valid_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(DecimalLoadSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


def test_load_decimal_nan_equivalence(monkeypatch):
    accelerated, pure = _load_both(
        DecimalLoadSchema, {"nan_ok": "NaN"}, monkeypatch=monkeypatch
    )
    assert accelerated["nan_ok"].is_nan() and pure["nan_ok"].is_nan()


@pytest.mark.parametrize(
    "data",
    [
        {"plain": "not-a-number"},  # InvalidOperation -> invalid
        {"plain": "NaN"},  # special (allow_nan False)
        {"plain": True},  # bool rejected
        {"ranged": "-1"},  # native validator failure
    ],
)
def test_load_decimal_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        DecimalLoadSchema().load(data)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        DecimalLoadSchema().load(data)
    assert acc_exc.value.messages == py_exc.value.messages


def test_decimal_is_native():
    d = DecimalDumpSchema()
    d.dump({"plain": decimal.Decimal("1")})
    ld = DecimalLoadSchema()
    ld.load({"plain": "1"})
    if accel.is_available():
        assert vars(d).get("_mc_dump_serializer") is not None
        assert vars(ld).get("_mc_load_deserializer") is not None


class DictSchema(Schema):
    meta = fields.Dict()


@pytest.mark.parametrize(
    "value",
    [{"a": 1, "b": [1, 2], "c": {"nested": True}}, {}, None],
)
def test_dump_dict_equivalence(value, monkeypatch):
    accelerated, pure = _dump_both(DictSchema, {"meta": value}, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "value",
    [{"a": 1, "b": "x"}, {}],
)
def test_load_dict_equivalence(value, monkeypatch):
    accelerated, pure = _load_both(DictSchema, {"meta": value}, monkeypatch=monkeypatch)
    assert accelerated == pure


def test_load_dict_non_mapping_error_matches_python(monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        DictSchema().load({"meta": [1, 2, 3]})  # not a Mapping
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        DictSchema().load({"meta": [1, 2, 3]})
    assert acc_exc.value.messages == py_exc.value.messages


def test_dict_with_value_field_falls_back(monkeypatch):
    """A Dict with a value field uses the callback path (still equivalent)."""

    class S(Schema):
        m = fields.Dict(values=fields.Integer())

    accelerated, pure = _dump_both(S, {"m": {"a": 1}}, monkeypatch=monkeypatch)
    assert accelerated == pure
    acc_load, pure_load = _load_both(S, {"m": {"a": "5"}}, monkeypatch=monkeypatch)
    assert acc_load == pure_load == {"m": {"a": 5}}


class ConstantSchema(Schema):
    ver = fields.Constant("v1")
    none_const = fields.Constant(None)
    num = fields.Integer()


@pytest.mark.parametrize(
    "data",
    [
        {"num": 7},
        {"ver": "ignored", "none_const": "ignored", "num": 1},  # input ignored
        {},
    ],
)
def test_dump_constant_equivalence(data, monkeypatch):
    accelerated, pure = _dump_both(ConstantSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "data",
    [
        {"num": 7},
        {"ver": "ignored", "num": 1},
        {},
    ],
)
def test_load_constant_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(ConstantSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


# ---- partial=True acceleration -------------------------------------------


class PartialInner(Schema):
    a = fields.Integer(required=True)
    b = fields.Integer(load_default=99)


class PartialSchema(Schema):
    id = fields.Integer(required=True)
    name = fields.String(load_default="DEF")
    inner = fields.Nested(PartialInner, required=True)
    tags = fields.List(fields.Integer())


@pytest.mark.parametrize(
    "data",
    [
        {"id": 1},  # missing required/defaulted fields skipped (no default applied)
        {"id": 1, "inner": {}, "tags": [1, 2]},  # partial propagates into nested
        {"id": 1, "name": "x", "inner": {"a": 5}},  # present values deserialize
        {"id": 1, "inner": {"b": 7}},  # nested required skipped, no default on a
    ],
)
def test_load_partial_true_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(
        PartialSchema, data, monkeypatch=monkeypatch, partial=True
    )
    assert accelerated == pure


def test_load_partial_true_is_native():
    schema = PartialSchema(partial=True)
    schema.load({"id": 1})  # trigger lazy build
    assert accel.is_available() == (vars(schema).get("_mc_load_deserializer") is not None)


def test_load_partial_true_default_not_applied(monkeypatch):
    """``partial=True`` skips missing fields entirely (no ``load_default``)."""
    accelerated, pure = _load_both(
        PartialSchema, {"id": 1}, monkeypatch=monkeypatch, partial=True
    )
    assert accelerated == pure == {"id": 1}


@pytest.mark.parametrize(
    "data",
    [
        {"id": 1, "inner": {"a": "NaN"}},  # present-but-invalid still errors
        {"id": 1, "name": None, "inner": {"a": 5}},  # null for non-nullable errors
    ],
)
def test_load_partial_true_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        PartialSchema(partial=True).load(data)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        PartialSchema(partial=True).load(data)
    assert acc_exc.value.messages == py_exc.value.messages


def test_load_partial_collection_defers_but_matches(monkeypatch):
    """Collection/dotted ``partial`` keeps its (defaults-applied) semantics."""
    accelerated, pure = _load_both(
        PartialSchema,
        {"id": 1, "inner": {"a": 5}},
        monkeypatch=monkeypatch,
        partial=("name",),
    )
    assert accelerated == pure


def test_load_partial_nested_own_option_defers(monkeypatch):
    """A nested schema's own ``partial`` must not be captured by the root flag."""

    class Outer(Schema):
        id = fields.Integer(required=True)
        inner = fields.Nested(PartialInner(partial=True))

    # Parent NOT partial; inner is partial=True on its own -> missing required ``a``
    # is allowed. The core must defer this (build None) so semantics match.
    accelerated, pure = _load_both(
        Outer, {"id": 1, "inner": {}}, monkeypatch=monkeypatch
    )
    assert accelerated == pure == {"id": 1, "inner": {}}


# ---- protocol-version handshake ------------------------------------------


def test_core_active_when_importable():
    """If the compiled core is importable it must actually be usable.

    A built-but-broken or version-mismatched extension is a loud failure here,
    not a silent no-op. When the core is not built at all (e.g. the non-``accel``
    tox environments) this skips visibly rather than passing trivially.
    """
    pytest.importorskip("marshmallow_core._core")
    assert accel.is_available()
    schema = LoadFlatSchema()
    schema.load({"i": 1})  # trigger the lazy build
    assert vars(schema).get("_mc_load_deserializer") is not None


def test_protocol_version_matches():
    """The compiled extension and ``_accel`` agree on the wire-format version."""
    if accel._core is None:
        pytest.skip("extension not built")
    assert accel._core.PROTOCOL_VERSION == accel._EXPECTED_PROTOCOL


def test_protocol_mismatch_disables_accel(monkeypatch):
    """A version mismatch must disable the core (pure-Python is always correct)."""

    class _StubCore:
        PROTOCOL_VERSION = accel._EXPECTED_PROTOCOL + 1
        AccelFallback = accel.AccelFallback

    monkeypatch.setattr(accel, "_core", _StubCore)
    assert accel.is_available() is False
    assert accel.build_load_deserializer(LoadFlatSchema()) is None
    assert accel.build_dump_serializer(FlatSchema()) is None
