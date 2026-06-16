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
    pre_load,
    validate,
    validates,
    validates_schema,
)

from importlib.metadata import version as _pkg_version

# marshmallow's major version. The 3.x and 4.x ``fields.TimeDelta`` deserializers
# differ: 4.x accepts float-strings and passes a ``timedelta`` through unchanged,
# while 3.x rejects both with "Not a valid period of time.". Inputs exercising
# those 4.x-only semantics are skipped on the 3.x line (where stock marshmallow
# itself raises, so there is no value to compare).
_MA_MAJOR = int(_pkg_version("marshmallow").split(".", 1)[0])


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

    def __eq__(self, other):
        return isinstance(other, Obj) and self.__dict__ == other.__dict__

    def __repr__(self):
        return f"Obj({self.__dict__!r})"


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


@pytest.mark.parametrize(
    "obj",
    [
        # UTC timezone → "+00:00"
        {"v": dt.datetime(2023, 6, 15, 12, 30, 45, tzinfo=dt.timezone.utc)},
        # Positive offset (UTC+5:30) → "+05:30"
        {
            "v": dt.datetime(
                2023,
                6,
                15,
                12,
                30,
                45,
                tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)),
            )
        },
        # Negative offset (UTC-5) → "-05:00"
        {
            "v": dt.datetime(
                2023,
                6,
                15,
                12,
                30,
                45,
                tzinfo=dt.timezone(dt.timedelta(hours=-5)),
            )
        },
        # Microseconds (no timezone)
        {"v": dt.datetime(2023, 6, 15, 12, 30, 45, 123456)},
        # Microseconds + timezone → ".123456+00:00"
        {"v": dt.datetime(2023, 6, 15, 12, 30, 45, 123456, tzinfo=dt.timezone.utc)},
        # Naive (no microseconds) — baseline
        {"v": dt.datetime(2020, 1, 2, 3, 4, 5)},
    ],
    ids=["utc", "plus530", "minus5", "us", "us+utc", "naive"],
)
def test_temporal_native_datetime_edge_cases(obj, monkeypatch):
    class S(Schema):
        v = fields.DateTime()

    accelerated, pure = _dump_both(S, obj, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "obj",
    [
        {"v": dt.time(12, 30, 45)},
        {"v": dt.time(12, 30, 45, 123456)},
        {"v": dt.time(12, 30, 45, tzinfo=dt.timezone.utc)},
        {"v": dt.time(12, 30, 45, 123456, tzinfo=dt.timezone.utc)},
    ],
    ids=["plain", "us", "utc", "us+utc"],
)
def test_temporal_native_time_edge_cases(obj, monkeypatch):
    class S(Schema):
        v = fields.Time()

    accelerated, pure = _dump_both(S, obj, monkeypatch=monkeypatch)
    assert accelerated == pure


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


def test_n1_generator_in_nested_many_with_dicttyped_sibling(monkeypatch):
    """N1: a generator inside a many=True Nested field must not be silently
    exhausted when a sibling DictTyped(UserDict) field triggers AccelFallback.
    """
    from collections import UserDict

    class Inner(Schema):
        x = fields.Integer()

    class S(Schema):
        a = fields.Nested(Inner, many=True)
        b = fields.Dict(keys=fields.String(), values=fields.Integer())

    gen_obj = {"a": ({"x": i} for i in range(3)), "b": UserDict({"k": 1})}
    # pure-Python path is the reference
    monkeypatch.setattr(accel, "build_dump_serializer", lambda schema: None)
    expected = S().dump(gen_obj)
    monkeypatch.undo()

    gen_obj2 = {"a": ({"x": i} for i in range(3)), "b": UserDict({"k": 1})}
    accelerated = S().dump(gen_obj2)
    assert accelerated == expected


def test_n1_generator_in_list_field_with_dicttyped_sibling(monkeypatch):
    """N1: a generator inside a List field must not be silently exhausted
    when a sibling DictTyped field triggers AccelFallback.
    """
    from collections import UserDict

    class S(Schema):
        a = fields.List(fields.Integer())
        b = fields.Dict(keys=fields.String(), values=fields.Integer())

    gen_obj = {"a": (i for i in range(3)), "b": UserDict({"k": 1})}
    monkeypatch.setattr(accel, "build_dump_serializer", lambda schema: None)
    expected = S().dump(gen_obj)
    monkeypatch.undo()

    gen_obj2 = {"a": (i for i in range(3)), "b": UserDict({"k": 1})}
    accelerated = S().dump(gen_obj2)
    assert accelerated == expected


# ---- fused dumps (dump -> JSON string in Rust) ----------------------------


def _dumps_both(schema_factory, obj, *, many=False, monkeypatch, **kwargs):
    """Return (fused, stock) JSON strings for ``obj``."""
    fused = schema_factory().dumps(obj, many=many, **kwargs)
    monkeypatch.setattr(accel, "build_dump_json_serializer", lambda schema: None)
    stock = schema_factory().dumps(obj, many=many, **kwargs)
    return fused, stock


def test_dumps_flat_equivalence(monkeypatch):
    obj = {"i": 3, "f": 1.5, "s": 'a "quote"\n', "b": True, "r": [1, None, False]}
    fused, stock = _dumps_both(FlatSchema, obj, monkeypatch=monkeypatch)
    assert fused == stock


def test_dumps_unicode_and_floats_equivalence(monkeypatch):
    class S(Schema):
        s = fields.String()
        f = fields.Float()
        big = fields.Float()

    obj = {"s": "héllo \U0001f600 / world", "f": 0.1, "big": 2.5e20}
    fused, stock = _dumps_both(S, obj, monkeypatch=monkeypatch)
    assert fused == stock


def test_dumps_nested_and_list_equivalence(monkeypatch):
    obj = {
        "people": [
            {
                "name": "Foo",
                "age": 30,
                "address": {"city": "X", "coordinates": {"lat": 1.0, "lng": 2.0}},
                "tags": ["a", "b"],
                "scores": [1, 2, 3],
            },
            {"name": "Bar", "age": 41, "address": None, "tags": [], "scores": None},
        ],
        "total": 2,
    }
    fused, stock = _dumps_both(Container, obj, monkeypatch=monkeypatch)
    assert fused == stock


def test_dumps_temporal_enum_uuid_equivalence(monkeypatch):
    fused, stock = _dumps_both(
        TemporalEnumSchema, _TEMPORAL_OBJ, monkeypatch=monkeypatch
    )
    assert fused == stock


def test_dumps_many_equivalence(monkeypatch):
    objs = [Obj(i=n, f=float(n), s=str(n), b=bool(n % 2), r=n) for n in range(4)]
    fused, stock = _dumps_both(
        lambda: FlatSchema(many=True), objs, many=True, monkeypatch=monkeypatch
    )
    assert fused == stock


