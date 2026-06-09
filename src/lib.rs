//! Rust acceleration core for marshmallow's `dump` (serialization) path.
//!
//! A `DumpSerializer` is compiled (see `marshmallow._accel`) from a bound
//! `Schema` and replaces the per-object Python `_serialize` loop. The model is
//! recursive:
//!
//! * a [`Serializer`] turns an object into a dict, one [`FieldSpec`] per field;
//! * a [`FieldSpec`] is either *native* (attribute access + an [`Element`]) or a
//!   *callback* that defers to the Python `Field.serialize`;
//! * an [`Element`] is the value->output transform — scalar formatting, a nested
//!   [`Serializer`] (for `Nested`), or a mapped inner element (for `List`).
//!
//! Anything the Rust side does not model natively stays a callback, so the
//! accelerated output is behaviour-identical to pure-Python marshmallow.
//!
//! WARNING: unlike the load path (below), the dump path has **no `AccelFallback`
//! safety net** — a `DumpSerializer` cannot defer to Python mid-serialization.
//! Every native dump `Element` must therefore be *provably* identical to the
//! corresponding `Field._serialize`, and every new one needs a `_dump_both`
//! equivalence test (`tests/test_accel.py`). When in doubt, leave it a callback.

use pyo3::create_exception;
use pyo3::exceptions::{
    PyAttributeError, PyException, PyIndexError, PyKeyError, PyKeyboardInterrupt, PySystemExit,
    PyTypeError,
};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

create_exception!(
    _core,
    AccelFallback,
    PyException,
    "Internal signal: the accelerated load hit an edge case and the caller \
     should fall back to pure-Python marshmallow (so errors/values match exactly)."
);

/// Shared, schema-independent context (sentinels and builtins).
struct Ctx {
    missing: Py<PyAny>,
    int_fn: Py<PyAny>,
    float_fn: Py<PyAny>,
    dict_fn: Py<PyAny>,
}

impl Ctx {
    fn new(py: Python<'_>, missing: Py<PyAny>) -> PyResult<Self> {
        let builtins = py.import("builtins")?;
        Ok(Ctx {
            missing,
            int_fn: builtins.getattr("int")?.unbind(),
            float_fn: builtins.getattr("float")?.unbind(),
            dict_fn: builtins.getattr("dict")?.unbind(),
        })
    }
}

/// A value -> serialized-output transform (mirrors a field's ``_serialize``).
enum Element {
    Passthrough,         // Raw, Boolean
    Str,                 // String, Email, Url
    Int(bool),           // Integer (bool = as_string)
    Float(bool),         // Float (bool = as_string)
    Nested(Box<Serializer>, bool), // Nested (bool = many)
    List(Box<Element>),  // List(inner)
    Uuid,                // UUID -> str(value)
    /// DateTime/Date/Time: a held serialization callable, else ``value.strftime(fmt)``.
    Temporal {
        func: Option<Py<PyAny>>,
        format: Py<PyString>,
    },
    /// Enum: take ``value.value``/``value.name`` then apply the inner element.
    Enum {
        by_value: bool,
        inner: Box<Element>,
    },
    /// Decimal: defer to the field's own ``_serialize`` (intrinsically Python
    /// ``decimal`` formatting), provably identical to the callback path.
    Decimal { serialize: Py<PyAny> },
    /// Dict (no key/value fields): ``dict(value)``.
    Dict,
    /// Constant: always returns the held constant, ignoring the input value.
    Constant { constant: Py<PyAny> },
}

enum FieldSpec {
    Native {
        key: Py<PyString>,
        key_parts: Option<Vec<Py<PyString>>>,
        output_key: Py<PyString>,
        dump_default: Py<PyAny>,
        element: Element,
    },
    Callback {
        name: Py<PyString>,
        output_key: Py<PyString>,
        field: Py<PyAny>,
    },
}

/// One schema level: an accessor (its ``get_attribute``) plus its field specs.
struct Serializer {
    accessor: Py<PyAny>,
    specs: Vec<FieldSpec>,
}

#[pyclass(module = "marshmallow_core._core")]
pub struct DumpSerializer {
    ctx: Ctx,
    root: Serializer,
}

#[pymethods]
impl DumpSerializer {
    /// ``payload`` is ``(accessor, [field_spec, ...])``; see ``marshmallow._accel``.
    #[new]
    fn new(py: Python<'_>, payload: &Bound<'_, PyAny>, missing: Py<PyAny>) -> PyResult<Self> {
        let ctx = Ctx::new(py, missing)?;
        let root = parse_serializer(py, payload)?;
        Ok(DumpSerializer { ctx, root })
    }

