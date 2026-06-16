//! The load value model: [`LoadElement`] (a value→deserialized-value transform),
//! [`LoadFieldSpec`] (a field's native/callback spec), and their evaluation from
//! both a Python ``dict`` source ([`LoadElement::apply`]) and directly off a
//! jiter ``JsonValue`` tree ([`LoadElement::apply_json`]).

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use jiter::{cached_py_string, JsonValue};

use crate::context::{fallback, is_list_like, to_fallback, Ctx};
use crate::load::json_tree::json_to_py;
use crate::load::serializer::LoadSerializer;
use crate::load::validators::{check_validators, Partial, Validator};

/// A value -> deserialized-value transform (mirrors a field's ``_deserialize``).
pub(crate) enum LoadElement {
    Passthrough, // Raw
    Str,         // String
    Int,         // Integer (non-strict)
    /// Integer(strict=True): accept an exact ``int`` as-is; anything else (an
    /// ``Integral`` subclass to coerce, or an invalid value) defers to Python.
    IntStrict,
    Float {
        allow_nan: bool,
    },
    Nested(Box<LoadSerializer>),
    /// List(inner_element, inner_allow_none) — mirrors ``inner.deserialize``.
    List(Box<LoadElement>, bool),
    /// Enum: deserialize ``value`` via the inner field, then ``enum(val)``
    /// (by value) or ``enum[val]`` (by name).
    Enum {
        enum_class: Py<PyAny>,
        by_value: bool,
        inner: Box<LoadElement>,
    },
    /// UUID: pass through an existing ``uuid.UUID``, else ``uuid.UUID(value)``
    /// (with the 16-byte ``bytes=`` special case).
    Uuid {
        uuid_class: Py<PyAny>,
    },
    /// DateTime/Date/Time: pass through an existing instance of ``internal_type``,
    /// else apply the held ``DESERIALIZATION_FUNCS[format]`` callable.
    Temporal {
        internal_type: Py<PyAny>,
        func: Py<PyAny>,
    },
    /// Decimal: defer to the field's own ``_deserialize`` (``_validated``);
    /// any ``ValidationError`` becomes ``AccelFallback``.
    Decimal {
        deserialize: Py<PyAny>,
    },
    /// TimeDelta: defer to the field's own ``_deserialize`` (float -> timedelta);
    /// any ``ValidationError`` becomes ``AccelFallback``.
    TimeDelta {
        deserialize: Py<PyAny>,
    },
    /// NaiveDateTime/AwareDateTime: defer to the field's own ``_deserialize``
    /// (parse + timezone-awareness check); any ``ValidationError`` -> fallback.
    DatetimeAwareness {
        deserialize: Py<PyAny>,
    },
    /// IP/IPv4/IPv6/IPInterface/...: defer to the field's own ``_deserialize``
    /// (``ensure_text_type`` + the held ``ipaddress`` ctor); any error -> fallback.
    IpAddr {
        deserialize: Py<PyAny>,
    },
    /// Dict (no key/value fields): copy a dict input via ``dict(value)``; a
    /// non-dict input defers (Python decides Mapping-or-``invalid``).
    Dict,
    /// Typed Dict: apply the key/value field per entry (``None`` = pass through).
    /// Defers on a non-dict input, a ``None`` key/value, or any per-entry error so
    /// Python re-runs and accumulates the exact error structure.
    DictTyped {
        key_el: Option<Box<LoadElement>>,
        key_validators: Vec<Validator>,
        val_el: Option<Box<LoadElement>>,
        val_validators: Vec<Validator>,
    },
    /// Constant: always returns the held constant, ignoring the input value.
    Constant {
        constant: Py<PyAny>,
    },
    /// Tuple: a fixed-length sequence, one element per position. Defers on a
    /// non-sequence, a length mismatch, a ``None`` element, or any per-element
    /// error so Python re-runs with the exact messages.
    Tuple(Vec<LoadElement>),
    /// Pluck: wrap the scalar as ``{data_key: value}`` (per item when ``many``)
    /// and run the inner ``only=(field_name,)`` schema's single-record load.
    Pluck {
        serializer: Box<LoadSerializer>,
        data_key: Py<PyString>,
        many: bool,
    },
    /// Boolean: ``value in truthy -> True``, ``value in falsy -> False``; a miss
    /// (or a ``TypeError`` from an unhashable value) defers so Python raises the
    /// exact ``invalid`` error. Holds the field's own ``truthy``/``falsy`` sets.
    Boolean {
        truthy: Py<PyAny>,
        falsy: Py<PyAny>,
    },
    /// Nested schema with only ``@post_load`` hooks: deserialize the inner fields
    /// natively in Rust (building a plain dict), then call the Python
    /// ``post_load_fn`` callable once per record to produce the final value.
    /// Eliminates the Python ``field.deserialize → inner.load → _patched_do_load``
    /// call overhead for nested schemas whose only hook is ``@post_load``.
    NestedPostLoad {
        serializer: Box<LoadSerializer>,
        post_load_fn: Py<PyAny>,
    },
}

