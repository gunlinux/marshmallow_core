//! Shared, schema-independent context plus the small fallback/type helpers used
//! by both the dump and load paths.
//!
//! [`AccelFallback`] is the internal signal raised the instant the accelerated
//! path meets an edge case it does not model; the Python caller then re-runs the
//! unchanged pure-Python marshmallow so every error/value matches exactly.

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyKeyboardInterrupt, PySystemExit};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};

create_exception!(
    _core,
    AccelFallback,
    PyException,
    "Internal signal: the accelerated load hit an edge case and the caller \
     should fall back to pure-Python marshmallow (so errors/values match exactly)."
);

/// Shared, schema-independent context (sentinels and builtins).
pub(crate) struct Ctx {
    pub(crate) missing: Py<PyAny>,
    pub(crate) int_fn: Py<PyAny>,
    pub(crate) float_fn: Py<PyAny>,
    pub(crate) dict_fn: Py<PyAny>,
}

impl Ctx {
    pub(crate) fn new(py: Python<'_>, missing: Py<PyAny>) -> PyResult<Self> {
        let builtins = py.import("builtins")?;
        Ok(Ctx {
            missing,
            int_fn: builtins.getattr("int")?.unbind(),
            float_fn: builtins.getattr("float")?.unbind(),
            dict_fn: builtins.getattr("dict")?.unbind(),
        })
    }
}

#[inline]
pub(crate) fn fallback() -> PyErr {
    AccelFallback::new_err(())
}

/// Turn a Python error raised while running user/Python code into an
/// ``AccelFallback`` so the caller re-runs the pure-Python path — *unless* it is
/// a ``KeyboardInterrupt`` or ``SystemExit``. Those must propagate unchanged
/// rather than be swallowed and silently retried on the pure-Python path.
#[inline]
pub(crate) fn to_fallback(py: Python<'_>, err: PyErr) -> PyErr {
    if err.is_instance_of::<PyKeyboardInterrupt>(py) || err.is_instance_of::<PySystemExit>(py) {
        err
    } else {
        fallback()
    }
}

/// list/tuple only — conservatively falls back for other iterables so we never
/// mis-iterate a ``Mapping`` (which ``is_collection`` excludes) or a generator.
pub(crate) fn is_list_like(val: &Bound<'_, PyAny>) -> bool {
    val.is_instance_of::<PyList>() || val.is_instance_of::<PyTuple>()
}

/// N1: returns ``true`` if ``value`` is a non-replayable one-shot iterator
/// (has ``__next__``). Re-iterable containers (list, tuple, set, range,
/// dict views) do NOT have ``__next__`` on the object itself, so they stay fast.
///
/// Lists and tuples short-circuit to ``false`` before the ``hasattr`` call —
/// they are the overwhelmingly common dump input, so the hot path pays only
/// two type-pointer comparisons and never reaches Python.
#[inline]
pub(crate) fn is_one_shot_iterator(value: &Bound<'_, PyAny>) -> bool {
    if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        return false;
    }
    value
        .hasattr(intern!(value.py(), "__next__"))
        .unwrap_or(false)
}

// R2: GC traverse helper shared by both pyclasses. CPython's cyclic GC can't see
// through Rust-owned ``Py<T>`` refs, so without ``__traverse__``/``__clear__``
// any schema that ever dumps/loads leaks itself permanently. This makes the
// ``Ctx``-held builtins visible so the collector can break the cycle.
pub(crate) fn traverse_ctx(
    ctx: &Ctx,
    visit: &pyo3::PyVisit<'_>,
) -> Result<(), pyo3::PyTraverseError> {
    visit.call(&ctx.missing)?;
    visit.call(&ctx.int_fn)?;
    visit.call(&ctx.float_fn)?;
    visit.call(&ctx.dict_fn)?;
    Ok(())
}