    /// Serialize ``obj`` (or each element if ``many``) into a dict (or list).
    fn run<'py>(&self, obj: &Bound<'py, PyAny>, many: bool) -> PyResult<Bound<'py, PyAny>> {
        self.root.run(&self.ctx, obj, many)
    }

    /// Serialize ``obj`` straight to a JSON string, fusing ``dump`` +
    /// ``json.dumps`` and skipping the intermediate Python dict. Byte-for-byte
    /// identical to stdlib ``json.dumps(self._serialize(obj, many))`` for the
    /// cases it handles; it raises ``AccelFallback`` for anything it cannot
    /// reproduce exactly (e.g. an unencodable value or a non-``str`` dict key),
    /// and the caller falls back to ``dump`` + ``json.dumps``.
    fn run_json(&self, obj: &Bound<'_, PyAny>, many: bool) -> PyResult<String> {
        let mut buf = String::new();
        self.root.write_json(&mut buf, &self.ctx, obj, many)?;
        Ok(buf)
    }
}

/// Append ``s`` to ``buf`` as a JSON string literal, matching CPython's
/// ``json.encoder.py_encode_basestring_ascii`` (``ensure_ascii=True``):
/// short escapes for ``" \\ \n \r \t \b \f``, ``\u00XX`` for other control
/// characters, raw bytes for printable ASCII (``0x20..=0x7E``, ``/`` unescaped),
/// and ``\uXXXX`` (surrogate pairs above the BMP) for everything else.
fn json_escape_into(buf: &mut String, s: &str) {
    use std::fmt::Write as _;
    buf.push('"');
    for ch in s.chars() {
        match ch {
            '"' => buf.push_str("\\\""),
            '\\' => buf.push_str("\\\\"),
            '\n' => buf.push_str("\\n"),
            '\r' => buf.push_str("\\r"),
            '\t' => buf.push_str("\\t"),
            '\u{8}' => buf.push_str("\\b"),
            '\u{c}' => buf.push_str("\\f"),
            c if ('\u{20}'..='\u{7e}').contains(&c) => buf.push(c),
            c => {
                let cp = c as u32;
                if cp <= 0xFFFF {
                    let _ = write!(buf, "\\u{cp:04x}");
                } else {
                    let v = cp - 0x10000;
                    let hi = 0xD800 + (v >> 10);
                    let lo = 0xDC00 + (v & 0x3FF);
                    let _ = write!(buf, "\\u{hi:04x}\\u{lo:04x}");
                }
            }
        }
    }
    buf.push('"');
}

/// JSON-encode an arbitrary Python value into ``buf`` exactly as stdlib
/// ``json.dumps`` (default options) would, or raise ``AccelFallback`` for a type
/// it cannot reproduce byte-for-byte (so the caller defers to ``json.dumps``,
/// which then either encodes it or raises the identical ``TypeError``).
fn write_json_value(buf: &mut String, value: &Bound<'_, PyAny>) -> PyResult<()> {
    if value.is_none() {
        buf.push_str("null");
        return Ok(());
    }
    // ``bool`` first: it is an ``int`` subclass.
    if value.is_instance_of::<PyBool>() {
        buf.push_str(if value.is_truthy()? { "true" } else { "false" });
        return Ok(());
    }
    if value.is_exact_instance_of::<PyInt>() {
        buf.push_str(value.str()?.to_str()?); // ``int.__repr__``
        return Ok(());
    }
    if value.is_exact_instance_of::<PyFloat>() {
        let f: f64 = value.extract()?;
        if f.is_nan() {
            buf.push_str("NaN");
        } else if f.is_infinite() {
            buf.push_str(if f > 0.0 { "Infinity" } else { "-Infinity" });
        } else {
            buf.push_str(value.repr()?.to_str()?); // ``float.__repr__`` == json
        }
        return Ok(());
    }
    if value.is_exact_instance_of::<PyString>() {
        json_escape_into(buf, value.cast::<PyString>()?.to_str()?);
        return Ok(());
    }
    if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        buf.push('[');
        let mut first = true;
        for item in value.try_iter()? {
            if !first {
                buf.push_str(", ");
            }
            first = false;
            write_json_value(buf, &item?)?;
        }
        buf.push(']');
        return Ok(());
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        buf.push('{');
        let mut first = true;
        for (k, v) in dict.iter() {
            // json.dumps coerces int/float/bool/None keys; defer those rare
            // cases to stdlib so we never mis-order or mis-format a key.
            let key = k.cast::<PyString>().map_err(|_| fallback())?;
            if !first {
                buf.push_str(", ");
            }
            first = false;
            json_escape_into(buf, key.to_str()?);
            buf.push_str(": ");
            write_json_value(buf, &v)?;
        }
        buf.push('}');
        return Ok(());
    }
    Err(fallback()) // unencodable type (Decimal, datetime, custom, ...) -> defer
}

