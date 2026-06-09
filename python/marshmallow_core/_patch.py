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
equivalence suite. The core still raises ``AccelFallback`` for any per-field edge
case, so the whole load re-runs the unchanged pure-Python path.
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

#: Whether the installed marshmallow is 4.x. Detected by feature-probing the two
#: private hook invokers that :func:`_accelerated_load` reproduces: 4.x threads
#: ``unknown`` through the load processors/schema validators and renamed the
#: schema-validator ``pass_many`` argument to ``pass_collection``; 3.x does
#: neither. We probe the signatures rather than the version string so a faithful
#: transcription tracks the actual API.
_MA4: typing.Final = (
    "pass_collection"
    in inspect.signature(Schema._invoke_schema_validators).parameters
)


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
    return (
        partial is True
        or not partial
        or isinstance(partial, _PARTIAL_COLLECTIONS)
    )

# Saved originals; ``None`` while not installed (also used by ``is_installed``).
_orig_serialize: typing.Any = None
_orig_do_load: typing.Any = None
_orig_dumps: typing.Any = None


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
    cache = vars(self)
    ds = cache.get("_mc_dump_serializer", _UNSET)
    if ds is _UNSET:
        ds = cache["_mc_dump_serializer"] = _compiler.build_dump_serializer(self)
    if ds is not None:
        return ds.run(obj, many)
    return _orig_serialize(self, obj, many=many)


def _patched_do_load(
    self: Schema,
    data: Mapping[str, typing.Any] | Sequence[Mapping[str, typing.Any]],
    *,
    many: bool | None = None,
    partial: typing.Any = None,
    unknown: typing.Any = None,
    postprocess: bool = True,
):
    many = self.many if many is None else bool(many)
    unknown = self.unknown if unknown is None else unknown
    # Track whether the caller supplied ``partial`` so we can reuse the cached
    # ``_core_partial(self.partial)`` on the common "use the schema default" call.
    partial_is_default = partial is None
    if partial_is_default:
        partial = self.partial
    # The core is compiled for this schema's own ``unknown``; ``partial`` (boolean
    # or a name collection) is threaded as a runtime argument. Hook-bearing
    # schemas use the hook-aware path (core does the per-field step, Python runs
    # the hooks around it); hook-free schemas call the core directly.
    if unknown == self.unknown and _partial_is_supported(partial):
        cache = vars(self)
        # Cached once per instance: the compiled deserializer plus the two
        # constants we'd otherwise recompute every load — whether the schema has
        # load hooks, and the core form of its default ``partial``. ``None`` means
        # "not compilable" (always pure Python); ``_UNSET`` means "not built yet".
        plan = cache.get("_mc_load_plan", _UNSET)
        if plan is _UNSET:
            ld = _compiler.build_load_deserializer(self)
            plan = cache["_mc_load_plan"] = (
                None
                if ld is None
                else (ld, _has_load_hooks(self), _core_partial(self.partial))
            )
        if plan is not None:
            ld, has_hooks, default_core_partial = plan
            try:
                if has_hooks:
                    return _accelerated_load(
                        self, ld, data, many, partial, unknown, postprocess
                    )
                core_partial = (
                    default_core_partial
                    if partial_is_default
                    else _core_partial(partial)
                )
                return ld.run(data, many, core_partial)
            except _compiler.AccelFallback:
                pass  # off the happy path -> unchanged pure-Python load below
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
        errors = error_store.errors
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


def _patched_dumps(self: Schema, obj: typing.Any, *args, many: bool | None = None, **kwargs):
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
                return js.run_json(obj, self.many if many is None else bool(many))
            except _compiler.AccelFallback:
                pass  # value the JSON writer can't reproduce -> stock path below
    return _orig_dumps(self, obj, *args, many=many, **kwargs)


def install() -> None:
    """Patch the Rust core into ``marshmallow.Schema`` (idempotent)."""
    global _orig_serialize, _orig_do_load, _orig_dumps
    if is_installed():
        return
    _orig_serialize = Schema._serialize
    _orig_do_load = Schema._do_load
    _orig_dumps = Schema.dumps
    Schema._serialize = _patched_serialize  # type: ignore[method-assign]
    Schema._do_load = _patched_do_load  # type: ignore[method-assign]
    Schema.dumps = _patched_dumps  # type: ignore[method-assign]


def uninstall() -> None:
    """Restore stock marshmallow's pure-Python ``Schema`` methods (idempotent)."""
    global _orig_serialize, _orig_do_load, _orig_dumps
    if not is_installed():
        return
    Schema._serialize = _orig_serialize  # type: ignore[method-assign]
    Schema._do_load = _orig_do_load  # type: ignore[method-assign]
    Schema.dumps = _orig_dumps  # type: ignore[method-assign]
    _orig_serialize = None
    _orig_do_load = None
    _orig_dumps = None
