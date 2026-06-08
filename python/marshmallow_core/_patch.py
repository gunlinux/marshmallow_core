"""Monkey-patch stock marshmallow's :class:`~marshmallow.Schema` to use the core.

:func:`install` swaps ``Schema._serialize`` and ``Schema._do_load`` for wrappers
that try the compiled Rust core and otherwise call the saved pure-Python
originals. :func:`uninstall` restores them. Patching is process-wide and
idempotent.

The compiled serializer/deserializer is cached per *instance* (on
``schema.__dict__``), so building it happens once per schema. A schema that the
core cannot model caches ``None`` and always uses the original method.

Unlike the in-tree integration on marshmallow's ``rust-core`` branch, schemas
with ``pre_load``/``post_load``/``validates``/``validates_schema`` hooks use the
pure-Python load path here: the hook-aware "core does the per-field step, Python
runs the hooks around it" split lives *inside* ``_do_load`` and cannot be
reproduced by wrapping the method from outside. Correctness is unaffected; those
schemas simply are not accelerated on load.
"""

from __future__ import annotations

import typing

from marshmallow import Schema
from marshmallow.decorators import POST_LOAD, PRE_LOAD, VALIDATES, VALIDATES_SCHEMA

from marshmallow_core import _compiler

if typing.TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Sentinel: "not yet built". A built-but-not-compilable schema caches ``None``.
_UNSET: typing.Final = object()

#: Schema-level hooks that force the pure-Python load path (see module docstring).
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
    # boolean ``partial=True``) config, and only for schemas without load hooks.
    if (
        unknown == self.unknown
        and (partial is True or not partial)
        and not _has_load_hooks(self)
    ):
        cache = vars(self)
        ld = cache.get("_mc_load_deserializer", _UNSET)
        if ld is _UNSET:
            ld = cache["_mc_load_deserializer"] = _compiler.build_load_deserializer(
                self
            )
        if ld is not None:
            try:
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
