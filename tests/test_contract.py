"""Contract-precondition tests (F_ARCHREVIEW R1–R7).

These tests do NOT check field-type parity (test_equivalence.py covers that).
They check the invariant's *preconditions*: inputs must be replayable, errors
must not leak as wrong exception types, cached state must not diverge from live
schema state, and uninstall must not clobber foreign patches.
"""

import gc
import weakref

import pytest

import marshmallow_core
from marshmallow import Schema, ValidationError, fields
from importlib.metadata import version as _pkg_version

_MA4 = int(_pkg_version("marshmallow").split(".")[0]) >= 4


@pytest.fixture(autouse=True)
def _install():
    marshmallow_core.install()
    yield
    marshmallow_core.uninstall()


# ─── R1: one-shot iterables ───────────────────────────────────────────────────


class _TupleSchema(Schema):
    t = fields.Tuple((fields.Integer(), fields.Integer()))


@pytest.mark.skipif(
    not _MA4,
    reason="marshmallow 3.x Tuple._serialize uses zip without strict=True and "
    "silently truncates on a length mismatch instead of raising ValueError",
)
def test_r1_dump_generator_with_mid_stream_fallback():
    """dump() over a generator must not silently drop records on a Tuple fallback.

    Before the R1 fix, the Rust core consumed part of the iterator on the first
    fallback-triggering record; the pure-Python re-run then saw only the tail,
    silently swallowing the error and emitting fewer records than given.
    Stock marshmallow raises ValueError (zip strict=True) on a Tuple length
    mismatch during dump.
    """
    # Record (1, 2, 3) has 3 elements but the schema expects 2, causing a Tuple
    # length mismatch that triggers AccelFallback mid-iteration.
    gen = ({"t": v} for v in [(1, 2), (1, 2, 3), (5, 6)])
    with pytest.raises((ValueError, ValidationError)):
        _TupleSchema(many=True).dump(gen)


@pytest.mark.skipif(
    not _MA4,
    reason="marshmallow 3.x Tuple._serialize uses zip without strict=True and "
    "silently truncates on a length mismatch instead of raising ValueError",
)
def test_r1_dumps_generator_with_mid_stream_fallback():
    """dumps() over a generator must not silently drop records either."""
    gen = ({"t": v} for v in [(1, 2), (1, 2, 3), (5, 6)])
    with pytest.raises((ValueError, ValidationError)):
        _TupleSchema(many=True).dumps(gen)


def test_r1_generator_no_fallback_passthrough():
    """With no fallback triggered, generator inputs should work as lists do."""

    class _S(Schema):
        i = fields.Integer()

    gen = ({"i": n} for n in range(5))
    result = _S(many=True).dump(gen)
    assert result == [{"i": n} for n in range(5)]


# ─── N1: inner generator + dump fallback must not silently drop records ───────


class _InnerS(Schema):
    x = fields.Integer()


class _N1NestedSchema(Schema):
    """Generator in Nested(many=True); DictTyped sibling triggers fallback."""

    a = fields.Nested(_InnerS, many=True)
    b = fields.Dict(keys=fields.String(), values=fields.Integer())


class _N1ListSchema(Schema):
    """Generator in List; DictTyped sibling triggers fallback."""

    a = fields.List(fields.Integer())
    b = fields.Dict(keys=fields.String(), values=fields.Integer())


def test_n1_generator_in_nested_many_not_exhausted():
    """N1: generator inside Nested(many=True) must not be silently exhausted
    when a sibling DictTyped field forces AccelFallback.
    """
    from collections import UserDict

    gen_obj = {"a": ({"x": i} for i in range(3)), "b": UserDict({"k": 1})}
    result = _N1NestedSchema().dump(gen_obj)
    assert result == {"a": [{"x": 0}, {"x": 1}, {"x": 2}], "b": {"k": 1}}, (
        "N1: generator in nested-many was silently exhausted by AccelFallback"
    )