impl Serializer {
    fn run<'py>(
        &self,
        ctx: &Ctx,
        obj: &Bound<'py, PyAny>,
        many: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = obj.py();
        if many && !obj.is_none() {
            let out = PyList::empty(py);
            for item in obj.try_iter()? {
                out.append(self.run_one(ctx, &item?)?)?;
            }
            Ok(out.into_any())
        } else {
            Ok(self.run_one(ctx, obj)?.into_any())
        }
    }

    fn run_one<'py>(&self, ctx: &Ctx, obj: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
        let py = obj.py();
        let missing = ctx.missing.bind(py);
        let dict = PyDict::new(py);
        let accessor = self.accessor.bind(py);
        for spec in &self.specs {
            match spec {
                FieldSpec::Callback {
                    name,
                    output_key,
                    field,
                } => {
                    let val = field.bind(py).call_method1(
                        intern!(py, "serialize"),
                        (name.bind(py), obj, accessor),
                    )?;
                    if val.is(missing) {
                        continue;
                    }
                    dict.set_item(output_key.bind(py), val)?;
                }
                FieldSpec::Native {
                    key,
                    key_parts,
                    output_key,
                    dump_default,
                    element,
                } => {
                    let mut value = get_value(py, obj, key, key_parts, missing)?;
                    if value.is(missing) {
                        value = dump_default.bind(py).clone();
                    }
                    if value.is(missing) {
                        continue;
                    }
                    let result = element.apply(ctx, &value)?;
                    dict.set_item(output_key.bind(py), result)?;
                }
            }
        }
        Ok(dict)
    }

    /// JSON form of [`Serializer::run`]: write ``obj`` (or each element if
    /// ``many``) as JSON into ``buf``. Mirrors ``run``'s ``many``/None handling.
    fn write_json(
        &self,
        buf: &mut String,
        ctx: &Ctx,
        obj: &Bound<'_, PyAny>,
        many: bool,
    ) -> PyResult<()> {
        if many && !obj.is_none() {
            buf.push('[');
            let mut first = true;
            for item in obj.try_iter()? {
                if !first {
                    buf.push_str(", ");
                }
                first = false;
                self.write_json_one(buf, ctx, &item?)?;
            }
            buf.push(']');
            Ok(())
        } else {
            self.write_json_one(buf, ctx, obj)
        }
    }

    /// JSON form of [`Serializer::run_one`]: write one object as a JSON object.
    fn write_json_one(&self, buf: &mut String, ctx: &Ctx, obj: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = obj.py();
        let missing = ctx.missing.bind(py);
        let accessor = self.accessor.bind(py);
        buf.push('{');
        let mut first = true;
        for spec in &self.specs {
            match spec {
                FieldSpec::Callback {
                    name,
                    output_key,
                    field,
                } => {
                    let val = field.bind(py).call_method1(
                        intern!(py, "serialize"),
                        (name.bind(py), obj, accessor),
                    )?;
                    if val.is(missing) {
                        continue;
                    }
                    if !first {
                        buf.push_str(", ");
                    }
                    first = false;
                    json_escape_into(buf, output_key.bind(py).to_str()?);
                    buf.push_str(": ");
                    write_json_value(buf, &val)?;
                }
                FieldSpec::Native {
                    key,
                    key_parts,
                    output_key,
                    dump_default,
                    element,
                } => {
                    let mut value = get_value(py, obj, key, key_parts, missing)?;
                    if value.is(missing) {
                        value = dump_default.bind(py).clone();
                    }
                    if value.is(missing) {
                        continue;
                    }
                    if !first {
                        buf.push_str(", ");
                    }
                    first = false;
                    json_escape_into(buf, output_key.bind(py).to_str()?);
                    buf.push_str(": ");
                    element.write_json(buf, ctx, &value)?;
                }
            }
        }
        buf.push('}');
        Ok(())
    }
}

