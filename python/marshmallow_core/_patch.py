"""Monkey-patch stock marshmallow's :class:`~marshmallow.Schema` to use the core.

:func:`install` swaps ``Schema._serialize`` and ``Schema._do_load`` for wrappers
that try the compiled Rust core and otherwise call the saved pure-Python
originals. :func:`uninstall` restores them. Patching is process-wide and
idempotent.

The compiled serializer/deserializer is cached per *instance* (on
``schema.__dict__``), so building it happens once per schema. A schema that the
core cannot model caches ``None`` and always uses the original method.

Schemas with ``pre_load``/``post_load``/``validates``/``validates_schema`` hooks
are accelerated too, reproducing marshmallow's own split: ``pre_load`` runs in
Python, the core does the per-field deserialize step (the body of
``Schema._deserialize``), then field/schema validators and ``post_load`` run in
Python around it. :func:`_accelerated_load` is a verbatim copy of
``Schema._do_load`` with the one ``self._deserialize(...)`` call replaced by the
compiled core; it therefore pins to marshmallow's ``_do_load`` internals (it
tracks both the 3.x and 4.x shapes, branching on :data:`_MA4`) and leans on the
equivalence suite. To bound that coupling, :func:`install` runs
:func:`_accel_load_supported` — a structural check of the private invokers the
transcription calls — and on a mismatch (an untested marshmallow) routes
hook-bearing schemas to the pure-Python load instead. The core still raises
``AccelFallback`` for any per-field edge case, so the whole load re-runs the
unchanged pure-Python path.
"""

from __future__ import annotations

import inspect
import json
import typing
import weakref

from marshmallow import Schema, ValidationError
from marshmallow.decorators import (
    POST_DUMP,
    POST_LOAD,
    PRE_DUMP,
    PRE_LOAD,
    VALIDATES,
    VALIDATES_SCHEMA,
)
from marshmallow.error_store import ErrorStore

from marshmallow_core import _compiler

if typing.TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Sentinel: "not yet built". A built-but-not-compilable schema caches ``None``.
_UNSET: typing.Final = object()

#: Schema-level load hooks; their presence routes load through the hook-aware
#: accelerated path (:func:`_accelerated_load`) instead of the direct core call.
_LOAD_HOOKS = (PRE_LOAD, POST_LOAD, VALIDATES, VALIDATES_SCHEMA)

#: Collection ``partial`` types the core models natively (mirrors marshmallow's
#: ``is_collection``: an iterable of field names, excluding strings).
_PARTIAL_COLLECTIONS = (list, tuple, set, frozenset)


def _schema_validator_params() -> frozenset[str]:
    """Parameter names of ``Schema._invoke_schema_validators`` (empty if absent).

    Used both to detect the 4.x rename below and by :func:`_accel_load_supported`.
    Defensive (``getattr``/``try``) so an exotic build missing or re-shaping the
    method degrades the accelerated hook path instead of crashing at import.
    """
    method = getattr(Schema, "_invoke_schema_validators", None)
    if method is None:
        return frozenset()
    try:
        return frozenset(inspect.signature(method).parameters)
    except (TypeError, ValueError):
        return frozenset()


#: Whether the installed marshmallow is 4.x. Detected by feature-probing the two
#: private hook invokers that :func:`_accelerated_load` reproduces: 4.x threads
#: ``unknown`` through the load processors/schema validators and renamed the
#: schema-validator ``pass_many`` argument to ``pass_collection``; 3.x does
#: neither. We probe the signatures rather than the version string so a faithful
#: transcription tracks the actual API.
_MA4: typing.Final = "pass_collection" in _schema_validator_params()


#: Set by :func:`install`: whether the running marshmallow's private ``_do_load``
#: internals match what :func:`_accelerated_load` transcribes. See
#: :func:`_accel_load_supported`.
_ACCEL_LOAD_VERIFIED: bool = False

#: The private ``Schema`` invokers :func:`_accelerated_load` calls, each mapped to
#: the keyword parameters the transcription passes. If a future marshmallow
#: renames one or drops a parameter, the transcription would diverge silently —
#: so :func:`install` verifies this shape and, on a mismatch, routes hook-bearing
#: schemas to the pure-Python load (correct, just unaccelerated).
_ACCEL_LOAD_INVOKERS: typing.Final = {
    "_invoke_load_processors": frozenset({"many", "original_data", "partial"}),
    "_invoke_field_validators": frozenset({"error_store", "data", "many"}),
    "_invoke_schema_validators": frozenset(
        {"error_store", "data", "original_data", "many", "partial", "field_errors"}
    ),
}


