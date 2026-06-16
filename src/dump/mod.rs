//! Rust acceleration core for marshmallow's `dump` (serialization) path.
//!
//! A `DumpSerializer` is compiled (see `marshmallow_core._compiler`) from a bound
//! `Schema` and replaces the per-object Python `_serialize` loop. The model is
//! recursive:
//!
//! * a [`serializer::Serializer`] turns an object into a dict, one
//!   [`serializer::FieldSpec`] per field;
//! * a [`serializer::FieldSpec`] is either *native* (attribute access + an
//!   [`serializer::Element`]) or a *callback* that defers to `Field.serialize`;
//! * an [`serializer::Element`] is the value->output transform — scalar
//!   formatting, a nested `Serializer` (for `Nested`), or a mapped inner element
//!   (for `List`).
//!
//! Anything the Rust side does not model natively stays a callback, so the
//! accelerated output is behaviour-identical to pure-Python marshmallow.
//!
//! The dump path has a **limited `AccelFallback`**: a few elements raise it for a
//! shape they can't reproduce (a `Tuple` length mismatch, a non-dict `DictTyped`
//! input, a value `write_json_value` cannot encode), and the caller discards the
//! partial result and re-runs pure Python — safe because dump has no side effects.
//! But it is *not* a general safety net: an element that silently produces the
//! *wrong* value is never caught. So every native dump `Element` must be
//! *provably* identical to the corresponding `Field._serialize` for every input
//! it accepts, deferring (raising `AccelFallback`) on any shape it does not, and
//! every new one needs a `_dump_both` equivalence test (`tests/test_equivalence.py`).
//! When in doubt, leave it a callback.

pub(crate) mod gc;
pub(crate) mod parsing;
pub(crate) mod serializer;
