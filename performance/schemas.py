"""Representative schemas + sample data shared by the benchmark and the probe.

Shapes chosen to exercise different parts of the core:

* ``flat``       — all-native scalar fields, the best case for the dump/load core.
* ``nested``     — a schema with a nested object and a couple of nested lists.
* ``list``       — a list of nested objects (list-heavy), the common "collection
  of records" payload.
* ``validator``  — fields carrying ``Range``/``Length``/``OneOf`` validators.
* ``hooks``      — schema-level ``pre_load``/``post_load``/``validates`` hooks.
* ``api``        — realistic mixed-payload shape.
* ``non_ascii``  — string fields with non-ASCII (Cyrillic) content; exercises
  the ``dumps`` JSON escaper on non-ASCII runs (F_SPEEDUP F1/F8).
* ``obj_source`` — object (non-dict) as dump source; exercises the type-level
  indexability cache (F_SPEEDUP F3).
* ``partial``    — flat schema loaded with ``partial=<all field names>``; the
  benchmark case for the ``consumes_partial`` optimisation (F_SPEEDUP F4).
"""

from __future__ import annotations

import datetime as dt

from marshmallow import Schema, fields, post_load, pre_load, validate, validates

# ---- flat scalars ----------------------------------------------------------


class FlatSchema(Schema):
    i = fields.Integer()
    f = fields.Float()
    s = fields.String()
    b = fields.Boolean()
    when = fields.DateTime()


def _flat_obj() -> dict:
    return {
        "i": 42,
        "f": 3.14159,
        "s": "the quick brown fox",
        "b": True,
        "when": dt.datetime(2020, 1, 2, 3, 4, 5),
    }


# ---- nested ----------------------------------------------------------------


class Coord(Schema):
    lat = fields.Float()
    lng = fields.Float()


class Address(Schema):
    city = fields.String()
    zip = fields.String()
    coordinates = fields.Nested(Coord)


class NestedSchema(Schema):
    name = fields.String()
    age = fields.Integer()
    address = fields.Nested(Address)
    tags = fields.List(fields.String())
    scores = fields.List(fields.Integer())


def _nested_obj() -> dict:
    return {
        "name": "Foo Bar",
        "age": 30,
        "address": {
            "city": "Springfield",
            "zip": "12345",
            "coordinates": {"lat": 1.5, "lng": 2.5},
        },
        "tags": ["a", "b", "c", "d"],
        "scores": [1, 2, 3, 4, 5],
    }


# ---- list-heavy ------------------------------------------------------------


class Record(Schema):
    id = fields.Integer()
    name = fields.String()
    active = fields.Boolean()
    score = fields.Float()


class ListSchema(Schema):
    records = fields.List(fields.Nested(Record))
    total = fields.Integer()


def _list_obj() -> dict:
    return {
        "records": [
            {"id": n, "name": f"record-{n}", "active": bool(n % 2), "score": n * 1.5}
            for n in range(50)
        ],
        "total": 50,
    }


# ---- validator-heavy -------------------------------------------------------


class ValidatorSchema(Schema):
    age = fields.Integer(validate=validate.Range(min=0, max=150))
    name = fields.String(validate=validate.Length(min=1, max=64))
    role = fields.String(validate=validate.OneOf(["admin", "user", "guest"]))
    score = fields.Float(validate=validate.Range(min=0.0, max=100.0))


def _validator_obj() -> dict:
    return {"age": 30, "name": "Foo Bar", "role": "user", "score": 88.5}


# ---- hook-bearing (pre_load / post_load / validates) -----------------------


class HookSchema(Schema):
    a = fields.Integer()
    b = fields.String()
    c = fields.Float()

    @pre_load
    def _pre(self, data, **kwargs):
        return data

    @post_load
    def _post(self, data, **kwargs):
        return data

    @validates("a")
    def _check_a(self, value, **kwargs):
        if value < 0:
            raise ValueError("negative")


def _hook_obj() -> dict:
    return {"a": 5, "b": "hello world", "c": 2.5}