def _accel_load_supported() -> bool:
    """Whether :func:`_accelerated_load`'s transcription matches this marshmallow.

    Checks that every private invoker it calls exists and still accepts the
    keyword arguments the transcription passes (a robust proxy for "the
    ``_do_load`` body we mirror is unchanged", without false-positiving on benign
    comment/whitespace edits the way a source hash would). On any mismatch the
    accelerated hook path is disabled; the pure-Python path is always correct.
    """
    for name, needed in _ACCEL_LOAD_INVOKERS.items():
        method = getattr(Schema, name, None)
        if method is None:
            return False
        try:
            params = frozenset(inspect.signature(method).parameters)
        except (TypeError, ValueError):
            return False
        if not needed <= params:
            return False
    # The schema-validator collection flag is ``pass_collection`` (4.x) or
    # ``pass_many`` (3.x); the transcription branches on :data:`_MA4` for it.
    sv_params = _schema_validator_params()
    return "pass_collection" in sv_params or "pass_many" in sv_params


def _core_partial(partial: typing.Any) -> typing.Any:
    """Normalise ``partial`` to what the core's ``run`` expects: ``True`` (all
    optional), a collection of names, or ``False`` (not partial)."""
    if partial is True:
        return True
    if partial and isinstance(partial, _PARTIAL_COLLECTIONS):
        return partial
    return False


def _partial_is_supported(partial: typing.Any) -> bool:
    """Whether the core can model this ``partial`` (boolean/falsy or a name
    collection). Dotted-string entries within a collection are handled by the
    core's ``set_value``-style prefix matching; a bare-string ``partial`` defers."""
    return partial is True or not partial or isinstance(partial, _PARTIAL_COLLECTIONS)


# Saved originals; ``None`` while not installed (also used by ``is_installed``).
_orig_serialize: typing.Any = None
_orig_do_load: typing.Any = None
_orig_dumps: typing.Any = None
_orig_loads: typing.Any = None


def is_installed() -> bool:
    """Return whether the core is currently patched into ``Schema``."""
    return _orig_serialize is not None


# ``_hooks`` is resolved once per Schema *class* (shared across instances), so we
# cache the cheap boolean per class rather than recomputing it on every load/dump.
# Weak keys let transient/dynamically-created schema classes be collected.
_HAS_LOAD_HOOKS_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_HAS_DUMP_HOOKS_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _has_load_hooks(schema: Schema) -> bool:
    cls = type(schema)
    try:
        return _HAS_LOAD_HOOKS_CACHE[cls]
    except KeyError:
        hooks = schema._hooks
        result = any(hooks.get(hook) for hook in _LOAD_HOOKS)
        _HAS_LOAD_HOOKS_CACHE[cls] = result
        return result


def _has_dump_hooks(schema: Schema) -> bool:
    cls = type(schema)
    try:
        return _HAS_DUMP_HOOKS_CACHE[cls]
    except KeyError:
        hooks = schema._hooks
        result = bool(hooks.get(PRE_DUMP)) or bool(hooks.get(POST_DUMP))
        _HAS_DUMP_HOOKS_CACHE[cls] = result
        return result


def _patched_serialize(self: Schema, obj: typing.Any, *, many: bool = False):
    # R1: materialize one-shot iterables before the core might partially consume
    # them; if an element triggers AccelFallback the pure-Python re-run needs to
    # replay the same sequence from the start.
    if many and not isinstance(obj, (list, tuple)):
        obj = list(obj)
    cache = vars(self)
    ds = cache.get("_mc_dump_serializer", _UNSET)
    if ds is _UNSET:
        ds = cache["_mc_dump_serializer"] = _compiler.build_dump_serializer(self)
    if ds is not None:
        try:
            # ``many`` arrives raw from ``dump`` (``self.many`` may be any truthy
            # value, e.g. ``1``); the Rust boundary wants a real ``bool``.
            return ds.run(obj, bool(many))
        except _compiler.AccelFallback:
            # An element hit a shape it can't reproduce. Dump has no side effects
            # (it builds a fresh output and returns it), so we discard the partial
            # result and re-run the unchanged pure-Python serialize — exactly as
            # the load path does. A *genuine* error from a callback ``_serialize``
            # is not an ``AccelFallback`` and still propagates unchanged.
            pass
    return _orig_serialize(self, obj, many=many)


