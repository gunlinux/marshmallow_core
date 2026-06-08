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

from marshmallow import Schema, fields, validate

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


#: name -> (schema_class, sample_dict). The sample is the *loaded* form for dump
#: and the input form for load; for these schemas the two coincide closely
#: enough that the same dict drives both directions.
CASES: dict[str, tuple[type[Schema], dict]] = {
    "flat": (FlatSchema, _flat_obj()),
    "nested": (NestedSchema, _nested_obj()),
    "list": (ListSchema, _list_obj()),
    "validator": (ValidatorSchema, _validator_obj()),
}
