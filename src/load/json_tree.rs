//! jiter ``JsonValue`` → Python conversion (the leaf materialiser the fused load
//! falls back to) plus the compile-time fusability/partial-forwarding predicates
//! that walk the load tree.

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyString};

use jiter::{cached_py_string, JsonValue};

use crate::load::element::{LoadElement, LoadFieldSpec};
use crate::load::serializer::LoadSerializer;

/// Convert a jiter ``JsonValue`` (sub)tree into exactly the Python object
/// ``json.loads`` would have produced for it. Parity of the fused load rests on
/// this being byte-identical to the stdlib parser's output per leaf.
///
/// String leaves use jiter's global LRU string cache (F_SPEEDUP F7): repeated
/// values (enum-like fields, status strings, country codes) return the same
/// Python object across calls, saving an allocation and making ``is``/``==``
/// comparisons in Python code cheaper.
pub(crate) fn json_to_py<'py>(py: Python<'py>, jv: &JsonValue<'_>) -> PyResult<Bound<'py, PyAny>> {
    match jv {
        JsonValue::Null => Ok(py.None().into_bound(py)),
        JsonValue::Bool(b) => Ok(PyBool::new(py, *b).to_owned().into_any()),
        JsonValue::Int(i) => Ok((*i).into_pyobject(py)?.into_any()),
        // NB: jiter is built without `num-bigint` here, so an integer larger
        // than i64 fails to parse and we fall back to ``json.loads`` (which
        // handles arbitrary precision) — rare, and correct.
        JsonValue::Float(f) => Ok((*f).into_pyobject(py)?.into_any()),
        JsonValue::Str(s) => Ok(cached_py_string(py, s.as_ref()).into_any()),
        JsonValue::Array(a) => {
            let out = PyList::empty(py);
            for item in a.iter() {
                out.append(json_to_py(py, item)?)?;
            }
            Ok(out.into_any())
        }
        JsonValue::Object(o) => {
            // ``json.loads`` keeps the last value for a duplicated key; building
            // the dict in order with ``set_item`` reproduces that.
            let out = PyDict::new(py);
            for (k, v) in o.iter() {
                out.set_item(PyString::new(py, k.as_ref()), json_to_py(py, v)?)?;
            }
            Ok(out.into_any())
        }
    }
}

/// Whether a serializer (and everything reachable through nested schemas) has no
/// callback field, so ``run_json`` can finish off the jiter tree. Used once at
/// construction to set ``LoadDeserializer::fusable``.
pub(crate) fn serializer_is_fusable(s: &LoadSerializer) -> bool {
    s.specs.iter().all(|spec| match spec {
        LoadFieldSpec::Callback { .. } => false,
        LoadFieldSpec::Native { element, .. } => element_is_fusable(element),
    })
}

/// Whether a load element contains only fusable sub-serializers (a nested schema
/// with a callback field makes the enclosing element non-fusable).
fn element_is_fusable(e: &LoadElement) -> bool {
    match e {
        LoadElement::Nested(serializer) => serializer_is_fusable(serializer),
        LoadElement::Pluck { serializer, .. } => serializer_is_fusable(serializer),
        LoadElement::NestedPostLoad { serializer, .. } => serializer_is_fusable(serializer),
        LoadElement::List(inner, _) => element_is_fusable(inner),
        LoadElement::Tuple(elements) => elements.iter().all(element_is_fusable),
        LoadElement::DictTyped { key_el, val_el, .. } => {
            key_el.as_deref().is_none_or(element_is_fusable)
                && val_el.as_deref().is_none_or(element_is_fusable)
        }
        // Scalars and held-method elements contain no nested serializer.
        _ => true,
    }
}

/// Whether a load element passes ``partial`` into a nested schema (F_SPEEDUP F4).
/// Only ``Nested``/``Pluck`` — and containers that wrap them — ever forward the
/// sub-partial; pure scalar elements never read it. Mirrors the recursion shape
/// of ``element_is_fusable``.
pub(crate) fn element_consumes_partial(e: &LoadElement) -> bool {
    match e {
        LoadElement::Nested(_) | LoadElement::Pluck { .. } | LoadElement::NestedPostLoad { .. } => {
            true
        }
        LoadElement::List(inner, _) => element_consumes_partial(inner),
        LoadElement::Tuple(elements) => elements.iter().any(element_consumes_partial),
        LoadElement::DictTyped { key_el, val_el, .. } => {
            key_el.as_deref().is_some_and(element_consumes_partial)
                || val_el.as_deref().is_some_and(element_consumes_partial)
        }
        _ => false,
    }
}