impl Element {
    fn apply<'py>(&self, ctx: &Ctx, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        match self {
            Element::Passthrough => Ok(value.clone()),
            Element::Str => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                if value.is_instance_of::<PyBytes>() {
                    value.call_method1(intern!(py, "decode"), (intern!(py, "utf-8"),))
                } else {
                    Ok(value.str()?.into_any())
                }
            }
            Element::Int(as_string) => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                let r = ctx.int_fn.bind(py).call1((value,))?;
                if *as_string {
                    Ok(r.str()?.into_any())
                } else {
                    Ok(r)
                }
            }
            Element::Float(as_string) => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                let r = ctx.float_fn.bind(py).call1((value,))?;
                if *as_string {
                    Ok(r.str()?.into_any())
                } else {
                    Ok(r)
                }
            }
            Element::Nested(serializer, many) => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                serializer.run(ctx, value, *many)
            }
            Element::List(inner) => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                let out = PyList::empty(py);
                for each in value.try_iter()? {
                    out.append(inner.apply(ctx, &each?)?)?;
                }
                Ok(out.into_any())
            }
            Element::Uuid => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                Ok(value.str()?.into_any())
            }
            Element::Temporal { func, format } => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                match func {
                    Some(f) => f.bind(py).call1((value,)),
                    None => value.call_method1(intern!(py, "strftime"), (format.bind(py),)),
                }
            }
            Element::Enum { by_value, inner } => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                let member = if *by_value {
                    value.getattr(intern!(py, "value"))?
                } else {
                    value.getattr(intern!(py, "name"))?
                };
                inner.apply(ctx, &member)
            }
            Element::Decimal { serialize } => {
                // ``_serialize`` itself returns ``None`` for ``None``; calling it
                // is byte-for-byte the callback path's ``_serialize``.
                serialize
                    .bind(py)
                    .call1((value, py.None(), py.None()))
            }
            Element::Dict => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                ctx.dict_fn.bind(py).call1((value,)) // ``self.mapping_type(value)``
            }
            Element::Constant { constant } => Ok(constant.bind(py).clone()),
        }
    }

    /// JSON form of [`Element::apply`]: write the serialized value as JSON into
    /// ``buf``. ``Nested``/``List`` recurse structurally (no intermediate dict);
    /// every other element computes its serialized value via ``apply`` and hands
    /// it to [`write_json_value`], so the JSON output is exactly
    /// ``json.dumps(self.apply(value))``.
    fn write_json(&self, buf: &mut String, ctx: &Ctx, value: &Bound<'_, PyAny>) -> PyResult<()> {
        match self {
            Element::Nested(serializer, many) => {
                if value.is_none() {
                    buf.push_str("null");
                    Ok(())
                } else {
                    serializer.write_json(buf, ctx, value, *many)
                }
            }
            Element::List(inner) => {
                if value.is_none() {
                    buf.push_str("null");
                    return Ok(());
                }
                buf.push('[');
                let mut first = true;
                for each in value.try_iter()? {
                    if !first {
                        buf.push_str(", ");
                    }
                    first = false;
                    inner.write_json(buf, ctx, &each?)?;
                }
                buf.push(']');
                Ok(())
            }
            _ => {
                let serialized = self.apply(ctx, value)?;
                write_json_value(buf, &serialized)
            }
        }
    }
}

// ---- Construction from the Python spec tuples ------------------------------

fn parse_serializer(py: Python<'_>, payload: &Bound<'_, PyAny>) -> PyResult<Serializer> {
    let t = payload.cast::<PyTuple>()?;
    let accessor = t.get_item(0)?.unbind();
    let specs_list = t.get_item(1)?.cast_into::<PyList>()?;
    let mut specs = Vec::with_capacity(specs_list.len());
    for item in specs_list.iter() {
        specs.push(parse_field_spec(py, &item)?);
    }
    Ok(Serializer { accessor, specs })
}

