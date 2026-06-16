//! Rust acceleration core for marshmallow's `dump`/`load` paths, built as the
//! `marshmallow_core._core` extension. The two pyclasses below — [`DumpSerializer`]
//! and [`LoadDeserializer`] — are compiled from a bound `Schema` (see
//! `marshmallow_core._compiler`) and replace the per-object Python `_serialize` /
//! `_do_load` loops, deferring (via `AccelFallback`) to pure-Python marshmallow on
//! any shape they don't model so output/errors match exactly.
//!
//! The value models, evaluation, parsing, and GC traversal live in the
//! `dump`/`load` submodules and the shared `context`/`json_writer`/`temporal`/
//! `attr_access` modules; this file is just the PyO3 surface (pyclasses + the
//! `#[pymodule]` registration).

mod attr_access;
mod context;
mod dump;
mod json_writer;
mod load;
mod temporal;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};

use jiter::JsonValue;

use crate::attr_access::INDEXABLE_LAST;
use crate::context::{fallback, traverse_ctx, Ctx};
use crate::dump::gc::traverse_dump_serializer;
use crate::dump::parsing::parse_serializer;
use crate::dump::serializer::Serializer;
use crate::load::gc::traverse_load_serializer_inner;
use crate::load::json_tree::serializer_is_fusable;
use crate::load::parsing::parse_load_serializer;
use crate::load::serializer::LoadSerializer;
use crate::load::validators::Partial;

// Inner content struct; wrapped in ``Option<Box<>>`` so ``__clear__`` can drop
// all Python refs atomically by setting ``inner = None``.
struct DsInner {
    ctx: Ctx,
    root: Serializer,
}

#[pyclass(module = "marshmallow_core._core")]
pub struct DumpSerializer {
    inner: Option<Box<DsInner>>,
}

#[pymethods]
impl DumpSerializer {
    /// ``payload`` is ``(accessor, [field_spec, ...])``; see ``marshmallow_core._compiler``.
    #[new]
    fn new(py: Python<'_>, payload: &Bound<'_, PyAny>, missing: Py<PyAny>) -> PyResult<Self> {
        let ctx = Ctx::new(py, missing)?;
        let root = parse_serializer(py, payload)?;
        Ok(DumpSerializer {
            inner: Some(Box::new(DsInner { ctx, root })),
        })
    }

    /// Serialize ``obj`` (or each element if ``many``) into a dict (or list).
    fn run<'py>(&self, obj: &Bound<'py, PyAny>, many: bool) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.as_ref().expect("DumpSerializer cleared by GC");
        // Clear the per-invocation type cache before each top-level serialize
        // (F_SPEEDUP F3: all non-dict objects share this single-slot cache, so a
        // homogeneous many=True dump pays only one hasattr probe instead of
        // fields × records).
        INDEXABLE_LAST.with(|c| c.set((0, false)));
        inner.root.run(&inner.ctx, obj, many)
    }

    /// Serialize ``obj`` straight to a JSON string, fusing ``dump`` +
    /// ``json.dumps`` and skipping the intermediate Python dict. Byte-for-byte
    /// identical to stdlib ``json.dumps(self._serialize(obj, many))`` for the
    /// cases it handles; it raises ``AccelFallback`` for anything it cannot
    /// reproduce exactly (e.g. an unencodable value or a non-``str`` dict key),
    /// and the caller falls back to ``dump`` + ``json.dumps``.
    fn run_json(&self, obj: &Bound<'_, PyAny>, many: bool) -> PyResult<String> {
        let inner = self.inner.as_ref().expect("DumpSerializer cleared by GC");
        INDEXABLE_LAST.with(|c| c.set((0, false)));
        let mut buf = String::new();
        inner.root.write_json(&mut buf, &inner.ctx, obj, many)?;
        Ok(buf)
    }

    // R2: GC protocol — make cycles through DumpSerializer visible to CPython's
    // collector so schemas don't leak permanently.
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Some(inner) = &self.inner {
            traverse_ctx(&inner.ctx, &visit)?;
            traverse_dump_serializer(&inner.root, &visit)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.inner = None; // drops all Py<> refs, breaking the cycle
    }
}

struct LdInner {
    ctx: Ctx,
    root: LoadSerializer,
    fusable: bool,
}

#[pyclass(module = "marshmallow_core._core")]
pub struct LoadDeserializer {
    inner: Option<Box<LdInner>>,
}

#[pymethods]
impl LoadDeserializer {
    /// ``payload`` is ``(many, unknown, known_keys, [field_spec, ...])``.
    #[new]
    fn new(py: Python<'_>, payload: &Bound<'_, PyAny>, missing: Py<PyAny>) -> PyResult<Self> {
        let ctx = Ctx::new(py, missing)?;
        let root = parse_load_serializer(py, payload)?;
        let fusable = serializer_is_fusable(&root);
        Ok(LoadDeserializer {
            inner: Some(Box::new(LdInner { ctx, root, fusable })),
        })
    }

