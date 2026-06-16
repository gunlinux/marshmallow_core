//! Object → value attribute access for the dump path, replicating
//! ``marshmallow.utils.get_value`` (dotted-path aware) with a per-thread type
//! cache for the ``__getitem__`` probe (F_SPEEDUP F3).

use pyo3::exceptions::{PyAttributeError, PyIndexError, PyKeyError, PyTypeError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use std::cell::Cell;

// Per-thread, single-slot cache: (type_ptr, has_getitem). For homogeneous
// ``many=True`` dumps (all objects of the same type) this eliminates all but
// the first ``hasattr("__getitem__")`` probe (F_SPEEDUP F3). Cleared at the
// start of each top-level ``DumpSerializer::run``/``run_json`` call.
thread_local! {
    pub(crate) static INDEXABLE_LAST: Cell<(usize, bool)> = const { Cell::new((0, false)) };
}

/// Replicates `marshmallow.utils.get_value(obj, key, missing)`, dotted-path aware.
pub(crate) fn get_value<'py>(
    py: Python<'py>,
    obj: &Bound<'py, PyAny>,
    key: &Py<PyString>,
    key_parts: &Option<Vec<Py<PyString>>>,
    missing: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    match key_parts {
        Some(parts) => {
            let mut cur = obj.clone();
            for part in parts {
                cur = get_one(py, &cur, part.bind(py), missing)?;
            }
            Ok(cur)
        }
        None => get_one(py, obj, key.bind(py), missing),
    }
}

/// Replicates `marshmallow.utils._get_value_for_key`: try ``obj[key]`` (when
/// indexable), falling back to ``getattr(obj, key, missing)``.
fn get_one<'py>(
    py: Python<'py>,
    obj: &Bound<'py, PyAny>,
    key: &Bound<'py, PyString>,
    missing: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    // Fast path for a plain ``dict`` (the common dump source): a direct lookup
    // avoids the per-field ``hasattr(__getitem__)`` probe and the exception-based
    // ``KeyError`` handling below. A present key returns its value (``obj[key]``);
    // a missing key falls through to ``getattr`` exactly as marshmallow does (so a
    // field named ``items``/``keys`` still resolves to the dict method). Only an
    // *exact* dict takes this path; a subclass that overrides ``__getitem__`` uses
    // the general path.
    if obj.is_exact_instance_of::<PyDict>() {
        let dict = obj.cast::<PyDict>()?;
        if let Some(v) = dict.get_item(key)? {
            return Ok(v);
        }
        return match obj.getattr(key) {
            Ok(v) => Ok(v),
            Err(e) if e.is_instance_of::<PyAttributeError>(py) => Ok(missing.clone()),
            Err(e) => Err(e),
        };
    }
    // Non-dict: check whether the object's type is indexable, using a
    // single-slot type-level cache (F_SPEEDUP F3). For homogeneous many=True
    // dumps (all objects the same type) this reduces N*fields hasattr probes to
    // just one. The cache is cleared at the start of each top-level run call.
    let type_ptr = obj.get_type().as_ptr() as usize;
    let cached = INDEXABLE_LAST.with(|c| {
        let (ptr, val) = c.get();
        if ptr == type_ptr {
            Some(val)
        } else {
            None
        }
    });
    let has_getitem = match cached {
        Some(v) => v,
        None => {
            let v = obj.hasattr(intern!(py, "__getitem__"))?;
            INDEXABLE_LAST.with(|c| c.set((type_ptr, v)));
            v
        }
    };
    if has_getitem {
        match obj.get_item(key) {
            Ok(v) => return Ok(v),
            Err(e) => {
                let caught = e.is_instance_of::<PyKeyError>(py)
                    || e.is_instance_of::<PyIndexError>(py)
                    || e.is_instance_of::<PyTypeError>(py)
                    || e.is_instance_of::<PyAttributeError>(py);
                if !caught {
                    return Err(e);
                }
            }
        }
    }
    match obj.getattr(key) {
        Ok(v) => Ok(v),
        Err(e) if e.is_instance_of::<PyAttributeError>(py) => Ok(missing.clone()),
        Err(e) => Err(e),
    }
}