fn parse_field_spec(py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<FieldSpec> {
    let t = item.cast::<PyTuple>()?;
    let is_callback: bool = t.get_item(0)?.extract()?;
    let output_key = t.get_item(1)?.cast_into::<PyString>()?.unbind();
    if is_callback {
        // (True, output_key, attr_name, field)
        Ok(FieldSpec::Callback {
            name: t.get_item(2)?.cast_into::<PyString>()?.unbind(),
            output_key,
            field: t.get_item(3)?.unbind(),
        })
    } else {
        // (False, output_key, key, dump_default, element)
        let key = t.get_item(2)?.cast_into::<PyString>()?;
        let dump_default = t.get_item(3)?.unbind();
        let element = parse_element(py, &t.get_item(4)?)?;
        let key_str = key.to_str()?;
        let key_parts = if key_str.contains('.') {
            Some(
                key_str
                    .split('.')
                    .map(|s| PyString::new(py, s).unbind())
                    .collect(),
            )
        } else {
            None
        };
        Ok(FieldSpec::Native {
            key: key.unbind(),
            key_parts,
            output_key,
            dump_default,
            element,
        })
    }
}

fn parse_element(py: Python<'_>, e: &Bound<'_, PyAny>) -> PyResult<Element> {
    let t = e.cast::<PyTuple>()?;
    let tag: u8 = t.get_item(0)?.extract()?;
    match tag {
        0 => Ok(Element::Passthrough),
        1 => Ok(Element::Str),
        2 => Ok(Element::Int(t.get_item(1)?.extract()?)),
        3 => Ok(Element::Float(t.get_item(1)?.extract()?)),
        4 => {
            // (4, payload, many)
            let serializer = parse_serializer(py, &t.get_item(1)?)?;
            let many: bool = t.get_item(2)?.extract()?;
            Ok(Element::Nested(Box::new(serializer), many))
        }
        5 => {
            // (5, inner_element)
            let inner = parse_element(py, &t.get_item(1)?)?;
            Ok(Element::List(Box::new(inner)))
        }
        6 => Ok(Element::Uuid),
        7 => {
            // (7, func_or_None, format_str)
            let func_obj = t.get_item(1)?;
            let func = if func_obj.is_none() {
                None
            } else {
                Some(func_obj.unbind())
            };
            let format = t.get_item(2)?.cast_into::<PyString>()?.unbind();
            Ok(Element::Temporal { func, format })
        }
        8 => {
            // (8, by_value, inner_element)
            let by_value: bool = t.get_item(1)?.extract()?;
            let inner = parse_element(py, &t.get_item(2)?)?;
            Ok(Element::Enum {
                by_value,
                inner: Box::new(inner),
            })
        }
        9 => Ok(Element::Decimal {
            // (9, bound _serialize)
            serialize: t.get_item(1)?.unbind(),
        }),
        10 => Ok(Element::Dict), // (10,)
        11 => Ok(Element::Constant {
            // (11, constant)
            constant: t.get_item(1)?.unbind(),
        }),
        other => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "unknown element tag {other}"
        ))),
    }
}