def _load_plan(self: Schema) -> typing.Any:
    """Build (once, cached on the instance) or fetch this schema's load plan.

    Returns ``None`` when the schema isn't compilable (always pure Python), else
    a tuple ``(deserializer, has_load_hooks, json_fusable)`` — the deserializer
    plus the two per-call constants. ``json_fusable`` is whether the whole spec
    tree is callback-free, so ``loads`` can skip the jiter parse for a schema
    that would only defer (ARCH.md B2). Shared by :func:`_patched_do_load` and
    :func:`_patched_loads` so the cached tuple shape can't drift between them.

    ``partial`` is intentionally *not* cached here (R4): ``_core_partial`` is
    two isinstance checks, and caching ``self.partial`` means mutations between
    calls silently skip validation.
    """
    cache = vars(self)
    plan = cache.get("_mc_load_plan", _UNSET)
    if plan is _UNSET:
        ld = _compiler.build_load_deserializer(self)
        plan = cache["_mc_load_plan"] = (
            None if ld is None else (ld, _has_load_hooks(self), ld.fusable)
        )
    return plan


def _patched_do_load(
    self: Schema,
    data: Mapping[str, typing.Any] | Sequence[Mapping[str, typing.Any]],
    *,
    many: bool | None = None,
    partial: typing.Any = None,
    unknown: typing.Any = None,
    postprocess: bool = True,
):
    many = bool(self.many) if many is None else bool(many)
    unknown = self.unknown if unknown is None else unknown
    if partial is None:
        partial = self.partial
    # The core is compiled for this schema's own ``unknown``; ``partial`` (boolean
    # or a name collection) is threaded as a runtime argument. Hook-bearing
    # schemas use the hook-aware path (core does the per-field step, Python runs
    # the hooks around it); hook-free schemas call the core directly.
    if unknown == self.unknown and _partial_is_supported(partial):
        plan = _load_plan(self)
        if plan is not None:
            ld, has_hooks, _fusable = plan
            if has_hooks:
                # Hook-bearing schemas use the hook-aware accelerated path only
                # when the running marshmallow's _do_load internals have been
                # verified (F_SPEEDUP F2). On an unverified build, go straight to
                # _orig_do_load without exception overhead (raising AccelFallback
                # costs ~0.5µs per call, making it 16% slower than stock).
                if _ACCEL_LOAD_VERIFIED:
                    try:
                        return _accelerated_load(
                            self, ld, data, many, partial, unknown, postprocess
                        )
                    except _compiler.AccelFallback:
                        pass  # per-field edge case → pure Python below
            else:
                # R4: recompute _core_partial live (two isinstance checks) rather
                # than using a cached value; self.partial can change between calls.
                try:
                    return ld.run(data, many, _core_partial(partial))
                except _compiler.AccelFallback:
                    pass  # off the happy path → unchanged pure-Python load below
    return _orig_do_load(
        self,
        data,
        many=many,
        partial=partial,
        unknown=unknown,
        postprocess=postprocess,
    )