def test_dumps_with_kwargs_uses_stock(monkeypatch):
    """Extra json kwargs (indent/sort_keys) must defer to stock json.dumps."""
    obj = {"i": 3, "f": 1.5, "s": "hi", "b": True, "r": 1}
    fused = FlatSchema().dumps(obj, indent=2, sort_keys=True)
    monkeypatch.setattr(accel, "build_dump_json_serializer", lambda schema: None)
    stock = FlatSchema().dumps(obj, indent=2, sort_keys=True)
    assert fused == stock


def test_dumps_post_dump_hook_equivalence(monkeypatch):
    class S(Schema):
        i = fields.Integer()

        @post_dump
        def add(self, data, **kwargs):
            data["doubled"] = data["i"] * 2
            return data

    fused, stock = _dumps_both(S, {"i": 5}, monkeypatch=monkeypatch)
    assert fused == stock


def test_dumps_decimal_raises_like_stock(monkeypatch):
    class S(Schema):
        d = fields.Decimal()

    with pytest.raises(TypeError):
        S().dumps({"d": decimal.Decimal("1.5")})


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


def test_truthy_nonbool_many_regression(monkeypatch):
    """A truthy non-bool ``many`` (e.g. ``Schema(many=1)``) must not crash.

    Stock marshmallow stores ``many`` raw and treats it truthily; the Rust
    boundary takes a strict ``bool``, so every entry point must coerce it
    (ARCH.md A1 — dump/dumps/loads previously raised ``TypeError`` here).
    """

    class S(Schema):
        i = fields.Integer()
        s = fields.String()

    data = [{"i": 1, "s": "a"}, {"i": 2, "s": "b"}]
    json_data = '[{"i": 1, "s": "a"}, {"i": 2, "s": "b"}]'

    # Core active (autouse install); ``many=1`` is stored on the instance and
    # reaches each patched method as a raw ``int``.
    acc_dump = S(many=1).dump(data)
    acc_load = S(many=1).load(data)
    acc_dumps = S(many=1).dumps(data)
    acc_loads = S(many=1).loads(json_data)

    # Force the pure-Python path and confirm byte-for-byte equivalence.
    monkeypatch.setattr(accel, "build_dump_serializer", lambda schema: None)
    monkeypatch.setattr(accel, "build_dump_json_serializer", lambda schema: None)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)

    assert acc_dump == S(many=1).dump(data)
    assert acc_load == S(many=1).load(data)
    assert acc_dumps == S(many=1).dumps(data)
    assert acc_loads == S(many=1).loads(json_data)


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
    assert accel.is_available() == (vars(schema).get("_mc_load_plan") is not None)


def test_load_nested_is_native():

    schema = LoadContainer()
    schema.load({"people": [], "total": 0})  # trigger lazy build
    assert accel.is_available() == (vars(schema).get("_mc_load_plan") is not None)


class _Region:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, _Region) and other.name == self.name


class _OverridesLoadSchema(Schema):
    """A schema that customises ``load`` directly instead of via ``post_load``.

    Mirrors ``marshmallow_dataclass`` (which overrides ``Schema.load`` to build a
    dataclass instance and leaves ``_hooks`` empty).
    """

    name = fields.String()

    def load(self, data, **kwargs):
        return _Region(**super().load(data, **kwargs))


class _OverridesDumpSchema(Schema):
    """A schema that customises ``dump`` directly instead of via ``post_dump``."""

    name = fields.String()

    def dump(self, obj, **kwargs):
        result = super().dump(obj, **kwargs)
        result["dumped"] = True
        return result


def test_load_nested_overridden_load_uses_callback(monkeypatch):
    """A ``Nested`` whose inner schema overrides ``load`` must use the callback
    path so the override runs — compiling it natively would emit a plain ``dict``
    instead of the override's instance (regression: marshmallow_dataclass)."""

    class _NestedOverrideSchema(Schema):
        id = fields.Integer()
        region = fields.Nested(_OverridesLoadSchema)

    accelerated, pure = _load_both(
        _NestedOverrideSchema,
        {"id": 1, "region": {"name": "Moscow"}},
        monkeypatch=monkeypatch,
    )
    assert accelerated == pure
    assert accelerated["region"] == _Region("Moscow")


def test_dump_nested_overridden_dump_uses_callback(monkeypatch):
    """Symmetric to the load case: a ``Nested`` whose inner schema overrides
    ``dump`` must use the callback path (the dump core has no fallback)."""

    class _NestedOverrideSchema(Schema):
        id = fields.Integer()
        region = fields.Nested(_OverridesDumpSchema)

    accelerated, pure = _dump_both(
        _NestedOverrideSchema,
        {"id": 1, "region": {"name": "Moscow"}},
        monkeypatch=monkeypatch,
    )
    assert accelerated == pure
    assert accelerated["region"]["dumped"] is True


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


class LoadBooleanSchema(Schema):
    flag = fields.Boolean()
    maybe = fields.Boolean(allow_none=True)


@pytest.mark.parametrize(
    "data",
    [
        {"flag": True, "maybe": False},  # actual bools
        {"flag": "true", "maybe": "false"},  # truthy/falsy strings
        {"flag": 1, "maybe": 0},  # 1/0 (hash-equal to True/False)
        {"flag": "yes", "maybe": "no"},
        {"maybe": None},  # null on an allow_none field
        {},  # missing, nothing required
    ],
)
def test_load_boolean_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(LoadBooleanSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "data",
    [
        {"flag": "maybe"},  # not in truthy/falsy -> invalid
        {"flag": 2},  # int not in the sets
        {"flag": [1]},  # unhashable -> TypeError -> invalid
        {"flag": None},  # null on a non-nullable field
    ],
)
def test_load_boolean_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        LoadBooleanSchema().load(data)

    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        LoadBooleanSchema().load(data)

    assert acc_exc.value.messages == py_exc.value.messages


def test_load_boolean_is_native():
    """A default ``Boolean`` compiles to a native load element (regression: it
    used to fall back to the Python callback path)."""
    from marshmallow_core import _compiler

    element = _compiler._build_load_element(fields.Boolean(), ())
    assert element is not None
    assert element[0] == _compiler._L_BOOLEAN


def test_load_boolean_custom_truthy(monkeypatch):
    """A field with customised ``truthy``/``falsy`` honours its own sets."""

    class S(Schema):
        flag = fields.Boolean(truthy={"Y"}, falsy={"N"})

    accelerated, pure = _load_both(S, {"flag": "Y"}, monkeypatch=monkeypatch)
    assert accelerated == pure == {"flag": True}