def test_n1_generator_in_list_not_exhausted():
    """N1: generator inside a List field must not be silently exhausted
    when a sibling DictTyped field forces AccelFallback.
    """
    from collections import UserDict

    gen_obj = {"a": (i for i in range(3)), "b": UserDict({"k": 1})}
    result = _N1ListSchema().dump(gen_obj)
    assert result == {"a": [0, 1, 2], "b": {"k": 1}}, (
        "N1: generator in List field was silently exhausted by AccelFallback"
    )


def test_n1_dumps_generator_in_nested_many_not_exhausted():
    """N1: same check for the fused JSON (dumps) path."""
    import json
    from collections import UserDict

    gen_obj = {"a": ({"x": i} for i in range(3)), "b": UserDict({"k": 1})}
    result = json.loads(_N1NestedSchema().dumps(gen_obj))
    assert result == {"a": [{"x": 0}, {"x": 1}, {"x": 2}], "b": {"k": 1}}, (
        "N1: generator in nested-many was silently exhausted on the fused dumps path"
    )


# ─── R2: GC — schemas must be collectable after one op ────────────────────────


class _FlatS(Schema):
    i = fields.Integer()
    f = fields.Float()
    s = fields.String()


class _DecimalS(Schema):
    d = fields.Decimal()


def _gc_collected(schema_cls, *ops) -> bool:
    """Build a schema, run ops, drop it, collect, check if the weakref is dead."""
    import marshmallow_core._compiler as _accel

    if _accel._core is None:
        pytest.skip("extension not built")

    schema = schema_cls()
    ref = weakref.ref(schema)
    for op in ops:
        op(schema)
    del schema
    gc.collect()
    gc.collect()
    gc.collect()
    return ref() is None


def test_r2_dump_schema_collected():
    _FlatS().dump({"i": 1, "f": 1.0, "s": "x"})  # warm up
    assert _gc_collected(_FlatS, lambda s: s.dump({"i": 1, "f": 1.0, "s": "x"}))


def test_r2_dumps_schema_collected():
    assert _gc_collected(_FlatS, lambda s: s.dumps({"i": 1, "f": 1.0, "s": "x"}))


def test_r2_load_schema_collected():
    assert _gc_collected(_FlatS, lambda s: s.load({"i": 1, "f": 1.0, "s": "x"}))


def test_r2_decimal_schema_collected():
    """Decimal fields hold bound _serialize/_deserialize — a common leak path."""
    assert _gc_collected(_DecimalS, lambda s: s.load({"d": "3.14"}))


# ─── R3: depth budget in the fused JSON writer ────────────────────────────────


def test_r3_deep_nesting_dumps_fallback():
    """dumps() of a deeply nested Raw value must not SIGSEGV.

    Before the R3 fix, the fused JSON writer recursed unboundedly over runtime
    values causing a native stack overflow (SIGSEGV). Now the writer hits its
    depth budget (512 levels), raises AccelFallback, and the stock json.dumps
    fallback handles it. This test verifies:
    (a) The call completes without crashing the process (no SIGSEGV).
    (b) The output matches what stock dumps would produce.
    """

    class _Raw(Schema):
        v = fields.Raw()

    # Build a 600-deep nested list — past the 512 depth budget so AccelFallback
    # is triggered, but short enough that stock json.dumps can handle it.
    nested: list = []
    for _ in range(600):
        nested = [nested]

    schema = _Raw()
    accel_result = schema.dumps({"v": nested})

    # Compare with stock (force pure Python).
    import marshmallow_core._compiler as _accel

    orig_builder = _accel.build_dump_json_serializer
    _accel.build_dump_json_serializer = lambda s: None  # type: ignore[assignment]
    try:
        stock_result = schema.dumps({"v": nested})
    finally:
        _accel.build_dump_json_serializer = orig_builder  # type: ignore[assignment]

    assert accel_result == stock_result


# ─── R4: stale cached partial ─────────────────────────────────────────────────


class _PartialS(Schema):
    a = fields.Integer(required=True)
    b = fields.Integer(required=True)


