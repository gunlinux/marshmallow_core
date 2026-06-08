"""Smoke tests: install/uninstall and accel-on == accel-off equivalence."""

from __future__ import annotations

import marshmallow as ma
import pytest

import marshmallow_core


@pytest.fixture
def core():
    """Install the core for the test and always uninstall afterwards."""
    marshmallow_core.install()
    try:
        yield
    finally:
        marshmallow_core.uninstall()


class Person(ma.Schema):
    name = ma.fields.String()
    age = ma.fields.Integer()
    tags = ma.fields.List(ma.fields.String())


def test_install_is_idempotent_and_reversible():
    assert not marshmallow_core.is_installed()
    marshmallow_core.install()
    marshmallow_core.install()  # idempotent
    assert marshmallow_core.is_installed()
    marshmallow_core.uninstall()
    marshmallow_core.uninstall()  # idempotent
    assert not marshmallow_core.is_installed()


def test_is_available_returns_bool():
    assert isinstance(marshmallow_core.is_available(), bool)


def test_dump_matches_pure_python(core):
    obj = {"name": "ann", "age": 30, "tags": ["a", "b"]}
    marshmallow_core.uninstall()
    expected = Person().dump(obj)
    marshmallow_core.install()
    assert Person().dump(obj) == expected


def test_load_matches_pure_python(core):
    data = {"name": "ann", "age": "30", "tags": ["a", "b"]}
    marshmallow_core.uninstall()
    expected = Person().load(data)
    marshmallow_core.install()
    assert Person().load(data) == expected


def test_load_error_falls_back_to_python(core):
    # A coercion failure must raise the same ValidationError as pure Python.
    with pytest.raises(ma.ValidationError) as exc:
        Person().load({"name": "ann", "age": "not-a-number"})
    assert "age" in exc.value.messages
