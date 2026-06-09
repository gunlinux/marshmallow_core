"""Representative schemas + sample data shared by the benchmark and the probe.

Four shapes chosen to exercise different parts of the core:

* ``flat``      — all-native scalar fields, the best case for the dump/load core.
* ``nested``    — a schema with a nested object and a couple of nested lists.
* ``list``      — a list of nested objects (list-heavy), the common "collection
  of records" payload.
* ``validator`` — fields carrying ``Range``/``Length``/``OneOf`` validators,
  which (until Phase 1 lands) force the load path onto Python callbacks.
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


#: name -> (schema_class, sample_dict). The sample is the *loaded* form for dump
#: and the input form for load; for these schemas the two coincide closely
#: enough that the same dict drives both directions.
CASES: dict[str, tuple[type[Schema], dict]] = {
    "flat": (FlatSchema, _flat_obj()),
    "nested": (NestedSchema, _nested_obj()),
    "list": (ListSchema, _list_obj()),
    "validator": (ValidatorSchema, _validator_obj()),
    "hooks": (HookSchema, _hook_obj()),
    "api": (ApiSchema, _api_obj()),
}