def _accelerated_load(
    self: Schema,
    ld: typing.Any,
    data: typing.Any,
    many: bool,
    partial: typing.Any,
    unknown: typing.Any,
    postprocess: bool,
):
    """Run ``Schema._do_load``'s body with the core doing the per-field step.

    This is a line-for-line transcription of ``_do_load`` (after the
    argument-normalisation prologue, which the caller already did) with the
    single ``result = self._deserialize(...)`` call replaced by ``ld.run(...)``.
    ``ld.run`` raises :data:`_compiler.AccelFallback` for any per-field edge case,
    which propagates to the caller and re-runs the unchanged pure-Python load.

    The hook/validator invokers differ between marshmallow 3.x and 4.x: 4.x
    threads ``unknown`` through the load processors/schema validators and renamed
    ``pass_many`` -> ``pass_collection``. We branch on :data:`_MA4` so the
    transcription matches whichever ``_do_load`` we patched. Keeping the
    surrounding logic identical to stock is what makes the accelerated result
    byte-for-byte equal — including the order of accumulated errors and the
    ``handle_error`` callback.
    """
    # ``unknown`` is only threaded through the hook/validator invokers on 4.x.
    proc_unknown = {"unknown": unknown} if _MA4 else {}
    # 4.x renamed the schema-validator collection flag ``pass_many`` ->
    # ``pass_collection``.
    pass_kw = "pass_collection" if _MA4 else "pass_many"
    error_store = ErrorStore()
    errors: dict = {}
    result: typing.Any = None
    processed_data: typing.Any = data
    # Run preprocessors
    if self._hooks[PRE_LOAD]:
        try:
            processed_data = self._invoke_load_processors(
                PRE_LOAD,
                data,
                many=many,
                original_data=data,
                partial=partial,
                **proc_unknown,
            )
        except ValidationError as err:
            errors = err.normalized_messages()
            result = None
    if not errors:
        # Deserialize data — the accelerated per-field step (was _deserialize).
        result = ld.run(processed_data, many, _core_partial(partial))
        # Run field-level validation
        self._invoke_field_validators(error_store=error_store, data=result, many=many)
        # Run schema-level validation
        if self._hooks[VALIDATES_SCHEMA]:
            field_errors = bool(error_store.errors)
            self._invoke_schema_validators(
                error_store=error_store,
                data=result,
                original_data=data,
                many=many,
                partial=partial,
                field_errors=field_errors,
                **{pass_kw: True},
                **proc_unknown,
            )
            self._invoke_schema_validators(
                error_store=error_store,
                data=result,
                original_data=data,
                many=many,
                partial=partial,
                field_errors=field_errors,
                **{pass_kw: False},
                **proc_unknown,
            )
        errors = error_store.errors  # type: ignore[assignment]
        # Run post processors
        if not errors and postprocess and self._hooks[POST_LOAD]:
            try:
                result = self._invoke_load_processors(
                    POST_LOAD,
                    result,
                    many=many,
                    original_data=data,
                    partial=partial,
                    **proc_unknown,
                )
            except ValidationError as err:
                errors = err.normalized_messages()
    if errors:
        exc = ValidationError(errors, data=data, valid_data=result)
        self.handle_error(exc, data, many=many, partial=partial)
        raise exc

    return result


def _patched_dumps(
    self: Schema, obj: typing.Any, *args, many: bool | None = None, **kwargs
):
    # R1: materialize one-shot iterables before the core or the fallback consumes
    # them; both paths must replay the same sequence.
    eff_many = bool(self.many) if many is None else bool(many)
    if eff_many and not isinstance(obj, (list, tuple)):
        obj = list(obj)
    # Only fuse the default ``json.dumps`` call: any extra positional/keyword
    # argument (indent=, sort_keys=, cls=, ...) or a non-stdlib render module
    # could change the output, so defer to ``dump`` + ``render_module.dumps``.
    # Dump hooks (pre_dump/post_dump) also force the unfused path (they run via
    # ``dump``, which the fused writer bypasses).
    if (
        not args
        and not kwargs
        and self.opts.render_module is json
        and not _has_dump_hooks(self)
    ):
        cache = vars(self)
        js = cache.get("_mc_dump_json", _UNSET)
        if js is _UNSET:
            js = cache["_mc_dump_json"] = _compiler.build_dump_json_serializer(self)
        if js is not None:
            try:
                return js.run_json(obj, eff_many)
            except _compiler.AccelFallback:
                pass  # value the JSON writer can't reproduce -> stock path below
    return _orig_dumps(self, obj, *args, many=many, **kwargs)