class LoadStrictIntSchema(Schema):
    n = fields.Integer(strict=True)


@pytest.mark.parametrize("data", [{"n": 5}, {"n": -1}, {"n": 0}, {}])
def test_load_strict_int_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(LoadStrictIntSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "data",
    [
        {"n": "5"},  # strict rejects str (non-strict would coerce)
        {"n": 2.5},  # strict rejects float
        {"n": True},  # bool rejected
        {"n": "x"},
    ],
)
def test_load_strict_int_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        LoadStrictIntSchema().load(data)

    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        LoadStrictIntSchema().load(data)

    assert acc_exc.value.messages == py_exc.value.messages


def test_load_strict_int_is_native():
    from marshmallow_core import _compiler

    element = _compiler._build_load_element(fields.Integer(strict=True), ())
    assert element == (_compiler._L_INTEGER_STRICT,)


class LoadTypedDictSchema(Schema):
    counts = fields.Dict(keys=fields.String(), values=fields.Integer())
    vals_only = fields.Dict(values=fields.Float())
    keys_only = fields.Dict(keys=fields.String())


@pytest.mark.parametrize(
    "data",
    [
        {"counts": {"a": 1, "b": "2"}},  # values coerced "2" -> 2
        {"vals_only": {"x": "1.5", "y": 2}},
        {"keys_only": {"a": "v", "b": "w"}},  # string keys pass the key field
        {"counts": {}},  # empty
        {},  # all missing
    ],
)
def test_load_typed_dict_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(LoadTypedDictSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "data",
    [
        {"counts": {"a": "not-an-int"}},  # value coercion failure
        {"counts": [1, 2]},  # not a mapping
        {"counts": {"a": None}},  # None value -> allow_none check in Python
    ],
)
def test_load_typed_dict_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        LoadTypedDictSchema().load(data)

    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        LoadTypedDictSchema().load(data)

    assert acc_exc.value.messages == py_exc.value.messages


def test_load_typed_dict_is_native():
    from marshmallow_core import _compiler

    field = fields.Dict(keys=fields.String(), values=fields.Integer())
    element = _compiler._build_load_element(field, ())
    assert element is not None and element[0] == _compiler._L_DICT_TYPED


def test_dump_typed_dict_is_native():
    """With the dump fallback in place, typed Dict dump compiles native."""
    from marshmallow_core import _compiler

    field = fields.Dict(keys=fields.String(), values=fields.Integer())
    element = _compiler._build_element(field, ())
    assert element is not None and element[0] == _compiler._DICT_TYPED


@pytest.mark.parametrize(
    "obj",
    [
        {"counts": {"a": 1, "b": 2}, "vals_only": {"x": 1.5}, "keys_only": {"k": "v"}},
        {"counts": {}},
        {},
    ],
)
def test_dump_typed_dict_equivalence(obj, monkeypatch):
    accelerated, pure = _dump_both(LoadTypedDictSchema, obj, monkeypatch=monkeypatch)
    assert accelerated == pure


class LoadTupleSchema(Schema):
    row = fields.Tuple((fields.String(), fields.Integer(), fields.Float()))


@pytest.mark.parametrize(
    "data",
    [
        {"row": ["a", 1, 2.5]},
        {"row": ("a", "7", "3.0")},  # coerced int/float
        {},  # missing
    ],
)
def test_load_tuple_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(LoadTupleSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "data",
    [
        {"row": ["a", 1]},  # too short
        {"row": ["a", 1, 2.5, 9]},  # too long
        {"row": ["a", "x", 2.5]},  # element coercion failure
        {"row": "abc"},  # string is not a valid tuple
        {"row": 5},  # not a sequence
    ],
)
def test_load_tuple_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        LoadTupleSchema().load(data)

    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        LoadTupleSchema().load(data)

    assert acc_exc.value.messages == py_exc.value.messages


def test_load_tuple_is_native():
    from marshmallow_core import _compiler

    field = fields.Tuple((fields.String(), fields.Integer()))
    element = _compiler._build_load_element(field, ())
    assert element is not None and element[0] == _compiler._L_TUPLE


@pytest.mark.parametrize(
    "obj",
    [
        {"row": ("a", 1, 2.5)},
        {"row": ["x", 7, 3.0]},  # list input dumps the same
    ],
)
def test_dump_tuple_equivalence(obj, monkeypatch):
    accelerated, pure = _dump_both(LoadTupleSchema, obj, monkeypatch=monkeypatch)
    assert accelerated == pure


def test_dump_tuple_is_native():
    from marshmallow_core import _compiler

    field = fields.Tuple((fields.String(), fields.Integer()))
    element = _compiler._build_element(field, ())
    assert element is not None and element[0] == _compiler._TUPLE


class _ArtistSchema(Schema):
    id = fields.Integer()
    name = fields.String()


class LoadPluckSchema(Schema):
    artist = fields.Pluck(_ArtistSchema, "id")


class LoadPluckManySchema(Schema):
    artists = fields.Pluck(_ArtistSchema, "id", many=True)