    /// Whether ``run_json`` can complete for this schema (no callback fields
    /// anywhere in the tree). Read once by the Python caller and cached.
    #[getter]
    fn fusable(&self) -> bool {
        self.inner
            .as_ref()
            .expect("LoadDeserializer cleared by GC")
            .fusable
    }

    /// Deserialize ``data``; raises ``AccelFallback`` to defer to Python.
    ///
    /// ``partial`` mirrors ``load(partial=...)``: ``True`` makes every field
    /// optional, a collection of (possibly dotted) names makes those optional,
    /// and anything falsy is non-partial. A field missing under partial is
    /// skipped (no default applied, no ``required`` error), recursively into
    /// nested schemas (dotted entries select nested fields).
    fn run<'py>(
        &self,
        data: &Bound<'py, PyAny>,
        many: bool,
        partial: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.as_ref().expect("LoadDeserializer cleared by GC");
        let p = Partial::from_arg(partial)?;
        inner.root.run(&inner.ctx, data, many, &p)
    }

    /// SPIKE (Design A): fused ``loads`` — parse JSON ``data`` (a ``str`` or
    /// ``bytes``) into a jiter ``JsonValue`` tree and deserialize directly off
    /// the tree, skipping the intermediate Python ``dict`` that ``json.loads``
    /// would build. Output keys come from the schema (already-interned
    /// ``out_key`` strings), so this allocates **no** per-record key strings.
    /// Raises ``AccelFallback`` for any shape the tree walker doesn't model, so
    /// the caller re-runs the unchanged ``json.loads`` + ``_do_load`` path.
    fn run_json<'py>(
        &self,
        py: Python<'py>,
        data: &Bound<'py, PyAny>,
        many: bool,
        partial: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let p = Partial::from_arg(partial)?;
        // ``allow_inf_nan = true`` matches stdlib ``json.loads`` (accepts
        // ``NaN``/``Infinity``); a Float field still rejects them via its own
        // guard, deferring to Python for the exact ``special`` error.
        let inner = self.inner.as_ref().expect("LoadDeserializer cleared by GC");
        if let Ok(s) = data.cast::<PyString>() {
            // R6: ``to_str()`` fails on lone surrogates; map the error to fallback
            // so stock ``json.loads`` handles it (which succeeds on these inputs).
            let txt = s.to_str().map_err(|_| fallback())?;
            let jv = JsonValue::parse(txt.as_bytes(), true).map_err(|_| fallback())?;
            inner.root.run_json_tree(&inner.ctx, py, &jv, many, &p)
        } else if let Ok(b) = data.cast::<PyBytes>() {
            let jv = JsonValue::parse(b.as_bytes(), true).map_err(|_| fallback())?;
            inner.root.run_json_tree(&inner.ctx, py, &jv, many, &p)
        } else {
            Err(fallback())
        }
    }

    // R2: GC protocol — makes cycles through LoadDeserializer visible to CPython.
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Some(inner) = &self.inner {
            traverse_ctx(&inner.ctx, &visit)?;
            traverse_load_serializer_inner(&inner.root, &visit)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.inner = None; // drops all Py<> refs, breaking the cycle
    }
}

/// Wire-format/ABI version of the payloads exchanged with ``marshmallow_core._compiler``.
/// Bump this whenever the element tags or payload tuple shapes change so a stale
/// compiled extension paired with a newer ``marshmallow`` (or vice versa) is
/// detected and the pure-Python path is used instead of misreading payloads.
const PROTOCOL_VERSION: u32 = 20;

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<DumpSerializer>()?;
    m.add_class::<LoadDeserializer>()?;
    m.add("AccelFallback", m.py().get_type::<context::AccelFallback>())?;
    m.add("PROTOCOL_VERSION", PROTOCOL_VERSION)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    //! Unit tests for the Python-free leaf logic. These run under `cargo test`
    //! (which links libpython because the `extension-module` feature is off — see
    //! Cargo.toml); the cross-language parity story lives in
    //! `tests/test_equivalence.py`, and the JSON-escaping units live in
    //! `json_writer`.
    use jiter::JsonValue;

    #[test]
    fn json_parse_keeps_last_duplicate_key() {
        // jiter does not dedup keys; stdlib json.loads keeps the last value, and
        // ``run_one_json``'s slot fill reproduces that by overwriting. Verify the
        // ordering assumption the loader relies on at the tree level.
        let data = br#"{"a": 1, "b": 2, "a": 3}"#;
        let JsonValue::Object(obj) = JsonValue::parse(data, false).unwrap() else {
            panic!("expected an object");
        };
        let mut last_a = None;
        for (k, v) in obj.iter() {
            if k.as_ref() == "a" {
                last_a = Some(v);
            }
        }
        assert!(matches!(last_a, Some(JsonValue::Int(3))));
    }
}