pub(crate) enum LoadFieldSpec {
    Native {
        data_key: Py<PyString>,                   // key read from the input mapping
        out_key: Py<PyString>,                    // key written to the output dict
        out_key_parts: Option<Vec<Py<PyString>>>, // Some if dotted (set_value)
        attr_name: Py<PyString>,                  // schema attribute name (for the partial check)
        load_default: Py<PyAny>,
        required: bool,
        allow_none: bool,
        element: LoadElement,
        /// True if this element can forward ``partial`` into a nested schema
        /// (``Nested``/``Pluck``, transitively through ``List``/``Tuple``/
        /// ``DictTyped``). When false, ``partial.derive`` is skipped entirely for
        /// this spec (F_SPEEDUP F4: for flat schemas with a ``Coll`` partial,
        /// all specs have ``consumes_partial = false`` and zero derive calls are
        /// made across all records).
        consumes_partial: bool,
        validators: Vec<Validator>,
    },
    Callback {
        data_key: Py<PyString>,
        attr_name: Py<PyString>, // passed to ``field.deserialize``
        out_key: Py<PyString>,
        out_key_parts: Option<Vec<Py<PyString>>>,
        field: Py<PyAny>,
    },
}

impl LoadElement {
    pub(crate) fn apply<'py>(
        &self,
        ctx: &Ctx,
        value: &Bound<'py, PyAny>,
        partial: &Partial<'py>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        match self {
            LoadElement::Passthrough => Ok(value.clone()),
            LoadElement::Str => {
                // ``ensure_text_type``: bytes -> utf-8, else ``str(value)``.
                if value.is_exact_instance_of::<PyString>() {
                    Ok(value.clone())
                } else if value.is_instance_of::<PyBytes>() {
                    value
                        .call_method1(intern!(py, "decode"), (intern!(py, "utf-8"),))
                        .map_err(|e| to_fallback(py, e))
                } else {
                    // non-str/bytes raises ``invalid``; str subclass would be
                    // coerced by ``str()`` — both handled correctly by Python.
                    Err(fallback())
                }
            }
            LoadElement::Int => {
                if value.is_instance_of::<PyBool>() {
                    return Err(fallback()); // bools are rejected as ``invalid``
                }
                // ``int(x)`` for an exact int (bool already excluded above) is
                // ``x`` — skip the Python call on the common already-parsed case.
                if value.is_exact_instance_of::<PyInt>() {
                    return Ok(value.clone());
                }
                ctx.int_fn
                    .bind(py)
                    .call1((value,))
                    .map_err(|e| to_fallback(py, e))
            }
            LoadElement::IntStrict => {
                if value.is_instance_of::<PyBool>() {
                    return Err(fallback()); // bool rejected as ``invalid``
                }
                // Accept an exact int unchanged (``int(x) is x``). Defer
                // everything else: an ``Integral`` subclass that Python would
                // coerce, or an invalid value Python rejects with the exact error.
                if value.is_exact_instance_of::<PyInt>() {
                    Ok(value.clone())
                } else {
                    Err(fallback())
                }
            }
            LoadElement::Float { allow_nan } => {
                if value.is_instance_of::<PyBool>() {
                    return Err(fallback());
                }
                // An exact float is its own ``float(x)``; reuse it directly and
                // run only the nan/inf guard, skipping the ``float`` call.
                let r = if value.is_exact_instance_of::<PyFloat>() {
                    value.clone()
                } else {
                    ctx.float_fn
                        .bind(py)
                        .call1((value,))
                        .map_err(|e| to_fallback(py, e))?
                };
                if !*allow_nan {
                    let f: f64 = r.extract()?;
                    if f.is_nan() || f.is_infinite() {
                        return Err(fallback()); // ``special`` error
                    }
                }
                Ok(r)
            }
            LoadElement::Nested(serializer) => {
                // ``partial`` propagates into nested schemas, matching Python.
                if serializer.many {
                    serializer.run(ctx, value, true, partial)
                } else {
                    Ok(serializer.run_one(ctx, value, partial)?.into_any())
                }
            }
            LoadElement::List(inner, inner_allow_none) => {
                if !is_list_like(value) {
                    return Err(fallback()); // ``invalid`` (not a list)
                }
                // ``is_list_like`` guarantees a list/tuple, so ``len`` is a cheap
                // exact capacity hint; build the list in one allocation.
                let mut items: Vec<Bound<'py, PyAny>> = Vec::with_capacity(value.len()?);
                for each in value.try_iter()? {
                    let each = each?;
                    if each.is_none() {
                        if *inner_allow_none {
                            items.push(py.None().into_bound(py));
                            continue;
                        }
                        return Err(fallback());
                    }
                    items.push(inner.apply(ctx, &each, partial)?);
                }
                Ok(PyList::new(py, items)?.into_any())
            }
            LoadElement::Enum {
                enum_class,
                by_value,
                inner,
            } => {
                let cls = enum_class.bind(py);
                if value.is_instance(cls)? {
                    return Ok(value.clone()); // already an enum member
                }
                let val = inner.apply(ctx, value, partial)?;
                let result = if *by_value {
                    cls.call1((val,)) // ``enum(val)``
                } else {
                    cls.get_item(val) // ``enum[val]``
                };
                result.map_err(|e| to_fallback(py, e))
            }
            LoadElement::Uuid { uuid_class } => {
                let cls = uuid_class.bind(py);
                if value.is_instance(cls)? {
                    return Ok(value.clone()); // already a UUID
                }
                if let Ok(b) = value.cast::<PyBytes>() {
                    if b.len()? == 16 {
                        // ``uuid.UUID(bytes=value)``
                        let kwargs = PyDict::new(py);
                        kwargs.set_item(intern!(py, "bytes"), value)?;
                        return cls.call((), Some(&kwargs)).map_err(|e| to_fallback(py, e));
                    }
                }
                cls.call1((value,)).map_err(|e| to_fallback(py, e)) // ``uuid.UUID(value)``
            }
            LoadElement::Temporal {
                internal_type,
                func,
            } => {
                if value.is_instance(internal_type.bind(py))? {
                    return Ok(value.clone()); // already a datetime/date/time
                }
                func.bind(py)
                    .call1((value,))
                    .map_err(|e| to_fallback(py, e))
            }
            LoadElement::Decimal { deserialize } => deserialize
                .bind(py)
                .call1((value, py.None(), py.None()))
                .map_err(|e| to_fallback(py, e)),
            LoadElement::TimeDelta { deserialize } => deserialize
                .bind(py)
                .call1((value, py.None(), py.None()))
                .map_err(|e| to_fallback(py, e)),
            LoadElement::DatetimeAwareness { deserialize } => deserialize
                .bind(py)
                .call1((value, py.None(), py.None()))
                .map_err(|e| to_fallback(py, e)),
            LoadElement::IpAddr { deserialize } => deserialize
                .bind(py)
                .call1((value, py.None(), py.None()))
                .map_err(|e| to_fallback(py, e)),
            LoadElement::Dict => {
                if value.is_instance_of::<PyDict>() {
                    ctx.dict_fn.bind(py).call1((value,))
                } else {
                    Err(fallback()) // non-dict: defer (Mapping check / ``invalid``)
                }
            }
            LoadElement::DictTyped {
                key_el,
                key_validators,
                val_el,
                val_validators,
            } => {
                // Only an exact dict happy-path; a non-dict Mapping defers so
                // Python applies its ``isinstance(_, Mapping)`` handling.
                let dict = value.cast::<PyDict>().map_err(|_| fallback())?;
                let out = PyDict::new(py);
                for (k, v) in dict.iter() {
                    // ``None`` key/value -> defer (Python honours ``allow_none``).
                    let ko = match key_el {
                        Some(ke) => {
                            if k.is_none() {
                                return Err(fallback());
                            }
                            let r = ke.apply(ctx, &k, partial)?;
                            check_validators(py, key_validators, &r)?;
                            r
                        }
                        None => k,
                    };
                    let vo = match val_el {
                        Some(ve) => {
                            if v.is_none() {
                                return Err(fallback());
                            }
                            let r = ve.apply(ctx, &v, partial)?;
                            check_validators(py, val_validators, &r)?;
                            r
                        }
                        None => v,
                    };
                    out.set_item(ko, vo)?;
                }
                Ok(out.into_any())
            }
            LoadElement::Constant { constant } => Ok(constant.bind(py).clone()),
            LoadElement::Tuple(elements) => {
                if !is_list_like(value) {
                    return Err(fallback()); // not a sequence -> ``invalid``
                }
                // Length mismatch -> defer (``zip(strict=True)`` / ``validate_length``).
                if value.len()? != elements.len() {
                    return Err(fallback());
                }
                let mut items: Vec<Bound<'py, PyAny>> = Vec::with_capacity(elements.len());
                for (element, each) in elements.iter().zip(value.try_iter()?) {
                    let each = each?;
                    if each.is_none() {
                        return Err(fallback()); // honour allow_none in Python
                    }
                    items.push(element.apply(ctx, &each, partial)?);
                }
                Ok(PyTuple::new(py, items)?.into_any())
            }
            LoadElement::Pluck {
                serializer,
                data_key,
                many,
            } => {
                let dk = data_key.bind(py);
                if *many {
                    if !is_list_like(value) {
                        return Err(fallback()); // ``_test_collection`` -> ``invalid``
                    }
                    let out = PyList::empty(py);
                    for v in value.try_iter()? {
                        let tmp = PyDict::new(py);
                        tmp.set_item(dk, v?)?;
                        out.append(serializer.run_one(ctx, tmp.as_any(), partial)?)?;
                    }
                    Ok(out.into_any())
                } else {
                    let tmp = PyDict::new(py);
                    tmp.set_item(dk, value)?;
                    Ok(serializer.run_one(ctx, tmp.as_any(), partial)?.into_any())
                }
            }
            LoadElement::Boolean { truthy, falsy } => {
                // ``value in truthy -> True``, ``value in falsy -> False``. Any
                // miss, or a ``TypeError`` from an unhashable value, defers so
                // Python raises the exact ``invalid`` error (matching the
                // ``try/except TypeError`` in ``Boolean._deserialize``).
                // N2: use ``to_fallback`` (not bare fallback) so KI/SystemExit
                // from ``__hash__``/``__eq__`` propagates instead of being eaten.
                if truthy
                    .bind(py)
                    .contains(value)
                    .map_err(|e| to_fallback(py, e))?
                {
                    Ok(PyBool::new(py, true).to_owned().into_any())
                } else if falsy
                    .bind(py)
                    .contains(value)
                    .map_err(|e| to_fallback(py, e))?
                {
                    Ok(PyBool::new(py, false).to_owned().into_any())
                } else {
                    Err(fallback())
                }
            }
            LoadElement::NestedPostLoad {
                serializer,
                post_load_fn,
            } => {
                // Deserialize the inner fields natively, then call the Python
                // post_load processor. Any error from either step defers to the
                // pure-Python path via AccelFallback; KI/SystemExit propagates.
                let dict = if serializer.many {
                    serializer.run(ctx, value, true, partial)?
                } else {
                    serializer.run_one(ctx, value, partial)?.into_any()
                };
                post_load_fn
                    .bind(py)
                    .call1((dict,))
                    .map_err(|e| to_fallback(py, e))
            }
        }
    }

    /// SPIKE (Design A): deserialize a value straight off the jiter tree.
    ///
    /// ``Nested`` and ``List`` recurse through the tree so a list-of-records
    /// never materialises an intermediate Python ``dict``/``list`` (the whole
    /// point). Hot scalar elements (``Str``, ``Int``, ``Float``, ``Passthrough``,
    /// ``IntStrict``, ``Boolean``) are handled inline without going through
    /// ``json_to_py`` + ``apply`` (F_SPEEDUP F6: eliminates the double dispatch
    /// for the common case). Every other element materialises the leaf via
    /// ``json_to_py`` and delegates to the unchanged ``apply`` path, so scalar
    /// parity holds by construction.
    pub(crate) fn apply_json<'py>(
        &self,
        py: Python<'py>,
        ctx: &Ctx,
        jv: &JsonValue<'_>,
        partial: &Partial<'py>,
    ) -> PyResult<Bound<'py, PyAny>> {
        match self {
            // --- F6: scalar fast paths -------------------------------------------
            // These convert JsonValue → Python object directly, skipping the
            // json_to_py materialisation + apply double-dispatch that the catch-all
            // would use. Each arm matches only the JSON type it can handle natively;
            // any mismatch falls through to the materialise+apply path which handles
            // coercion and error generation identically to the dict-source load path.
            LoadElement::Passthrough => json_to_py(py, jv),

            LoadElement::Str => match jv {
                // JSON strings arrive already as valid UTF-8 &str; use the global
                // string cache (F7) so repeated values share the same Python object.
                JsonValue::Str(s) => Ok(cached_py_string(py, s.as_ref()).into_any()),
                _ => {
                    let v = json_to_py(py, jv)?;
                    self.apply(ctx, &v, partial)
                }
            },

            LoadElement::Int => match jv {
                // Boolean in JSON means ``true``/``false``; ``apply`` rejects them
                // as ``invalid``, defer so Python emits the right message.
                JsonValue::Bool(_) => Err(fallback()),
                JsonValue::Int(i) => Ok((*i).into_pyobject(py)?.into_any()),
                // Float or string coercion: materialise and let ``apply`` handle.
                _ => {
                    let v = json_to_py(py, jv)?;
                    self.apply(ctx, &v, partial)
                }
            },

            LoadElement::IntStrict => match jv {
                JsonValue::Bool(_) => Err(fallback()),
                // Strict: exact int only; any other JSON type defers.
                JsonValue::Int(i) => Ok((*i).into_pyobject(py)?.into_any()),
                _ => Err(fallback()),
            },

            LoadElement::Float { allow_nan } => match jv {
                JsonValue::Bool(_) => Err(fallback()),
                JsonValue::Float(f) => {
                    if !allow_nan && (f.is_nan() || f.is_infinite()) {
                        return Err(fallback()); // ``special`` error
                    }
                    Ok((*f).into_pyobject(py)?.into_any())
                }
                JsonValue::Int(i) => {
                    // JSON integers are valid float inputs (same as ``float(42)``).
                    let f = *i as f64;
                    if !allow_nan && (f.is_nan() || f.is_infinite()) {
                        return Err(fallback());
                    }
                    Ok(f.into_pyobject(py)?.into_any())
                }
                _ => {
                    let v = json_to_py(py, jv)?;
                    self.apply(ctx, &v, partial)
                }
            },

            LoadElement::Boolean { .. } => {
                // JSON booleans can be resolved directly without a Python set lookup.
                match jv {
                    JsonValue::Bool(b) => Ok(PyBool::new(py, *b).to_owned().into_any()),
                    // Non-bool JSON: materialise and fall through to apply which
                    // checks the truthy/falsy sets and defers on a miss.
                    _ => {
                        let v = json_to_py(py, jv)?;
                        self.apply(ctx, &v, partial)
                    }
                }
            }

            // --- end F6 fast paths -----------------------------------------------
            LoadElement::Nested(serializer) => {
                if serializer.many {
                    match jv {
                        JsonValue::Array(a) => serializer.run_many_json(ctx, py, a, partial),
                        _ => Err(fallback()),
                    }
                } else {
                    match jv {
                        JsonValue::Object(o) => {
                            Ok(serializer.run_one_json(ctx, py, o, partial)?.into_any())
                        }
                        _ => Err(fallback()),
                    }
                }
            }
            LoadElement::List(inner, inner_allow_none) => match jv {
                JsonValue::Array(a) => {
                    // Pre-size: the element count is known, so build the list in
                    // one allocation instead of growing it by append.
                    let mut items: Vec<Bound<'py, PyAny>> = Vec::with_capacity(a.len());
                    for item in a.iter() {
                        if matches!(item, JsonValue::Null) {
                            if *inner_allow_none {
                                items.push(py.None().into_bound(py));
                                continue;
                            }
                            return Err(fallback());
                        }
                        items.push(inner.apply_json(py, ctx, item, partial)?);
                    }
                    Ok(PyList::new(py, items)?.into_any())
                }
                _ => Err(fallback()),
            },
            LoadElement::Tuple(elements) => match jv {
                JsonValue::Array(a) => {
                    // Mirror the pure Tuple: non-sequence / length mismatch / a
                    // ``None`` element all defer for the exact message.
                    if a.len() != elements.len() {
                        return Err(fallback());
                    }
                    let mut items: Vec<Bound<'py, PyAny>> = Vec::with_capacity(elements.len());
                    for (element, item) in elements.iter().zip(a.iter()) {
                        if matches!(item, JsonValue::Null) {
                            return Err(fallback());
                        }
                        items.push(element.apply_json(py, ctx, item, partial)?);
                    }
                    Ok(PyTuple::new(py, items)?.into_any())
                }
                _ => Err(fallback()),
            },
            LoadElement::Dict => match jv {
                // Plain Dict (no key/value fields) = ``dict(value)``: a fresh dict
                // copy, which ``json_to_py`` of an object produces exactly.
                JsonValue::Object(_) => json_to_py(py, jv),
                _ => Err(fallback()),
            },
            LoadElement::DictTyped {
                key_el,
                key_validators,
                val_el,
                val_validators,
            } => match jv {
                JsonValue::Object(o) => {
                    let out = PyDict::new(py);
                    for (k, v) in o.iter() {
                        // JSON keys are always strings; apply the key field (+its
                        // validators) to the string key, the value field to the
                        // value subtree. Duplicate keys overwrite (last wins, like
                        // ``json.loads``); any per-entry failure -> fallback.
                        let kstr = PyString::new(py, k.as_ref());
                        let ko = match key_el {
                            Some(ke) => {
                                let r = ke.apply(ctx, kstr.as_any(), partial)?;
                                check_validators(py, key_validators, &r)?;
                                r
                            }
                            None => kstr.into_any(),
                        };
                        let vo = match val_el {
                            Some(ve) => {
                                if matches!(v, JsonValue::Null) {
                                    return Err(fallback());
                                }
                                let r = ve.apply_json(py, ctx, v, partial)?;
                                check_validators(py, val_validators, &r)?;
                                r
                            }
                            None => json_to_py(py, v)?,
                        };
                        out.set_item(ko, vo)?;
                    }
                    Ok(out.into_any())
                }
                _ => Err(fallback()),
            },
            LoadElement::NestedPostLoad {
                serializer,
                post_load_fn,
            } => {
                // Same as the dict path: deserialize inner fields off the JSON
                // tree natively, then call the Python post_load processor.
                let dict = if serializer.many {
                    match jv {
                        JsonValue::Array(a) => serializer.run_many_json(ctx, py, a, partial)?,
                        _ => return Err(fallback()),
                    }
                } else {
                    match jv {
                        JsonValue::Object(o) => {
                            serializer.run_one_json(ctx, py, o, partial)?.into_any()
                        }
                        _ => return Err(fallback()),
                    }
                };
                post_load_fn
                    .bind(py)
                    .call1((dict,))
                    .map_err(|e| to_fallback(py, e))
            }
            // Scalars and the remaining wrappers (Enum/Pluck/Decimal/...): the
            // input is a leaf, so materialise it and reuse the exact pure path.
            _ => {
                let v = json_to_py(py, jv)?;
                self.apply(ctx, &v, partial)
            }
        }
    }
}
