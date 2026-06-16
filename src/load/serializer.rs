//! [`LoadSerializer`] — one schema level for load — plus its dict-source
//! ([`LoadSerializer::run`]) and jiter-tree-source ([`LoadSerializer::run_json_tree`])
//! evaluation, and the dotted-output-key helpers.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};

use jiter::{JsonArray, JsonObject, JsonValue};
use std::collections::HashMap;

use crate::context::{fallback, is_list_like, to_fallback, Ctx};
use crate::load::element::LoadFieldSpec;
use crate::load::json_tree::json_to_py;
use crate::load::validators::Partial;
use crate::load::{UNKNOWN_INCLUDE, UNKNOWN_RAISE};

/// Split ``key`` into dotted parts, or ``None`` if it contains no ``.``.
pub(crate) fn split_key_parts(
    py: Python<'_>,
    key: &Bound<'_, PyString>,
) -> PyResult<Option<Vec<Py<PyString>>>> {
    let s = key.to_str()?;
    if s.contains('.') {
        Ok(Some(
            s.split('.')
                .map(|p| PyString::new(py, p).unbind())
                .collect(),
        ))
    } else {
        Ok(None)
    }
}

/// Write ``value`` into ``dict`` at ``key``, reproducing
/// ``marshmallow.utils.set_value``: a dotted key builds nested dicts. An
/// intermediate path component that already holds a non-dict value raises
/// ``AccelFallback`` so Python reproduces the exact ``ValueError``.
fn set_load_value(
    py: Python<'_>,
    dict: &Bound<'_, PyDict>,
    key: &Bound<'_, PyString>,
    parts: &Option<Vec<Py<PyString>>>,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    match parts {
        None => dict.set_item(key, value),
        Some(parts) => {
            let mut target = dict.clone();
            for part in &parts[..parts.len() - 1] {
                let p = part.bind(py);
                target = match target.get_item(p)? {
                    Some(existing) => existing.cast_into::<PyDict>().map_err(|_| fallback())?,
                    None => {
                        let nested = PyDict::new(py);
                        target.set_item(p, &nested)?;
                        nested
                    }
                };
            }
            target.set_item(parts[parts.len() - 1].bind(py), value)
        }
    }
}

/// One schema level for load: its field specs plus unknown-key handling.
pub(crate) struct LoadSerializer {
    pub(crate) specs: Vec<LoadFieldSpec>,
    pub(crate) many: bool,
    pub(crate) unknown: u8,           // UNKNOWN_RAISE | UNKNOWN_EXCLUDE
    pub(crate) known_keys: Py<PyAny>, // frozenset of data keys (consulted on the PyDict path)
    /// ``data_key`` -> index into ``specs``, built once. The JSON-tree path uses
    /// it to bucket each record's keys in a single pass instead of scanning the
    /// object per field (which was O(fields x keys); see ``run_one_json``).
    pub(crate) data_key_index: HashMap<String, usize>,
    /// Whether every ``data_key`` is distinct. If two specs share one, the
    /// single-pass slot fill can't reproduce stock marshmallow (both fields read
    /// the same input key), so the JSON path defers. Virtually always ``true``.
    pub(crate) distinct_data_keys: bool,
}