# ---- API-response-shaped (realistic mixed payload) -------------------------
# A paginated list of records mixing every common native field type — bool,
# str, int, float, datetime, a list of scalars and a nested object — closer to
# a real JSON API response than the single-type synthetic shapes above. This is
# the case to watch for real-world regressions.


class _Profile(Schema):
    bio = fields.String()
    verified = fields.Boolean()


class _User(Schema):
    id = fields.Integer(strict=True)
    name = fields.String()
    email = fields.String()
    active = fields.Boolean()
    score = fields.Float()
    created = fields.DateTime()
    tags = fields.List(fields.String())
    profile = fields.Nested(_Profile)


class ApiSchema(Schema):
    page = fields.Integer()
    total = fields.Integer()
    users = fields.List(fields.Nested(_User))


def _api_obj() -> dict:
    return {
        "page": 1,
        "total": 25,
        "users": [
            {
                "id": n,
                "name": f"user-{n}",
                "email": f"user{n}@example.com",
                "active": bool(n % 2),
                "score": n * 1.25,
                "created": dt.datetime(2020, 1, 2, 3, 4, 5),
                "tags": ["alpha", "beta", "gamma"],
                "profile": {"bio": f"bio for {n}", "verified": n % 3 == 0},
            }
            for n in range(25)
        ],
    }


# ---- non-ASCII string dump/loads -------------------------------------------
# Two long Cyrillic strings; exercises the JSON escaper's non-ASCII code path
# (F_SPEEDUP F1/F8). A regression here means the escaper fell back to ``write!``.

_CYRILLIC = "Привет мир это тест строка для бенчмарка производительности кириллица"


class NonAsciiSchema(Schema):
    a = fields.String()
    b = fields.String()


def _non_ascii_obj() -> dict:
    return {"a": _CYRILLIC, "b": _CYRILLIC}


# ---- object (non-dict) dump source -----------------------------------------
# An attribute-bearing Python object rather than a dict; exercises the
# ``get_one`` type-level indexability cache (F_SPEEDUP F3). A regression here
# means the per-field ``hasattr("__getitem__")`` probe is being called N×M times.


class _ObjRec:
    def __init__(self) -> None:
        self.i = 42
        self.f = 3.14
        self.s = "hello"
        self.b = True
        self.when = dt.datetime(2020, 1, 2, 3, 4, 5)


class ObjSourceSchema(Schema):
    i = fields.Integer()
    f = fields.Float()
    s = fields.String()
    b = fields.Boolean()
    when = fields.DateTime()


_OBJ_RECORD = _ObjRec()


def _obj_source_obj() -> _ObjRec:
    return _OBJ_RECORD


# ---- partial=<all field names> load ----------------------------------------
# A flat 10-field int schema loaded with all field names in ``partial``; the
# expected case that motivated the ``consumes_partial`` flag (F_SPEEDUP F4).
# A regression here means ``partial.derive`` is called per field per record.

_PartialFields = {f"f{i}": fields.Integer() for i in range(10)}
PartialSchema = type("PartialSchema", (Schema,), {**_PartialFields})

_PARTIAL_NAMES = [f"f{i}" for i in range(10)]
_PARTIAL_RECORDS = [{f"f{i}": i for i in range(10)} for _ in range(50)]


def _partial_obj() -> list:
    return _PARTIAL_RECORDS


#: name -> (schema_class, sample). The sample is the *loaded* form for dump
#: and the input form for load; for these schemas the two coincide closely
#: enough that the same object drives both directions.
CASES: dict[str, tuple[type[Schema], object]] = {
    "flat": (FlatSchema, _flat_obj()),
    "nested": (NestedSchema, _nested_obj()),
    "list": (ListSchema, _list_obj()),
    "validator": (ValidatorSchema, _validator_obj()),
    "hooks": (HookSchema, _hook_obj()),
    "api": (ApiSchema, _api_obj()),
    "non_ascii": (NonAsciiSchema, _non_ascii_obj()),
    "obj_source": (ObjSourceSchema, _obj_source_obj()),
}
