//! Load (deserialization) acceleration.
//!
//! The load accelerator handles only the *happy path* — valid input through a
//! schema with no hooks/validators/partial. The instant it meets anything off
//! the happy path (a coercion failure, a missing ``required`` field, an unknown
//! key under ``RAISE``, a non-dict, a callback field raising, ...) it raises
//! [`crate::context::AccelFallback`], and the Python caller re-runs the
//! pure-Python ``_do_load``. That keeps every error message and edge-case value
//! byte-for-byte identical to pure-Python marshmallow while accelerating the
//! common case.

pub(crate) mod element;
pub(crate) mod gc;
pub(crate) mod json_tree;
pub(crate) mod parsing;
pub(crate) mod serializer;
pub(crate) mod validators;

pub(crate) const UNKNOWN_RAISE: u8 = 0;
// EXCLUDE (1) needs no Rust-side handling (unknown keys are simply ignored), so
// it is not named here.
pub(crate) const UNKNOWN_INCLUDE: u8 = 2;