def _patched_loads(
    self: Schema,
    json_data: typing.Any,
    *args,
    many: bool | None = None,
    partial: typing.Any = None,
    unknown: typing.Any = None,
    **kwargs,
):
    # SPIKE (Design A): fuse ``json.loads`` + the per-field load in Rust, reading
    # straight off a jiter tree (no intermediate Python dict). Only the plain
    # default call is fusable: extra ``render_module.loads`` args/kwargs, a
    # non-stdlib render module, schema load hooks, a non-default ``unknown``, or
    # an unsupported ``partial`` all defer to the stock ``loads``.
    eff_unknown = self.unknown if unknown is None else unknown
    eff_partial = self.partial if partial is None else partial
    if (
        not args
        and not kwargs
        and self.opts.render_module is json
        and eff_unknown == self.unknown
        and _partial_is_supported(eff_partial)
    ):
        # Reuse the same compiled plan the ``_do_load`` path builds/caches.
        plan = _load_plan(self)
        if plan is not None:
            ld, has_hooks, fusable = plan
            # Hook-bearing schemas can't fuse the JSON parse (the hooks run in
            # Python around the per-field step). A schema with a callback field
            # anywhere can't finish ``run_json`` either — it would parse the whole
            # JSON only to defer on the first record — so skip straight to stock
            # ``loads`` instead of wasting the jiter parse (ARCH.md B2).
            if not has_hooks and fusable:
                many_v = bool(self.many) if many is None else bool(many)
                try:
                    return ld.run_json(json_data, many_v, _core_partial(eff_partial))
                except _compiler.AccelFallback:
                    pass  # any edge case -> unchanged stock loads below
    return _orig_loads(
        self, json_data, *args, many=many, partial=partial, unknown=unknown, **kwargs
    )


def invalidate(schema: Schema) -> None:
    """Clear the compiled dump/load plan cached on *schema*.

    The core builds one plan per schema *instance* on the first ``dump``/
    ``load`` call and caches it on the instance forever.  A schema is treated
    as **immutable after its first accelerated call**: mutating fields or their
    validators afterwards is not reflected in the cached plan (the pure-Python
    path always reads live state, so the two paths would diverge).

    If you must reconfigure a schema in place — e.g. appending a validator or
    changing a field — call ``invalidate(schema)`` to drop all three caches and
    force a recompile on the next call.  Building a fresh ``Schema()`` instance
    is the simpler alternative.
    """
    cache = vars(schema)
    cache.pop("_mc_dump_serializer", None)
    cache.pop("_mc_dump_json", None)
    cache.pop("_mc_load_plan", None)


def install() -> None:
    """Patch the Rust core into ``marshmallow.Schema`` (idempotent)."""
    global _orig_serialize, _orig_do_load, _orig_dumps, _orig_loads
    global _ACCEL_LOAD_VERIFIED
    if is_installed():
        return
    # Verify the transcribed ``_do_load`` internals before trusting the
    # accelerated hook path; on a mismatch hook-bearing schemas use pure Python.
    _ACCEL_LOAD_VERIFIED = _accel_load_supported()
    _orig_serialize = Schema._serialize
    _orig_do_load = Schema._do_load
    _orig_dumps = Schema.dumps
    _orig_loads = Schema.loads
    Schema._serialize = _patched_serialize  # type: ignore[method-assign]
    Schema._do_load = _patched_do_load  # type: ignore[method-assign]
    Schema.dumps = _patched_dumps  # type: ignore[method-assign]
    Schema.loads = _patched_loads  # type: ignore[method-assign]


def uninstall() -> None:
    """Restore stock marshmallow's pure-Python ``Schema`` methods (idempotent)."""
    import warnings

    global _orig_serialize, _orig_do_load, _orig_dumps, _orig_loads
    if not is_installed():
        return
    # R7: check identity before restoring; if something else patched Schema after
    # our install(), restoring blindly would clobber its wrapper. Leave that
    # attribute alone and warn instead.
    def _restore(attr: str, our_fn: typing.Any, saved: typing.Any) -> None:
        if getattr(Schema, attr) is our_fn:
            setattr(Schema, attr, saved)  # type: ignore[method-assign]
        else:
            warnings.warn(
                f"marshmallow_core: Schema.{attr} was modified after install(); "
                "not restoring to avoid clobbering a foreign patch.",
                RuntimeWarning,
                stacklevel=3,
            )

    _restore("_serialize", _patched_serialize, _orig_serialize)
    _restore("_do_load", _patched_do_load, _orig_do_load)
    _restore("dumps", _patched_dumps, _orig_dumps)
    _restore("loads", _patched_loads, _orig_loads)
    _orig_serialize = None
    _orig_do_load = None
    _orig_dumps = None
    _orig_loads = None