@pytest.mark.parametrize(
    ("factory", "data"),
    [
        (LoadPluckSchema, {"artist": 42}),
        (LoadPluckSchema, {"artist": "7"}),  # coerced to int by the inner field
        (LoadPluckSchema, {}),  # missing
        (LoadPluckManySchema, {"artists": [1, 2, 3]}),
        (LoadPluckManySchema, {"artists": []}),
    ],
)
def test_load_pluck_equivalence(factory, data, monkeypatch):
    accelerated, pure = _load_both(factory, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    ("factory", "data"),
    [
        (LoadPluckSchema, {"artist": "not-an-int"}),  # inner coercion failure
        (LoadPluckManySchema, {"artists": 5}),  # not a collection
        (LoadPluckManySchema, {"artists": [1, "x"]}),  # one bad element
    ],
)
def test_load_pluck_errors_match_python(factory, data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        factory().load(data)

    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        factory().load(data)

    assert acc_exc.value.messages == py_exc.value.messages


def test_load_pluck_is_native():
    from marshmallow_core import _compiler

    element = _compiler._build_load_element(fields.Pluck(_ArtistSchema, "id"), ())
    assert element is not None and element[0] == _compiler._L_PLUCK


@pytest.mark.parametrize(
    ("factory", "obj"),
    [
        (LoadPluckSchema, {"artist": {"id": 42, "name": "x"}}),
        (LoadPluckManySchema, {"artists": [{"id": 1}, {"id": 2}, {"id": 3}]}),
        (LoadPluckManySchema, {"artists": []}),
    ],
)
def test_dump_pluck_equivalence(factory, obj, monkeypatch):
    accelerated, pure = _dump_both(factory, obj, monkeypatch=monkeypatch)
    assert accelerated == pure


def test_dump_pluck_is_native():
    from marshmallow_core import _compiler

    element = _compiler._build_element(fields.Pluck(_ArtistSchema, "id"), ())
    assert element is not None and element[0] == _compiler._PLUCK


class TimeDeltaSchema(Schema):
    secs = fields.TimeDelta()  # precision="seconds"
    millis = fields.TimeDelta(precision="milliseconds")


@pytest.mark.parametrize(
    "loaded",
    [
        {"secs": dt.timedelta(seconds=90), "millis": dt.timedelta(milliseconds=1500)},
        {"secs": dt.timedelta(0), "millis": dt.timedelta(microseconds=123456)},
    ],
)
def test_dump_timedelta_equivalence(loaded, monkeypatch):
    accelerated, pure = _dump_both(TimeDeltaSchema, loaded, monkeypatch=monkeypatch)
    assert accelerated == pure


_ma4_only = pytest.mark.skipif(
    _MA_MAJOR < 4, reason="float-string / timedelta passthrough is marshmallow 4.x-only"
)


@pytest.mark.parametrize(
    "data",
    [
        {"secs": 90, "millis": 1500},
        # float-string, rounding
        pytest.param({"secs": "1.1234567", "millis": 0}, marks=_ma4_only),
        # already a timedelta -> passthrough
        pytest.param({"secs": dt.timedelta(seconds=5)}, marks=_ma4_only),
        {},
    ],
)
def test_load_timedelta_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(TimeDeltaSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize("data", [{"secs": "not-a-number"}, {"secs": None}])
def test_load_timedelta_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        TimeDeltaSchema().load(data)

    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        TimeDeltaSchema().load(data)

    assert acc_exc.value.messages == py_exc.value.messages


def test_timedelta_is_native():
    from marshmallow_core import _compiler

    field = fields.TimeDelta()
    assert _compiler._build_element(field, ())[0] == _compiler._TIMEDELTA
    assert _compiler._build_load_element(field, ())[0] == _compiler._L_TIMEDELTA


class AwarenessSchema(Schema):
    naive = fields.NaiveDateTime()
    aware = fields.AwareDateTime()


@pytest.mark.parametrize(
    "data",
    [
        {"naive": "2020-01-02T03:04:05", "aware": "2020-01-02T03:04:05+00:00"},
        {"naive": "2020-06-01T12:00:00"},
        {},
    ],
)
def test_load_awareness_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(AwarenessSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "data",
    [
        {"naive": "2020-01-02T03:04:05+00:00"},  # aware input -> naive field errors
        {"aware": "2020-01-02T03:04:05"},  # naive input -> aware field errors
        {"naive": "not-a-date"},
    ],
)
def test_load_awareness_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        AwarenessSchema().load(data)

    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        AwarenessSchema().load(data)

    assert acc_exc.value.messages == py_exc.value.messages


def test_load_awareness_is_native():
    from marshmallow_core import _compiler

    assert (
        _compiler._build_load_element(fields.NaiveDateTime(), ())[0]
        == _compiler._L_DATETIME_AWARENESS
    )
    assert (
        _compiler._build_load_element(fields.AwareDateTime(), ())[0]
        == _compiler._L_DATETIME_AWARENESS
    )


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


class IncludeSchema(Schema):
    a = fields.Integer()
    b = fields.Integer()

    class Meta:
        unknown = INCLUDE


def test_load_include_equivalence(monkeypatch):
    """``unknown=INCLUDE`` keeps unknown keys natively, matching pure Python."""
    data = {"b": "2", "z": 9, "a": "1", "y": [1, 2]}
    accelerated, pure = _load_both(IncludeSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure == {"a": 1, "b": 2, "z": 9, "y": [1, 2]}


def test_load_include_is_native():
    schema = IncludeSchema()
    schema.load({"a": 1, "extra": "x"})  # trigger lazy build
    assert accel.is_available() == (vars(schema).get("_mc_load_plan") is not None)


def test_load_include_nested_equivalence(monkeypatch):
    class Inner(Schema):
        x = fields.Integer()

        class Meta:
            unknown = INCLUDE

    class Outer(Schema):
        inner = fields.Nested(Inner)

        class Meta:
            unknown = INCLUDE

    data = {"inner": {"x": 1, "extra": "kept"}, "top": True}
    accelerated, pure = _load_both(Outer, data, monkeypatch=monkeypatch)
    assert accelerated == pure


class DottedLoadSchema(Schema):
    a = fields.Integer(attribute="nested.value")
    b = fields.String(attribute="nested.deep.name")
    c = fields.Integer()
    when = fields.DateTime(attribute="meta.ts")  # callback field, dotted


@pytest.mark.parametrize(
    "data",
    [
        {"a": "5", "b": "hi", "c": 1, "when": "2020-01-02T03:04:05"},
        {"a": 7},  # only one dotted field present
        {"c": 9},  # no dotted fields present
    ],
)
def test_load_dotted_attribute_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(DottedLoadSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


def test_load_dotted_attribute_is_native():
    schema = DottedLoadSchema()
    schema.load({"c": 1})  # trigger lazy build
    assert accel.is_available() == (vars(schema).get("_mc_load_plan") is not None)


def test_load_dotted_default_and_none(monkeypatch):
    class S(Schema):
        a = fields.Integer(attribute="g.x", load_default=42)
        b = fields.String(attribute="g.y", allow_none=True)

    accelerated, pure = _load_both(S, {"b": None}, monkeypatch=monkeypatch)
    assert accelerated == pure == {"g": {"x": 42, "y": None}}


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
    assert accel.is_available() == (vars(schema).get("_mc_load_plan") is not None)


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


class EqNoneContainsSchema(Schema):
    eq = fields.Integer(validate=validate.Equal(42))
    none = fields.String(validate=validate.NoneOf(["x", "y", "z"]))
    only = fields.List(fields.String(), validate=validate.ContainsOnly(["a", "b", "c"]))


@pytest.mark.parametrize(
    "data",
    [
        {"eq": 42, "none": "ok", "only": ["a", "b"]},
        {"only": []},  # empty passes ContainsOnly
        {"only": ["a", "a", "c"]},  # duplicates allowed
        {},
    ],
)
def test_load_eq_none_contains_valid(data, monkeypatch):
    accelerated, pure = _load_both(EqNoneContainsSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "data",
    [
        {"eq": 41},  # not Equal
        {"none": "y"},  # in NoneOf iterable
        {"only": ["a", "q"]},  # element not in ContainsOnly choices
    ],
)
def test_load_eq_none_contains_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        EqNoneContainsSchema().load(data)

    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        EqNoneContainsSchema().load(data)

    assert acc_exc.value.messages == py_exc.value.messages


def test_load_eq_none_contains_is_native():
    from marshmallow_core import _compiler

    assert _compiler._build_validator(validate.Equal(1)) == (_compiler._V_EQUAL, 1)
    assert _compiler._build_validator(validate.NoneOf([1]))[0] == _compiler._V_NONEOF
    assert (
        _compiler._build_validator(validate.ContainsOnly([1]))[0]
        == _compiler._V_CONTAINSONLY
    )


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


# ---- hook-bearing load schemas (accelerated per-field step) --------------


class HookLoadSchema(Schema):
    a = fields.Integer()
    b = fields.String()
    c = fields.Integer(validate=validate.Range(min=0))

    @pre_load
    def inject(self, data, **kwargs):
        data = dict(data)
        data.setdefault("a", 0)
        return data

    @post_load
    def total(self, data, **kwargs):
        data["seen"] = sorted(data)
        return data

    @validates("b")
    def check_b(self, value, **kwargs):
        if value == "bad":
            raise ValidationError("b is bad")

    @validates_schema
    def check_all(self, data, **kwargs):
        if data.get("a") == data.get("c"):
            raise ValidationError("a must differ from c")


@pytest.mark.parametrize(
    "data",
    [
        {"a": 5, "b": "x", "c": 3},
        {"b": "y", "c": 1},  # pre_load injects a
        {"a": 7},  # b/c missing
    ],
)
def test_load_hooks_valid_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(HookLoadSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "data",
    [
        {"a": 1, "b": "bad", "c": 3},  # field-level @validates fails
        {"a": 4, "b": "x", "c": 4},  # @validates_schema fails
        {"a": 1, "b": "x", "c": -5},  # native validator fails
        {"a": "NaN", "b": "x"},  # field deserialize fails
        {"a": 1, "b": 123, "c": 2},  # b not a string
    ],
)
def test_load_hooks_error_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        HookLoadSchema().load(data)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        HookLoadSchema().load(data)
    assert acc_exc.value.messages == py_exc.value.messages


def test_load_hooks_is_native():
    schema = HookLoadSchema()
    schema.load({"a": 1, "b": "x", "c": 2})  # trigger lazy build
    assert accel.is_available() == (vars(schema).get("_mc_load_plan") is not None)


def test_accel_load_supported_for_installed_marshmallow():
    """The transcription tripwire must pass against the marshmallow under test
    (otherwise the accelerated hook path is silently disabled in CI)."""
    from marshmallow_core import _patch

    assert _patch._accel_load_supported() is True
    assert _patch._ACCEL_LOAD_VERIFIED is True  # set by the autouse install()


def test_unverified_accel_load_falls_back_to_pure(monkeypatch):
    """When the ``_do_load`` internals look untested, a hook-bearing schema must
    still load correctly via the pure-Python path (ARCH.md A2 tripwire)."""
    from marshmallow_core import _patch

    monkeypatch.setattr(_patch, "_ACCEL_LOAD_VERIFIED", False)
    # Equivalence still holds — the accelerated branch just isn't taken.
    accelerated, pure = _load_both(
        HookLoadSchema, {"a": 5, "b": "x", "c": 3}, monkeypatch=monkeypatch
    )
    assert accelerated == pure
    assert accelerated == {"a": 5, "b": "x", "c": 3, "seen": ["a", "b", "c"]}


def test_accel_load_supported_detects_missing_invoker(monkeypatch):
    """A removed/renamed private invoker trips the tripwire (returns False)."""
    from marshmallow_core import _patch

    monkeypatch.delattr(Schema, "_invoke_field_validators", raising=True)
    assert _patch._accel_load_supported() is False


def test_load_pre_load_validation_error_matches(monkeypatch):
    """A ``pre_load`` that raises must produce identical errors via both paths."""

    class S(Schema):
        x = fields.Integer()

        @pre_load
        def guard(self, data, **kwargs):
            if "x" not in data:
                raise ValidationError("x required", field_name="x")
            return data

    with pytest.raises(ValidationError) as acc_exc:
        S().load({})
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        S().load({})
    assert acc_exc.value.messages == py_exc.value.messages


def test_load_hooks_many_equivalence(monkeypatch):
    accelerated, pure = _load_both(
        lambda: HookLoadSchema(many=True),
        [{"a": 1, "b": "x", "c": 2}, {"a": 9, "b": "z", "c": 0}],
        many=True,
        monkeypatch=monkeypatch,
    )
    assert accelerated == pure


def test_load_post_load_only_uses_core(monkeypatch):
    """A schema whose only hook is ``post_load`` is accelerated (not deferred)."""

    class S(Schema):
        i = fields.Integer()

        @post_load
        def add(self, data, **kwargs):
            data["doubled"] = data["i"] * 2
            return data

    schema = S()
    schema.load({"i": 5})
    assert accel.is_available() == (vars(schema).get("_mc_load_plan") is not None)
    accelerated, pure = _load_both(S, {"i": 5}, monkeypatch=monkeypatch)
    assert accelerated == pure == {"i": 5, "doubled": 10}


# ---- NestedPostLoad (_L_NESTED_POST_LOAD) ------------------------------------


class _PostLoadInner(Schema):
    """Schema with only a @post_load hook — the NestedPostLoad target."""

    x = fields.Integer()
    y = fields.String()

    @post_load
    def make_obj(self, data, **kwargs):
        return Obj(**data)


class _PostLoadOuter(Schema):
    name = fields.String()
    inner = fields.Nested(_PostLoadInner())


class _PostLoadOuterMany(Schema):
    name = fields.String()
    items = fields.List(fields.Nested(_PostLoadInner()))


def test_nested_post_load_is_native():
    """A nested @post_load-only schema compiles to a NestedPostLoad element."""
    schema = _PostLoadOuter()
    schema.load({"name": "t", "inner": {"x": 1, "y": "a"}})
    if accel.is_available():
        plan = vars(schema).get("_mc_load_plan")
        assert plan is not None, "schema should be accelerated"


def test_nested_post_load_equivalence(monkeypatch):
    data = {"name": "Alice", "inner": {"x": 7, "y": "hi"}}
    accelerated, pure = _load_both(_PostLoadOuter, data, monkeypatch=monkeypatch)
    assert accelerated == pure
    assert isinstance(accelerated["inner"], Obj)
    assert accelerated["inner"].x == 7


def test_nested_post_load_none_allowed(monkeypatch):
    class S(Schema):
        inner = fields.Nested(_PostLoadInner(), allow_none=True)

    accelerated, pure = _load_both(S, {"inner": None}, monkeypatch=monkeypatch)
    assert accelerated == pure == {"inner": None}


def test_nested_post_load_many(monkeypatch):
    """Nested field with many=True and @post_load on inner schema."""

    class Inner(Schema):
        v = fields.Integer()

        @post_load
        def wrap(self, data, **kwargs):
            return Obj(**data)

    class Outer(Schema):
        items = fields.Nested(Inner(many=True))

    data = {"items": [{"v": 1}, {"v": 2}, {"v": 3}]}
    accelerated, pure = _load_both(Outer, data, monkeypatch=monkeypatch)
    assert accelerated == pure
    assert all(isinstance(i, Obj) for i in accelerated["items"])


def test_nested_post_load_via_list_field(monkeypatch):
    """List(Nested(@post_load-schema)) — the List wraps the NestedPostLoad."""
    data = {"name": "x", "items": [{"x": 1, "y": "a"}, {"x": 2, "y": "b"}]}
    accelerated, pure = _load_both(_PostLoadOuterMany, data, monkeypatch=monkeypatch)
    assert accelerated == pure
    assert all(isinstance(i, Obj) for i in accelerated["items"])


def test_nested_post_load_root_schema_equivalence(monkeypatch):
    """Root @post_load schema unchanged — accelerated via _accelerated_load."""
    accelerated, pure = _load_both(
        _PostLoadInner, {"x": 3, "y": "z"}, monkeypatch=monkeypatch
    )
    assert accelerated == pure
    assert isinstance(accelerated, Obj)


def test_nested_post_load_error_falls_back(monkeypatch):
    """An invalid inner value still raises a ValidationError (not AccelFallback)."""
    with pytest.raises(ValidationError):
        _PostLoadOuter().load({"name": "x", "inner": {"x": "not-an-int", "y": "a"}})


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
    assert accel.is_available() == (vars(schema).get("_mc_load_plan") is not None)


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
        assert vars(ld).get("_mc_load_plan") is not None


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
    assert accel.is_available() == (vars(schema).get("_mc_load_plan") is not None)


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


@pytest.mark.parametrize(
    "data,partial",
    [
        ({"id": 1, "inner": {"a": 5}}, ("name",)),  # name optional, default skipped
        ({"id": 1}, ("name",)),  # nested still required -> error
        ({"id": 1, "name": "x"}, ("inner",)),  # whole nested optional
        ({"id": 1, "inner": {"b": 7}}, ["inner.a"]),  # dotted: nested.a optional
        ({"id": 1, "inner": {}}, ("inner.a", "name")),  # dotted + flat
        ({}, ("id", "name", "inner", "tags")),  # everything optional
        ({"id": 1, "inner": {"a": "x"}}, ("name",)),  # present-but-invalid errors
        ({"id": 1, "name": None, "inner": {"a": 5}}, ("name",)),  # null non-nullable
    ],
)
def test_load_partial_collection_equivalence(data, partial, monkeypatch):
    """Collection/dotted ``partial`` is accelerated and matches pure Python."""
    try:
        accelerated = PartialSchema().load(data, partial=partial)
        acc_err = None
    except ValidationError as exc:
        accelerated, acc_err = None, exc.messages
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    try:
        pure = PartialSchema().load(data, partial=partial)
        pure_err = None
    except ValidationError as exc:
        pure, pure_err = None, exc.messages
    assert accelerated == pure
    assert acc_err == pure_err


def test_load_partial_collection_is_native():
    schema = PartialSchema()
    schema.load({"id": 1, "inner": {"a": 5}}, partial=("name",))  # lazy build
    assert accel.is_available() == (vars(schema).get("_mc_load_plan") is not None)


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


# ---- Email/Url + arbitrary (Python-arm) validators -----------------------


def _custom_even(v):
    if v % 2:
        raise ValidationError("must be even")


def _custom_nonneg(v):
    return v >= 0  # plain callable: a ``False`` return means fail


class ValidatorArmSchema(Schema):
    email = fields.Email()
    url = fields.Url()
    rx = fields.String(validate=validate.Regexp(r"^[a-z]+$"))
    even = fields.Integer(validate=_custom_even)
    nonneg = fields.Integer(validate=_custom_nonneg)
    # native ``Range`` + a Python-arm callable on the same field
    multi = fields.Integer(validate=[validate.Range(min=0), _custom_even])


def test_validator_arm_is_native():
    from marshmallow_core import _compiler

    # Email/Url deserialize as String; a custom callable compiles to _V_PYTHON.
    assert _compiler._build_load_element(fields.Email(), ())[0] == _compiler._L_STRING
    assert _compiler._build_load_element(fields.Url(), ())[0] == _compiler._L_STRING
    assert _compiler._build_validator(_custom_even)[0] == _compiler._V_PYTHON
    assert _compiler._build_validator(validate.Regexp("x"))[0] == _compiler._V_PYTHON


@pytest.mark.parametrize(
    "data",
    [
        {
            "email": "a@b.com",
            "url": "http://x.com",
            "rx": "abc",
            "even": 4,
            "nonneg": 3,
            "multi": 2,
        },
        {"email": "a@b.com"},  # valid subset
        {},
    ],
)
def test_load_validator_arm_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(ValidatorArmSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


_ma3_only = pytest.mark.skipif(
    _MA_MAJOR >= 4,
    reason="a plain callable returning False only fails on marshmallow 3.x",
)


@pytest.mark.parametrize(
    "data",
    [
        {"email": "not-an-email"},
        {"url": "not a url"},
        {"rx": "ABC"},  # regexp fail
        {"even": 3},  # custom raise
        # A plain callable returning False fails on 3.x but is ignored on 4.x.
        pytest.param({"nonneg": -1}, marks=_ma3_only),
        {"multi": -3},  # native Range fail AND python fail -> both collected
        {"multi": 3},  # native pass, python fail
    ],
)
def test_load_validator_arm_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        ValidatorArmSchema().load(data)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        ValidatorArmSchema().load(data)
    assert acc_exc.value.messages == py_exc.value.messages


# ---- typed Dict with inner key/value validators --------------------------


class DictValidatedSchema(Schema):
    d = fields.Dict(
        keys=fields.String(validate=validate.Length(min=2)),
        values=fields.Integer(validate=validate.Range(min=0)),
    )


def test_dict_inner_validators_is_native():
    from marshmallow_core import _compiler

    field = DictValidatedSchema().load_fields["d"]
    assert _compiler._build_load_element(field, ())[0] == _compiler._L_DICT_TYPED


@pytest.mark.parametrize("data", [{"d": {"ab": 1, "cd": 2}}, {"d": {}}, {}])
def test_load_dict_inner_validators_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(DictValidatedSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "data",
    [
        {"d": {"x": 1}},  # key too short
        {"d": {"ab": -1}},  # value below range
        {"d": {"x": -1}},  # both fail
    ],
)
def test_load_dict_inner_validators_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        DictValidatedSchema().load(data)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        DictValidatedSchema().load(data)
    assert acc_exc.value.messages == py_exc.value.messages


# ---- IP family (ipaddress-backed fields) ---------------------------------

import ipaddress as _ipaddress


class IpSchema(Schema):
    a = fields.IP()
    b = fields.IPv4()
    c = fields.IPv6()
    iface = fields.IPInterface()
    v4i = fields.IPv4Interface()
    v6i = fields.IPv6Interface()


_IP_LOADED = {
    "a": _ipaddress.ip_address("1.2.3.4"),
    "b": _ipaddress.IPv4Address("8.8.8.8"),
    "c": _ipaddress.IPv6Address("::1"),
    "iface": _ipaddress.ip_interface("10.0.0.1/24"),
    "v4i": _ipaddress.IPv4Interface("192.168.0.5/16"),
    "v6i": _ipaddress.IPv6Interface("fe80::1/64"),
}


def test_dump_ip_equivalence(monkeypatch):
    accelerated, pure = _dump_both(IpSchema, _IP_LOADED, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize(
    "data",
    [
        {
            "a": "1.2.3.4",
            "b": "8.8.8.8",
            "c": "::1",
            "iface": "10.0.0.1/24",
            "v4i": "192.168.0.5/16",
            "v6i": "fe80::1/64",
        },
        {"a": _ipaddress.ip_address("5.6.7.8")},  # already an instance -> passthrough
        {},  # all missing
    ],
)
def test_load_ip_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(IpSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize("data", [{"a": "not-an-ip"}, {"b": "::1"}, {"a": None}])
def test_load_ip_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        IpSchema().load(data)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        IpSchema().load(data)
    assert acc_exc.value.messages == py_exc.value.messages


def test_loads_ip_equivalence(monkeypatch):
    payload = '{"a": "1.2.3.4", "c": "::1", "iface": "10.0.0.1/24"}'
    fused, pure = _loads_both(IpSchema, payload, monkeypatch=monkeypatch)
    assert fused == pure


# ---- custom (non-ISO) strptime temporal formats on load ------------------


class CustomTemporalSchema(Schema):
    when = fields.DateTime(format="%Y/%m/%d %H:%M")
    day = fields.Date(format="%d-%m-%Y")
    t = fields.Time(format="%H.%M.%S")


def test_custom_temporal_is_native_load():
    """A custom strptime format must compile to the native (held-method) load
    element, not fall back."""
    from marshmallow_core import _compiler

    field = fields.DateTime(format="%Y/%m/%d %H:%M")
    field._bind_to_schema("when", CustomTemporalSchema())
    assert (
        _compiler._build_load_element(field, ())[0] == _compiler._L_DATETIME_AWARENESS
    )


@pytest.mark.parametrize(
    "data",
    [
        {"when": "2020/01/02 03:04", "day": "15-06-2026", "t": "23.59.01"},
        {"day": "15-06-2026"},  # subset
        {},  # all missing
    ],
)
def test_load_custom_temporal_equivalence(data, monkeypatch):
    accelerated, pure = _load_both(CustomTemporalSchema, data, monkeypatch=monkeypatch)
    assert accelerated == pure


@pytest.mark.parametrize("data", [{"when": "bad-format"}, {"day": "2026-06-15"}])
def test_load_custom_temporal_errors_match_python(data, monkeypatch):
    with pytest.raises(ValidationError) as acc_exc:
        CustomTemporalSchema().load(data)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as py_exc:
        CustomTemporalSchema().load(data)
    assert acc_exc.value.messages == py_exc.value.messages


# ---- fused loads (jiter, Design A) ---------------------------------------
# ``loads`` parses JSON straight off a jiter tree and deserializes without the
# intermediate Python dict ``json.loads`` would build. These mirror the
# ``_load_both`` pattern: run the fused path, then force the stock
# ``json.loads`` + pure-Python load and assert the two agree.


def _loads_both(schema_factory, json_str, *, many=False, monkeypatch, **kwargs):
    """Return (fused, pure) ``loads`` of ``json_str``."""
    fused = schema_factory().loads(json_str, many=many, **kwargs)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    pure = schema_factory().loads(json_str, many=many, **kwargs)
    return fused, pure


class LoadsFlat(Schema):
    i = fields.Integer()
    f = fields.Float()
    s = fields.String()
    b = fields.Boolean()
    r = fields.Raw()


@pytest.mark.parametrize(
    "payload",
    [
        '{"i": 1, "f": 2.5, "s": "x", "b": true, "r": [1, 2, null]}',
        '{"i": 1}',  # missing fields -> no default applied
        '{"i": 1, "i": 2}',  # duplicate key -> last wins (== json.loads)
        '{"i": 123456789012345678901234567890}',  # big int -> jiter parse fails, fallback
        '{"f": 1.1234567890123456}',  # float round-trip
        "{}",  # empty object
    ],
)
def test_loads_flat_equivalence(payload, monkeypatch):
    fused, pure = _loads_both(LoadsFlat, payload, monkeypatch=monkeypatch)
    assert fused == pure


def test_loads_many_equivalence(monkeypatch):
    fused, pure = _loads_both(
        LoadsFlat, '[{"i": 1}, {"i": 2}, {"i": 3}]', many=True, monkeypatch=monkeypatch
    )
    assert fused == pure


def test_loads_partial_equivalence(monkeypatch):
    fused, pure = _loads_both(
        LoadsFlat, '{"i": 1}', partial=True, monkeypatch=monkeypatch
    )
    assert fused == pure


class _LoadsInner(Schema):
    x = fields.Integer()
    y = fields.String()


class LoadsNested(Schema):
    items = fields.List(fields.Nested(_LoadsInner))  # list-of-records: the win case
    tags = fields.List(fields.String())
    total = fields.Integer()


def test_loads_nested_list_equivalence(monkeypatch):
    payload = (
        '{"items": [{"x": 1, "y": "a"}, {"x": 2, "y": "b"}],'
        ' "tags": ["p", "q"], "total": 2}'
    )
    fused, pure = _loads_both(LoadsNested, payload, monkeypatch=monkeypatch)
    assert fused == pure


def test_loads_nested_post_load_equivalence(monkeypatch):
    """Fused JSON loads path through a NestedPostLoad element."""
    payload = '{"name": "Bob", "inner": {"x": 5, "y": "hi"}}'
    fused, pure = _loads_both(_PostLoadOuter, payload, monkeypatch=monkeypatch)
    assert fused == pure
    assert isinstance(fused["inner"], Obj)


class LoadsContainers(Schema):
    # Dict / typed-Dict / Tuple threaded straight off the jiter tree.
    rows = fields.List(
        fields.Dict(
            keys=fields.String(), values=fields.Integer(validate=validate.Range(min=0))
        )
    )
    pairs = fields.List(fields.Tuple((fields.Integer(), fields.String())))
    plain = fields.Dict()


@pytest.mark.parametrize(
    "payload",
    [
        '{"rows": [{"a": 1, "b": 2}, {"c": 3}], "pairs": [[1, "x"], [2, "y"]], "plain": {"k": [1, 2]}}',
        '{"rows": [{"a": 1, "a": 5}]}',  # duplicate key -> last wins
        '{"plain": {}}',
        "{}",
    ],
)
def test_loads_threaded_containers_equivalence(payload, monkeypatch):
    fused, pure = _loads_both(LoadsContainers, payload, monkeypatch=monkeypatch)
    assert fused == pure


@pytest.mark.parametrize(
    "payload",
    [
        '{"rows": [{"a": -1}]}',  # value validator fail
        '{"pairs": [[1, "x", 9]]}',  # tuple length mismatch
        '{"pairs": [[1, 2]]}',  # tuple element wrong type
    ],
)
def test_loads_threaded_containers_errors_match_python(payload, monkeypatch):
    with pytest.raises(ValidationError) as fused_exc:
        LoadsContainers().loads(payload)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as pure_exc:
        LoadsContainers().loads(payload)
    assert fused_exc.value.messages == pure_exc.value.messages


class LoadsInclude(Schema):
    class Meta:
        unknown = INCLUDE

    i = fields.Integer()


class LoadsExclude(Schema):
    class Meta:
        unknown = EXCLUDE

    i = fields.Integer()


@pytest.mark.parametrize("factory", [LoadsInclude, LoadsExclude])
def test_loads_unknown_equivalence(factory, monkeypatch):
    fused, pure = _loads_both(
        factory,
        '{"i": 1, "extra": "yo", "n": 3, "extra": "last"}',
        monkeypatch=monkeypatch,
    )
    assert fused == pure


def test_loads_wide_schema_equivalence(monkeypatch):
    """A wide schema is the case the single-pass loader fixed (was O(fields^2));
    confirm correctness still holds across many fields and a list of records."""
    wide = type("Wide50", (Schema,), {f"f{n}": fields.Integer() for n in range(50)})
    record = {f"f{n}": n for n in range(50)}
    body = "{" + ", ".join(f'"f{n}": {n}' for n in range(50)) + "}"
    payload = f"[{body}, {body}]"
    fused, pure = _loads_both(wide, payload, many=True, monkeypatch=monkeypatch)
    assert fused == pure
    assert fused == [record, record]


def test_loads_include_unknown_collides_with_out_key(monkeypatch):
    """An INCLUDE'd unknown key whose name equals a field's output key must win
    over the field's value — stock applies unknown keys after the fields, which
    the single-pass loader reproduces by flushing them in a final pass."""

    class S(Schema):
        class Meta:
            unknown = INCLUDE

        # reads "src", writes "dest"; an unknown input key "dest" then collides.
        x = fields.Integer(data_key="src", attribute="dest")

    fused, pure = _loads_both(
        S, '{"src": 1, "dest": "unknown-wins"}', monkeypatch=monkeypatch
    )
    assert fused == pure
    assert fused == {"dest": "unknown-wins"}


def test_loads_bytes_input_equivalence(monkeypatch):
    fused, pure = _loads_both(
        LoadsFlat, b'{"i": 7, "s": "hi"}', monkeypatch=monkeypatch
    )
    assert fused == pure


def test_loads_fusable_flag_tracks_callback_fields(monkeypatch):
    """A callback field anywhere makes the schema non-fusable (transitively
    through Nested); ``_patched_loads`` reads this to skip a doomed jiter parse
    and go straight to stock ``loads`` (ARCH.md B2). The plan caches the flag."""

    class AllNative(Schema):
        a = fields.Integer()
        b = fields.String()

    class WithCallback(Schema):
        a = fields.Integer()
        c = fields.Function(deserialize=lambda v: v)  # forces the callback path

    class NestedCallback(Schema):
        inner = fields.Nested(WithCallback)

    # The ``fusable`` flag is a core-only construct: with the extension disabled
    # (``MARSHMALLOW_NO_ACCEL``) ``build_load_deserializer`` returns ``None``.
    # Guard those assertions on availability; the equivalence below holds either way.
    if accel.is_available():
        assert accel.build_load_deserializer(AllNative()).fusable is True
        assert accel.build_load_deserializer(WithCallback()).fusable is False
        assert accel.build_load_deserializer(NestedCallback()).fusable is False

        # The cached plan carries the flag as its 3rd element (index 2).
        # (R4 removed the stale ``default_core_partial`` slot from the tuple,
        # shifting ``fusable`` from index 3 to index 2.)
        schema = WithCallback()
        schema.loads('{"a": 1, "c": 5}')
        assert vars(schema)["_mc_load_plan"][2] is False

    # The non-fusable schema still loads correctly via the stock path.
    fused, pure = _loads_both(WithCallback, '{"a": 1, "c": 5}', monkeypatch=monkeypatch)
    assert fused == pure


@pytest.mark.parametrize(
    "payload",
    [
        '{"i": null}',  # null into a non-allow_none field
        '{"i": 1, "zzz": 9}',  # unknown key under the default RAISE
        '{"f": NaN}',  # NaN rejected by Float (special)
        '{"i": "abc"}',  # invalid int
        '{"s": 123}',  # number into String -> "Not a valid string."
    ],
)
def test_loads_errors_match_python(payload, monkeypatch):
    with pytest.raises(ValidationError) as fused_exc:
        LoadsFlat().loads(payload)
    monkeypatch.setattr(accel, "build_load_deserializer", lambda schema: None)
    with pytest.raises(ValidationError) as pure_exc:
        LoadsFlat().loads(payload)
    assert fused_exc.value.messages == pure_exc.value.messages


def test_loads_malformed_json_defers(monkeypatch):
    """Malformed JSON: jiter fails -> fallback -> stock ``render_module.loads``
    raises the same ``JSONDecodeError`` (which marshmallow does not wrap)."""
    import json as _json

    with pytest.raises(_json.JSONDecodeError):
        LoadsFlat().loads('{"i": 1,')


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
    assert vars(schema).get("_mc_load_plan") is not None


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