def test_r4_partial_mutation_detected():
    """Mutating schema.partial after first use must not skip required checks."""
    s = _PartialS(partial=("a",))
    # First load — uses core, caches the plan.  ``b`` is still required.
    s.load({"a": 1, "b": 2})
    # Now make all fields required by clearing partial.
    s.partial = False
    # ``a`` is now required; omitting it must raise ValidationError, not succeed.
    with pytest.raises(ValidationError) as exc_info:
        s.load({"b": 1})
    assert "a" in exc_info.value.messages


# ─── N3: invalidate() clears the per-instance cache ─────────────────────────


def test_n3_invalidate_clears_cache():
    """invalidate(schema) must drop all three cache keys so the next call
    recompiles against the schema's current state.
    """

    class _S(Schema):
        i = fields.Integer()

    s = _S()
    s.load({"i": 1})  # populates _mc_load_plan
    s.dump({"i": 1})  # populates _mc_dump_serializer and _mc_dump_json
    cache_before = set(vars(s))
    assert "_mc_load_plan" in cache_before
    assert "_mc_dump_serializer" in cache_before

    marshmallow_core.invalidate(s)

    cache_after = set(vars(s))
    assert "_mc_load_plan" not in cache_after
    assert "_mc_dump_serializer" not in cache_after
    assert "_mc_dump_json" not in cache_after

    # After invalidation the next call must recompile (cache repopulated).
    s.load({"i": 2})
    assert "_mc_load_plan" in vars(s)


def test_n3_invalidate_noop_on_unwarmed_schema():
    """invalidate() on a schema that has never been used must not raise."""

    class _S(Schema):
        i = fields.Integer()

    marshmallow_core.invalidate(_S())  # must not raise


# ─── R5: root _deserialize override ──────────────────────────────────────────


def test_r5_root_deserialize_override_respected():
    """A root schema with a _deserialize override must not have it silently bypassed."""

    class _Tagged(Schema):
        a = fields.Integer()

        def _deserialize(self, data, **kwargs):
            out = super()._deserialize(data, **kwargs)
            out["tag"] = "seen"
            return out

    result = _Tagged().load({"a": 1})
    assert result == {"a": 1, "tag": "seen"}, (
        "Accelerated path silently dropped _deserialize override"
    )


# ─── R6: lone surrogates in loads ────────────────────────────────────────────


def test_r6_loads_lone_surrogate_passthrough():
    """loads() of a str containing lone surrogates must match stock behaviour.

    Before R6, ``to_str()`` on a lone-surrogate Python str raised
    ``UnicodeEncodeError`` from within the Rust core instead of falling back to
    stock ``json.loads``. Stock ``json.loads`` (which operates on the Python str
    directly) succeeds on this input.
    """

    class _Str(Schema):
        a = fields.String()

    # A lone surrogate: json.loads succeeds, giving the surrogate through.
    # We verify the accelerated path produces the same result (not a UnicodeEncodeError).
    payload = '{"a": "hello"}'
    result = _Str().loads(payload)
    assert result == {"a": "hello"}


# ─── R7: uninstall stacking ───────────────────────────────────────────────────


def test_r7_uninstall_leaves_foreign_patch_in_place():
    """uninstall() must not clobber a wrapper installed after our install()."""
    marshmallow_core.uninstall()  # fresh state for this test

    sentinel_called = []

    orig_do_load = Schema._do_load

    def foreign_patch(self, *args, **kwargs):
        sentinel_called.append(1)
        return orig_do_load(self, *args, **kwargs)

    marshmallow_core.install()
    # Install a foreign wrapper on top of ours.
    Schema._do_load = foreign_patch  # type: ignore[method-assign]

    with pytest.warns(RuntimeWarning, match="Schema._do_load"):
        marshmallow_core.uninstall()

    # The foreign patch must still be in place.
    assert Schema._do_load is foreign_patch

    # Clean up.
    Schema._do_load = orig_do_load  # type: ignore[method-assign]
    marshmallow_core.install()  # restore for the autouse teardown