impl LoadSerializer {
    pub(crate) fn run<'py>(
        &self,
        ctx: &Ctx,
        data: &Bound<'py, PyAny>,
        many: bool,
        partial: &Partial<'py>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = data.py();
        if many {
            // ``_deserialize`` stores a type error for non-sequences -> fall back.
            if !is_list_like(data) {
                return Err(fallback());
            }
            let out = PyList::empty(py);
            for item in data.try_iter()? {
                out.append(self.run_one(ctx, &item?, partial)?)?;
            }
            Ok(out.into_any())
        } else {
            Ok(self.run_one(ctx, data, partial)?.into_any())
        }
    }

    pub(crate) fn run_one<'py>(
        &self,
        ctx: &Ctx,
        data: &Bound<'py, PyAny>,
        partial: &Partial<'py>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let py = data.py();
        // Python checks ``isinstance(data, Mapping)``; we only model plain dicts.
        let data = data.cast::<PyDict>().map_err(|_| fallback())?;
        let missing = ctx.missing.bind(py);
        let out = PyDict::new(py);
        for spec in &self.specs {
            match spec {
                LoadFieldSpec::Native {
                    data_key,
                    out_key,
                    out_key_parts,
                    attr_name,
                    load_default,
                    required,
                    allow_none,
                    element,
                    consumes_partial,
                    validators,
                } => {
                    let raw = data.get_item(data_key.bind(py))?;
                    let Some(value) = raw else {
                        // missing from input
                        if partial.allows_missing(attr_name.bind(py))? {
                            continue; // ``partial``: skip (no default, no required)
                        }
                        if *required {
                            return Err(fallback());
                        }
                        let default = load_default.bind(py);
                        if !default.is(missing) {
                            set_load_value(py, &out, out_key.bind(py), out_key_parts, default)?;
                        }
                        continue;
                    };
                    if value.is_none() {
                        if *allow_none {
                            set_load_value(
                                py,
                                &out,
                                out_key.bind(py),
                                out_key_parts,
                                &py.None().into_bound(py),
                            )?;
                            continue;
                        }
                        return Err(fallback()); // ``null`` error
                    }
                    // The sub-partial for this field. Only call ``derive`` when
                    // the element actually forwards partial into a nested schema
                    // (F_SPEEDUP F4: for flat schemas all specs have
                    // ``consumes_partial = false``, eliminating all derive calls).
                    let sub_partial_opt: Option<Partial> = if *consumes_partial {
                        match partial {
                            Partial::Coll(_) => Some(partial.derive(py, attr_name.bind(py))?),
                            _ => None,
                        }
                    } else {
                        None
                    };
                    let field_partial: &Partial = sub_partial_opt.as_ref().unwrap_or(partial);
                    let result = element.apply(ctx, &value, field_partial)?;
                    // Validators run on the deserialized value (mirrors
                    // ``Field._validate(output)``); any failure or error defers
                    // to Python for the exact ``ValidationError`` message.
                    for validator in validators {
                        match validator.check(&result) {
                            Ok(true) => {}
                            Ok(false) => return Err(fallback()),
                            Err(e) => return Err(to_fallback(py, e)),
                        }
                    }
                    set_load_value(py, &out, out_key.bind(py), out_key_parts, &result)?;
                }
                LoadFieldSpec::Callback {
                    data_key,
                    attr_name,
                    out_key,
                    out_key_parts,
                    field,
                } => {
                    let raw = data.get_item(data_key.bind(py))?;
                    let value = match raw {
                        Some(v) => v,
                        None => {
                            if partial.allows_missing(attr_name.bind(py))? {
                                continue; // ``partial``: skip the missing field
                            }
                            missing.clone()
                        }
                    };
                    // Any error (ValidationError, TypeError, ...) -> fall back so
                    // Python accumulates the full, correct error structure. Pass
                    // the sub-partial down so a callback ``Nested`` propagates it.
                    let bound = field.bind(py);
                    // Same as the native arm: ``derive`` is identity for
                    // ``None``/``All``; only a ``Coll`` needs prefix-stripping.
                    let derived;
                    let field_partial: &Partial = match partial {
                        Partial::Coll(_) => {
                            derived = partial.derive(py, attr_name.bind(py))?;
                            &derived
                        }
                        _ => partial,
                    };
                    let res = if let Some(p) = field_partial.as_kwarg(py) {
                        let kwargs = PyDict::new(py);
                        kwargs.set_item(intern!(py, "partial"), p)?;
                        bound.call_method(
                            intern!(py, "deserialize"),
                            (value, attr_name.bind(py), data),
                            Some(&kwargs),
                        )
                    } else {
                        bound.call_method1(
                            intern!(py, "deserialize"),
                            (value, attr_name.bind(py), data),
                        )
                    }
                    .map_err(|e| to_fallback(py, e))?;
                    if res.is(missing) {
                        continue;
                    }
                    set_load_value(py, &out, out_key.bind(py), out_key_parts, &res)?;
                }
            }
        }
        if self.unknown == UNKNOWN_RAISE {
            let known = self.known_keys.bind(py);
            for key in data.keys() {
                if !known.contains(&key)? {
                    return Err(fallback()); // unknown field
                }
            }
        } else if self.unknown == UNKNOWN_INCLUDE {
            // Copy unknown keys through with their raw values (``ret_d[key] =
            // data[key]``). marshmallow appends them after the known fields;
            // dict equality is order-insensitive, so input order is fine.
            let known = self.known_keys.bind(py);
            for (key, value) in data.iter() {
                if !known.contains(&key)? {
                    out.set_item(key, value)?;
                }
            }
        }
        Ok(out)
    }

    // ---- SPIKE (Design A): jiter-tree variants of run/run_one ----------------

    pub(crate) fn run_json_tree<'py>(
        &self,
        ctx: &Ctx,
        py: Python<'py>,
        jv: &JsonValue<'_>,
        many: bool,
        partial: &Partial<'py>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if many {
            match jv {
                JsonValue::Array(a) => self.run_many_json(ctx, py, a, partial),
                _ => Err(fallback()), // non-list -> Python's type handling
            }
        } else {
            match jv {
                JsonValue::Object(o) => Ok(self.run_one_json(ctx, py, o, partial)?.into_any()),
                _ => Err(fallback()),
            }
        }
    }

    pub(crate) fn run_many_json<'py>(
        &self,
        ctx: &Ctx,
        py: Python<'py>,
        arr: &JsonArray<'_>,
        partial: &Partial<'py>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let out = PyList::empty(py);
        for item in arr.iter() {
            match item {
                JsonValue::Object(o) => out.append(self.run_one_json(ctx, py, o, partial)?)?,
                _ => return Err(fallback()),
            }
        }
        Ok(out.into_any())
    }

    /// Deserialize one JSON object off the tree. Mirrors ``run_one`` exactly
    /// (missing/partial/default, ``null``, validators, ``set_value``, unknown
    /// handling) but reads fields from the jiter ``JsonObject`` instead of a
    /// ``PyDict``, and converts only the values it keeps. Callback fields are not
    /// modelled here — they defer the whole load to Python.
    ///
    /// One pass buckets the object's keys (via the precomputed
    /// ``data_key_index``) into per-spec slots and collects any unknown keys;
    /// then the specs are applied in order. That is O(fields + keys), where the
    /// previous per-spec ``lookup_last`` scan was O(fields x keys) — the cost that
    /// made wide-schema fused ``loads`` slower than ``json.loads`` + load.
    pub(crate) fn run_one_json<'py, 'j>(
        &self,
        ctx: &Ctx,
        py: Python<'py>,
        obj: &'j JsonObject<'j>,
        partial: &Partial<'py>,
    ) -> PyResult<Bound<'py, PyDict>> {
        // Two specs sharing a data_key can't be reproduced by the slot fill
        // (both read the same input key); defer the whole load. Extremely rare.
        if !self.distinct_data_keys {
            return Err(fallback());
        }
        let missing = ctx.missing.bind(py);
        let out = PyDict::new(py);

        // Pass 1: bucket each input key once. Known keys land in their spec slot
        // (last-wins on duplicates via overwrite); unknown keys are rejected
        // (RAISE) or held for INCLUDE — applied *after* the fields below so the
        // overwrite order matches stock marshmallow.
        let mut slots: Vec<Option<&'j JsonValue<'j>>> = vec![None; self.specs.len()];
        let mut unknown_include: Vec<(&'j str, &'j JsonValue<'j>)> = Vec::new();
        for (key, value) in obj.iter() {
            match self.data_key_index.get(key.as_ref()) {
                Some(&idx) => slots[idx] = Some(value),
                None => match self.unknown {
                    UNKNOWN_RAISE => return Err(fallback()),
                    UNKNOWN_INCLUDE => unknown_include.push((key.as_ref(), value)),
                    _ => {} // EXCLUDE: drop the unknown key
                },
            }
        }

        // Pass 2: apply the specs in declaration order.
        for (i, spec) in self.specs.iter().enumerate() {
            let LoadFieldSpec::Native {
                data_key: _,
                out_key,
                out_key_parts,
                attr_name,
                load_default,
                required,
                allow_none,
                element,
                consumes_partial,
                validators,
            } = spec
            else {
                // Callback field: defer the entire load to Python.
                return Err(fallback());
            };
            let Some(value) = slots[i] else {
                if partial.allows_missing(attr_name.bind(py))? {
                    continue;
                }
                if *required {
                    return Err(fallback());
                }
                let default = load_default.bind(py);
                if !default.is(missing) {
                    set_load_value(py, &out, out_key.bind(py), out_key_parts, default)?;
                }
                continue;
            };
            if matches!(value, JsonValue::Null) {
                if *allow_none {
                    set_load_value(
                        py,
                        &out,
                        out_key.bind(py),
                        out_key_parts,
                        &py.None().into_bound(py),
                    )?;
                    continue;
                }
                return Err(fallback());
            }
            let sub_partial_opt: Option<Partial> = if *consumes_partial {
                match partial {
                    Partial::Coll(_) => Some(partial.derive(py, attr_name.bind(py))?),
                    _ => None,
                }
            } else {
                None
            };
            let field_partial: &Partial = sub_partial_opt.as_ref().unwrap_or(partial);
            let result = element.apply_json(py, ctx, value, field_partial)?;
            for validator in validators {
                match validator.check(&result) {
                    Ok(true) => {}
                    Ok(false) => return Err(fallback()),
                    Err(e) => return Err(to_fallback(py, e)),
                }
            }
            set_load_value(py, &out, out_key.bind(py), out_key_parts, &result)?;
        }

        // Pass 3 (INCLUDE only): copy unknown keys through, after the fields so a
        // collision on an output key resolves the unknown value last, as stock does.
        for (key, value) in unknown_include {
            out.set_item(PyString::new(py, key), json_to_py(py, value)?)?;
        }
        Ok(out)
    }
}