/// Replicates `marshmallow.utils.get_value(obj, key, missing)`, dotted-path aware.
fn get_value<'py>(
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
    if obj.hasattr(intern!(py, "__getitem__"))? {
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

// ===========================================================================
// Load (deserialization) acceleration
// ===========================================================================
//
// The load accelerator handles only the *happy path* — valid input through a
// schema with no hooks/validators/partial. The instant it meets anything off
// the happy path (a coercion failure, a missing ``required`` field, an unknown
// key under ``RAISE``, a non-dict, a callback field raising, ...) it raises
// [`AccelFallback`], and the Python caller re-runs the pure-Python
// ``_do_load``. That keeps every error message and edge-case value byte-for-byte
// identical to pure-Python marshmallow while accelerating the common case.

const UNKNOWN_RAISE: u8 = 0;
// EXCLUDE (1) needs no Rust-side handling (unknown keys are simply ignored), so
// it is not named here.
const UNKNOWN_INCLUDE: u8 = 2;

#[inline]
fn fallback() -> PyErr {
    AccelFallback::new_err(())
}

/// Turn a Python error raised while running user/Python code into an
/// ``AccelFallback`` so the caller re-runs the pure-Python path — *unless* it is
/// a ``KeyboardInterrupt`` or ``SystemExit``. Those must propagate unchanged
/// rather than be swallowed and silently retried on the pure-Python path.
#[inline]
fn to_fallback(py: Python<'_>, err: PyErr) -> PyErr {
    if err.is_instance_of::<PyKeyboardInterrupt>(py) || err.is_instance_of::<PySystemExit>(py) {
        err
    } else {
        fallback()
    }
}

/// A value -> deserialized-value transform (mirrors a field's ``_deserialize``).
enum LoadElement {
    Passthrough, // Raw
    Str,         // String
    Int,         // Integer (non-strict)
    Float { allow_nan: bool },
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
    Uuid { uuid_class: Py<PyAny> },
    /// DateTime/Date/Time: pass through an existing instance of ``internal_type``,
    /// else apply the held ``DESERIALIZATION_FUNCS[format]`` callable.
    Temporal {
        internal_type: Py<PyAny>,
        func: Py<PyAny>,
    },
    /// Decimal: defer to the field's own ``_deserialize`` (``_validated``);
    /// any ``ValidationError`` becomes ``AccelFallback``.
    Decimal { deserialize: Py<PyAny> },
    /// Dict (no key/value fields): copy a dict input via ``dict(value)``; a
    /// non-dict input defers (Python decides Mapping-or-``invalid``).
    Dict,
    /// Constant: always returns the held constant, ignoring the input value.
    Constant { constant: Py<PyAny> },
}

/// A recognized ``marshmallow.validate`` validator, modelled to reproduce only
/// its *pass/fail decision* (mirrors `marshmallow_core._compiler._build_validator`).
/// On failure the field raises ``AccelFallback`` and Python re-runs the validator
/// to emit the exact (possibly custom) error message.
enum Validator {
    /// ``Range``: fail if below ``min`` or above ``max`` (inclusivity per flag).
    Range {
        min: Option<Py<PyAny>>,
        max: Option<Py<PyAny>>,
        min_inclusive: bool,
        max_inclusive: bool,
    },
    /// ``Length``: fail unless ``len(value)`` satisfies equal / min / max.
    Length {
        min: Option<i64>,
        max: Option<i64>,
        equal: Option<i64>,
    },
    /// ``OneOf``: fail unless ``value in choices``.
    OneOf { choices: Py<PyAny> },
}

enum LoadFieldSpec {
    Native {
        data_key: Py<PyString>, // key read from the input mapping
        out_key: Py<PyString>,  // key written to the output dict
        out_key_parts: Option<Vec<Py<PyString>>>, // Some if dotted (set_value)
        load_default: Py<PyAny>,
        required: bool,
        allow_none: bool,
        element: LoadElement,
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

/// Split ``key`` into dotted parts, or ``None`` if it contains no ``.``.
fn split_key_parts(
    py: Python<'_>,
    key: &Bound<'_, PyString>,
) -> PyResult<Option<Vec<Py<PyString>>>> {
    let s = key.to_str()?;
    if s.contains('.') {
        Ok(Some(
            s.split('.').map(|p| PyString::new(py, p).unbind()).collect(),
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
struct LoadSerializer {
    specs: Vec<LoadFieldSpec>,
    many: bool,
    unknown: u8, // UNKNOWN_RAISE | UNKNOWN_EXCLUDE
    known_keys: Py<PyAny>, // frozenset of data keys (only consulted when RAISE)
}

#[pyclass(module = "marshmallow_core._core")]
pub struct LoadDeserializer {
    ctx: Ctx,
    root: LoadSerializer,
}

#[pymethods]
impl LoadDeserializer {
    /// ``payload`` is ``(many, unknown, known_keys, [field_spec, ...])``.
    #[new]
    fn new(py: Python<'_>, payload: &Bound<'_, PyAny>, missing: Py<PyAny>) -> PyResult<Self> {
        let ctx = Ctx::new(py, missing)?;
        let root = parse_load_serializer(py, payload)?;
        Ok(LoadDeserializer { ctx, root })
    }

    /// Deserialize ``data``; raises ``AccelFallback`` to defer to Python.
    ///
    /// ``partial`` mirrors ``load(partial=True)``: a field missing from the input
    /// is skipped (no default applied, no ``required`` error), recursively into
    /// nested schemas. Only the boolean form is modelled; collection/dotted
    /// ``partial`` stays on the pure-Python path (handled by the caller).
    #[pyo3(signature = (data, many, partial=false))]
    fn run<'py>(
        &self,
        data: &Bound<'py, PyAny>,
        many: bool,
        partial: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.root.run(&self.ctx, data, many, partial)
    }
}

impl LoadSerializer {
    fn run<'py>(
        &self,
        ctx: &Ctx,
        data: &Bound<'py, PyAny>,
        many: bool,
        partial: bool,
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

    fn run_one<'py>(
        &self,
        ctx: &Ctx,
        data: &Bound<'py, PyAny>,
        partial: bool,
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
                    load_default,
                    required,
                    allow_none,
                    element,
                    validators,
                } => {
                    let raw = data.get_item(data_key.bind(py))?;
                    let Some(value) = raw else {
                        // missing from input
                        if partial {
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
                    let result = element.apply(ctx, &value, partial)?;
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
                            if partial {
                                continue; // ``partial``: skip the missing field
                            }
                            missing.clone()
                        }
                    };
                    // Any error (ValidationError, TypeError, ...) -> fall back so
                    // Python accumulates the full, correct error structure. Pass
                    // ``partial`` down so a callback ``Nested`` propagates it.
                    let bound = field.bind(py);
                    let res = if partial {
                        let kwargs = PyDict::new(py);
                        kwargs.set_item(intern!(py, "partial"), true)?;
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
}

impl Validator {
    /// Return ``Ok(true)`` if ``value`` passes, ``Ok(false)`` if it fails (the
    /// caller then raises ``AccelFallback``). A comparison/``len``/``in`` that
    /// itself raises is propagated as ``Err`` and turned into a fallback too.
    fn check(&self, value: &Bound<'_, PyAny>) -> PyResult<bool> {
        let py = value.py();
        match self {
            Validator::Range {
                min,
                max,
                min_inclusive,
                max_inclusive,
            } => {
                if let Some(m) = min {
                    let m = m.bind(py);
                    let below = if *min_inclusive {
                        value.lt(m)?
                    } else {
                        value.le(m)?
                    };
                    if below {
                        return Ok(false);
                    }
                }
                if let Some(m) = max {
                    let m = m.bind(py);
                    let above = if *max_inclusive {
                        value.gt(m)?
                    } else {
                        value.ge(m)?
                    };
                    if above {
                        return Ok(false);
                    }
                }
                Ok(true)
            }
            Validator::Length { min, max, equal } => {
                let length = value.len()? as i64;
                if let Some(e) = equal {
                    return Ok(length == *e);
                }
                if let Some(m) = min {
                    if length < *m {
                        return Ok(false);
                    }
                }
                if let Some(m) = max {
                    if length > *m {
                        return Ok(false);
                    }
                }
                Ok(true)
            }
            Validator::OneOf { choices } => choices.bind(py).contains(value),
        }
    }
}

impl LoadElement {
    fn apply<'py>(
        &self,
        ctx: &Ctx,
        value: &Bound<'py, PyAny>,
        partial: bool,
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
                if value.is_instance_of::<pyo3::types::PyBool>() {
                    return Err(fallback()); // bools are rejected as ``invalid``
                }
                ctx.int_fn
                    .bind(py)
                    .call1((value,))
                    .map_err(|e| to_fallback(py, e))
            }
            LoadElement::Float { allow_nan } => {
                if value.is_instance_of::<pyo3::types::PyBool>() {
                    return Err(fallback());
                }
                let r = ctx
                    .float_fn
                    .bind(py)
                    .call1((value,))
                    .map_err(|e| to_fallback(py, e))?;
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
                let out = PyList::empty(py);
                for each in value.try_iter()? {
                    let each = each?;
                    if each.is_none() {
                        if *inner_allow_none {
                            out.append(py.None())?;
                            continue;
                        }
                        return Err(fallback());
                    }
                    out.append(inner.apply(ctx, &each, partial)?)?;
                }
                Ok(out.into_any())
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
                func.bind(py).call1((value,)).map_err(|e| to_fallback(py, e))
            }
            LoadElement::Decimal { deserialize } => deserialize
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
            LoadElement::Constant { constant } => Ok(constant.bind(py).clone()),
        }
    }
}

/// list/tuple only — conservatively falls back for other iterables so we never
/// mis-iterate a ``Mapping`` (which ``is_collection`` excludes) or a generator.
fn is_list_like(val: &Bound<'_, PyAny>) -> bool {
    val.is_instance_of::<PyList>() || val.is_instance_of::<PyTuple>()
}

fn parse_load_serializer(py: Python<'_>, payload: &Bound<'_, PyAny>) -> PyResult<LoadSerializer> {
    let t = payload.cast::<PyTuple>()?;
    let many: bool = t.get_item(0)?.extract()?;
    let unknown: u8 = t.get_item(1)?.extract()?;
    let known_keys = t.get_item(2)?.unbind();
    let specs_list = t.get_item(3)?.cast_into::<PyList>()?;
    let mut specs = Vec::with_capacity(specs_list.len());
    for item in specs_list.iter() {
        specs.push(parse_load_field_spec(py, &item)?);
    }
    Ok(LoadSerializer {
        specs,
        many,
        unknown,
        known_keys,
    })
}

fn parse_load_field_spec(py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<LoadFieldSpec> {
    let t = item.cast::<PyTuple>()?;
    let is_callback: bool = t.get_item(0)?.extract()?;
    if is_callback {
        // (True, data_key, attr_name, out_key, field)
        let out_key = t.get_item(3)?.cast_into::<PyString>()?;
        let out_key_parts = split_key_parts(py, &out_key)?;
        Ok(LoadFieldSpec::Callback {
            data_key: t.get_item(1)?.cast_into::<PyString>()?.unbind(),
            attr_name: t.get_item(2)?.cast_into::<PyString>()?.unbind(),
            out_key: out_key.unbind(),
            out_key_parts,
            field: t.get_item(4)?.unbind(),
        })
    } else {
        // (False, data_key, out_key, load_default, required, allow_none,
        //  element, [validator, ...])
        let validators_list = t.get_item(7)?.cast_into::<PyList>()?;
        let mut validators = Vec::with_capacity(validators_list.len());
        for item in validators_list.iter() {
            validators.push(parse_validator(py, &item)?);
        }
        let out_key = t.get_item(2)?.cast_into::<PyString>()?;
        let out_key_parts = split_key_parts(py, &out_key)?;
        Ok(LoadFieldSpec::Native {
            data_key: t.get_item(1)?.cast_into::<PyString>()?.unbind(),
            out_key: out_key.unbind(),
            out_key_parts,
            load_default: t.get_item(3)?.unbind(),
            required: t.get_item(4)?.extract()?,
            allow_none: t.get_item(5)?.extract()?,
            element: parse_load_element(py, &t.get_item(6)?)?,
            validators,
        })
    }
}

/// Parse a validator spec tuple (see ``_compiler._build_validator``).
fn parse_validator(_py: Python<'_>, v: &Bound<'_, PyAny>) -> PyResult<Validator> {
    let t = v.cast::<PyTuple>()?;
    let tag: u8 = t.get_item(0)?.extract()?;
    match tag {
        0 => {
            // (0, min_or_None, max_or_None, min_inclusive, max_inclusive)
            let min_obj = t.get_item(1)?;
            let max_obj = t.get_item(2)?;
            Ok(Validator::Range {
                min: if min_obj.is_none() {
                    None
                } else {
                    Some(min_obj.unbind())
                },
                max: if max_obj.is_none() {
                    None
                } else {
                    Some(max_obj.unbind())
                },
                min_inclusive: t.get_item(3)?.extract()?,
                max_inclusive: t.get_item(4)?.extract()?,
            })
        }
        1 => {
            // (1, min_or_None, max_or_None, equal_or_None)
            let parse_opt = |i: usize| -> PyResult<Option<i64>> {
                let o = t.get_item(i)?;
                if o.is_none() {
                    Ok(None)
                } else {
                    Ok(Some(o.extract()?))
                }
            };
            Ok(Validator::Length {
                min: parse_opt(1)?,
                max: parse_opt(2)?,
                equal: parse_opt(3)?,
            })
        }
        2 => {
            // (2, choices)
            Ok(Validator::OneOf {
                choices: t.get_item(1)?.unbind(),
            })
        }
        other => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "unknown validator tag {other}"
        ))),
    }
}

fn parse_load_element(py: Python<'_>, e: &Bound<'_, PyAny>) -> PyResult<LoadElement> {
    let t = e.cast::<PyTuple>()?;
    let tag: u8 = t.get_item(0)?.extract()?;
    match tag {
        0 => Ok(LoadElement::Passthrough),
        1 => Ok(LoadElement::Str),
        2 => Ok(LoadElement::Int),
        3 => Ok(LoadElement::Float {
            allow_nan: t.get_item(1)?.extract()?,
        }),
        4 => {
            // (4, payload)
            let serializer = parse_load_serializer(py, &t.get_item(1)?)?;
            Ok(LoadElement::Nested(Box::new(serializer)))
        }
        5 => {
            // (5, inner_element, inner_allow_none)
            let inner = parse_load_element(py, &t.get_item(1)?)?;
            let inner_allow_none: bool = t.get_item(2)?.extract()?;
            Ok(LoadElement::List(Box::new(inner), inner_allow_none))
        }
        6 => {
            // (6, enum_class, by_value, inner_element)
            let enum_class = t.get_item(1)?.unbind();
            let by_value: bool = t.get_item(2)?.extract()?;
            let inner = parse_load_element(py, &t.get_item(3)?)?;
            Ok(LoadElement::Enum {
                enum_class,
                by_value,
                inner: Box::new(inner),
            })
        }
        7 => {
            // (7, uuid_class)
            Ok(LoadElement::Uuid {
                uuid_class: t.get_item(1)?.unbind(),
            })
        }
        8 => {
            // (8, internal_type, func)
            Ok(LoadElement::Temporal {
                internal_type: t.get_item(1)?.unbind(),
                func: t.get_item(2)?.unbind(),
            })
        }
        9 => Ok(LoadElement::Decimal {
            // (9, bound _deserialize)
            deserialize: t.get_item(1)?.unbind(),
        }),
        10 => Ok(LoadElement::Dict), // (10,)
        11 => Ok(LoadElement::Constant {
            // (11, constant)
            constant: t.get_item(1)?.unbind(),
        }),
        other => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "unknown load element tag {other}"
        ))),
    }
}

/// Wire-format/ABI version of the payloads exchanged with ``marshmallow._accel``.
/// Bump this whenever the element tags or payload tuple shapes change so a stale
/// compiled extension paired with a newer ``marshmallow`` (or vice versa) is
/// detected and the pure-Python path is used instead of misreading payloads.
const PROTOCOL_VERSION: u32 = 3;

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<DumpSerializer>()?;
    m.add_class::<LoadDeserializer>()?;
    m.add("AccelFallback", m.py().get_type::<AccelFallback>())?;
    m.add("PROTOCOL_VERSION", PROTOCOL_VERSION)?;
    Ok(())
}
