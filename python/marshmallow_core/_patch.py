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
compiled core; it therefore pins to marshmallow 4.x's ``_do_load`` internals and
leans on the equivalence suite. The core still raises ``AccelFallback`` for any
per-field edge case, so the whole load re-runs the unchanged pure-Python path.
"""

from __future__ import annotations

import typing

from marshmallow import Schema, ValidationError
from marshmallow.decorators import POST_LOAD, PRE_LOAD, VALIDATES, VALIDATES_SCHEMA
from marshmallow.error_store import ErrorStore

from marshmallow_core import _compiler

if typing.TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Sentinel: "not yet built". A built-but-not-compilable schema caches ``None``.
_UNSET: typing.Final = object()

#: Schema-level load hooks; their presence routes load through the hook-aware
#: accelerated path (:func:`_accelerated_load`) instead of the direct core call.
_LOAD_HOOKS = (PRE_LOAD, POST_LOAD, VALIDATES, VALIDATES_SCHEMA)

# Saved originals; ``None`` while not installed (also used by ``is_installed``).
_orig_serialize: typing.Any = None
_orig_do_load: typing.Any = None


def is_installed() -> bool:
    """Return whether the core is currently patched into ``Schema``."""
    return _orig_serialize is not None


def _has_load_hooks(schema: Schema) -> bool:
    hooks = schema._hooks
    return any(hooks.get(hook) for hook in _LOAD_HOOKS)


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
    if partial is None:
        partial = self.partial
    # The core is compiled for this schema's own ``unknown`` and non-partial (or
    # boolean ``partial=True``) config. Hook-bearing schemas use the hook-aware
    # path (core does the per-field step, Python runs the hooks around it);
    # hook-free schemas call the core directly.
    if unknown == self.unknown and (partial is True or not partial):
        cache = vars(self)
        ld = cache.get("_mc_load_deserializer", _UNSET)
        if ld is _UNSET:
            ld = cache["_mc_load_deserializer"] = _compiler.build_load_deserializer(
                self
            )
        if ld is not None:
            try:
                if _has_load_hooks(self):
                    return _accelerated_load(
                        self, ld, data, many, partial, unknown, postprocess
                    )
                return ld.run(data, many, partial is True)
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

    This is a line-for-line transcription of marshmallow 4.x ``_do_load`` (after
    the argument-normalisation prologue, which the caller already did) with the
    single ``result = self._deserialize(...)`` call replaced by ``ld.run(...)``.
    ``ld.run`` raises :data:`_compiler.AccelFallback` for any per-field edge case,
    which propagates to the caller and re-runs the unchanged pure-Python load.

    Keeping the surrounding hook/validator/post-load logic identical to stock is
    what makes the accelerated result byte-for-byte equal — including the order
    of accumulated errors and the ``handle_error`` callback.
    """
    error_store = ErrorStore()
    errors: dict = {}
    result: typing.Any = None
    # Run preprocessors
    if self._hooks[PRE_LOAD]:
        try:
            processed_data = self._invoke_load_processors(
                PRE_LOAD,
                data,
                many=many,
                original_data=data,
                partial=partial,
                unknown=unknown,
            )
        except ValidationError as err:
            errors = err.normalized_messages()
            result = None
    else:
        processed_data = data
    if not errors:
        # Deserialize data — the accelerated per-field step (was _deserialize).
        result = ld.run(processed_data, many, partial is True)
        # Run field-level validation
        self._invoke_field_validators(error_store=error_store, data=result, many=many)
        # Run schema-level validation
        if self._hooks[VALIDATES_SCHEMA]:
            field_errors = bool(error_store.errors)
            self._invoke_schema_validators(
                error_store=error_store,
                pass_collection=True,
                data=result,
                original_data=data,
                many=many,
                partial=partial,
                unknown=unknown,
                field_errors=field_errors,
            )
            self._invoke_schema_validators(
                error_store=error_store,
                pass_collection=False,
                data=result,
                original_data=data,
                many=many,
                partial=partial,
                unknown=unknown,
                field_errors=field_errors,
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
                    unknown=unknown,
                )
            except ValidationError as err:
                errors = err.normalized_messages()
    if errors:
        exc = ValidationError(errors, data=data, valid_data=result)
        self.handle_error(exc, data, many=many, partial=partial)
        raise exc

    return result


def install() -> None:
    """Patch the Rust core into ``marshmallow.Schema`` (idempotent)."""
    global _orig_serialize, _orig_do_load
    if is_installed():
        return
    _orig_serialize = Schema._serialize
    _orig_do_load = Schema._do_load
    Schema._serialize = _patched_serialize  # type: ignore[method-assign]
    Schema._do_load = _patched_do_load  # type: ignore[method-assign]


def uninstall() -> None:
    """Restore stock marshmallow's pure-Python ``Schema`` methods (idempotent)."""
    global _orig_serialize, _orig_do_load
    if not is_installed():
        return
    Schema._serialize = _orig_serialize  # type: ignore[method-assign]
    Schema._do_load = _orig_do_load  # type: ignore[method-assign]
    _orig_serialize = None
    _orig_do_load = None
