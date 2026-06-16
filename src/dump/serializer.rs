//! The dump value model: [`Element`] (a value→output transform), [`FieldSpec`]
//! (a field's native/callback spec), and [`Serializer`] (one schema level), plus
//! their object→dict and object→JSON evaluation.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::attr_access::get_value;
use crate::context::{fallback, is_list_like, is_one_shot_iterator, Ctx};
use crate::json_writer::write_json_value;
use crate::temporal::{write_temporal_native, TemporalKind};

/// A value -> serialized-output transform (mirrors a field's ``_serialize``).
pub(crate) enum Element {
    Passthrough,                   // Raw, Boolean
    Str,                           // String, Email, Url
    Int(bool),                     // Integer (bool = as_string)
    Float(bool),                   // Float (bool = as_string)
    Nested(Box<Serializer>, bool), // Nested (bool = many)
    List(Box<Element>),            // List(inner)
    Uuid,                          // UUID -> str(value)
    IpAddr,                        // IP/IPv4/IPv6/IPInterface/... -> str(value)
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
    Decimal {
        serialize: Py<PyAny>,
    },
    /// Dict (no key/value fields): ``dict(value)``.
    Dict,
    /// Typed Dict: serialize keys/values via their fields per entry (``None`` =
    /// pass through). Defers on a non-dict input (dump has a fallback).
    DictTyped {
        key_el: Option<Box<Element>>,
        val_el: Option<Box<Element>>,
    },
    /// Constant: always returns the held constant, ignoring the input value.
    Constant {
        constant: Py<PyAny>,
    },
    /// TimeDelta: defer to the field's own ``_serialize`` (precision-sensitive
    /// timedelta -> float), provably identical to the callback path.
    TimeDelta {
        serialize: Py<PyAny>,
    },
    /// Tuple: serialize each position; defers (dump fallback) on a length
    /// mismatch so Python raises the exact ``zip(strict=True)`` error.
    Tuple(Vec<Element>),
    /// Pluck: dump via the inner schema, then extract ``data_key`` (``utils.pluck``
    /// per item when ``many``).
    Pluck {
        serializer: Box<Serializer>,
        data_key: Py<PyString>,
        many: bool,
    },
    /// Native ISO format for ``datetime``/``date``/``time`` — uses C-level struct
    /// accessors instead of calling the Python ``isoformat()`` method.  Requires a
    /// non-abi3 build (struct accessors are not in the limited API).
    TemporalNative(TemporalKind),
}

pub(crate) enum FieldSpec {
    Native {
        key: Py<PyString>,
        key_parts: Option<Vec<Py<PyString>>>,
        output_key: Py<PyString>,
        /// Pre-escaped JSON key prefix: ``"\"name\": "`` built once at compile
        /// time so ``write_json_one`` avoids re-escaping per record (F_SPEEDUP F5).
        json_key: Box<str>,
        dump_default: Py<PyAny>,
        element: Element,
    },
    Callback {
        name: Py<PyString>,
        output_key: Py<PyString>,
        /// Pre-escaped JSON key prefix for the callback case (same optimization).
        json_key: Box<str>,
        field: Py<PyAny>,
    },
}

/// One schema level: an accessor (its ``get_attribute``) plus its field specs.
pub(crate) struct Serializer {
    pub(crate) accessor: Py<PyAny>,
    pub(crate) specs: Vec<FieldSpec>,
}

impl Serializer {
    pub(crate) fn run<'py>(
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
                    ..
                } => {
                    let val = field
                        .bind(py)
                        .call_method1(intern!(py, "serialize"), (name.bind(py), obj, accessor))?;
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
                    ..
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
    pub(crate) fn write_json(
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
                    json_key,
                    field,
                    ..
                } => {
                    let val = field
                        .bind(py)
                        .call_method1(intern!(py, "serialize"), (name.bind(py), obj, accessor))?;
                    if val.is(missing) {
                        continue;
                    }
                    if !first {
                        buf.push_str(", ");
                    }
                    first = false;
                    buf.push_str(json_key); // F5: pre-escaped key prefix
                    write_json_value(buf, &val, 0)?;
                }
                FieldSpec::Native {
                    key,
                    key_parts,
                    json_key,
                    dump_default,
                    element,
                    ..
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
                    buf.push_str(json_key); // F5: pre-escaped key prefix
                    element.write_json(buf, ctx, &value)?;
                }
            }
        }
        buf.push('}');
        Ok(())
    }
}

impl Element {
    pub(crate) fn apply<'py>(
        &self,
        ctx: &Ctx,
        value: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
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
                // ``int(x)`` for an *exact* int returns ``x`` unchanged, so skip
                // the Python call. ``is_exact_instance_of`` excludes ``bool`` and
                // int subclasses, which ``int()`` would still need to coerce.
                if !*as_string && value.is_exact_instance_of::<PyInt>() {
                    return Ok(value.clone());
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
                // ``float(x)`` for an exact float returns ``x`` unchanged.
                if !*as_string && value.is_exact_instance_of::<PyFloat>() {
                    return Ok(value.clone());
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
                // N1: defer before consuming a non-replayable iterator; re-iterable
                // containers (set, range, dict views) stay fast.
                if *many && is_one_shot_iterator(value) {
                    return Err(fallback());
                }
                serializer.run(ctx, value, *many)
            }
            Element::List(inner) => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                // N1: same guard — generators in a List field must not be consumed
                // before a potential later AccelFallback.
                if is_one_shot_iterator(value) {
                    return Err(fallback());
                }
                let out = PyList::empty(py);
                for each in value.try_iter()? {
                    out.append(inner.apply(ctx, &each?)?)?;
                }
                Ok(out.into_any())
            }
            Element::Uuid | Element::IpAddr => {
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
                serialize.bind(py).call1((value, py.None(), py.None()))
            }
            Element::TimeDelta { serialize } => {
                // Like ``Decimal``: the field's own ``_serialize`` (timedelta ->
                // float) is precision-sensitive, so call it directly. It returns
                // ``None`` for ``None``.
                serialize.bind(py).call1((value, py.None(), py.None()))
            }
            Element::Dict => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                ctx.dict_fn.bind(py).call1((value,)) // ``self.mapping_type(value)``
            }
            Element::DictTyped { key_el, val_el } => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                // Non-dict input -> defer (dump fallback re-runs pure Python,
                // which handles any ``Mapping`` or raises the same error).
                let dict = value.cast::<PyDict>().map_err(|_| fallback())?;
                let out = PyDict::new(py);
                for (k, v) in dict.iter() {
                    // Mirrors ``key_field._serialize(k, None, None)`` /
                    // ``value_field._serialize(v, None, None)``; ``None`` = pass.
                    let ko = match key_el {
                        Some(ke) => ke.apply(ctx, &k)?,
                        None => k,
                    };
                    let vo = match val_el {
                        Some(ve) => ve.apply(ctx, &v)?,
                        None => v,
                    };
                    out.set_item(ko, vo)?;
                }
                Ok(out.into_any())
            }
            Element::Constant { constant } => Ok(constant.bind(py).clone()),
            Element::Tuple(elements) => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                // Defer on a non-sequence or length mismatch; pure Python then
                // raises the exact ``zip(strict=True)`` error.
                if !is_list_like(value) || value.len()? != elements.len() {
                    return Err(fallback());
                }
                let mut items: Vec<Bound<'py, PyAny>> = Vec::with_capacity(elements.len());
                for (element, each) in elements.iter().zip(value.try_iter()?) {
                    items.push(element.apply(ctx, &each?)?);
                }
                Ok(PyTuple::new(py, items)?.into_any())
            }
            Element::Pluck {
                serializer,
                data_key,
                many,
            } => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                // N1: defer before consuming a non-replayable iterator.
                if *many && is_one_shot_iterator(value) {
                    return Err(fallback());
                }
                let dk = data_key.bind(py);
                let ret = serializer.run(ctx, value, *many)?;
                if *many {
                    // ``utils.pluck(ret, key)`` == ``[d[key] for d in ret]``.
                    let out = PyList::empty(py);
                    for d in ret.try_iter()? {
                        out.append(d?.get_item(dk)?)?;
                    }
                    Ok(out.into_any())
                } else {
                    ret.get_item(dk) // ``ret[data_key]``
                }
            }
            Element::TemporalNative(kind) => {
                if value.is_none() {
                    return Ok(py.None().into_bound(py));
                }
                let mut buf = String::with_capacity(32);
                write_temporal_native(&mut buf, py, value, *kind)?;
                Ok(PyString::new(py, &buf).into_any())
            }
        }
    }

    /// JSON form of [`Element::apply`]: write the serialized value as JSON into
    /// ``buf``. ``Nested``/``List`` recurse structurally (no intermediate dict);
    /// every other element computes its serialized value via ``apply`` and hands
    /// it to [`write_json_value`], so the JSON output is exactly
    /// ``json.dumps(self.apply(value))``.
    pub(crate) fn write_json(
        &self,
        buf: &mut String,
        ctx: &Ctx,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        match self {
            Element::Nested(serializer, many) => {
                if value.is_none() {
                    buf.push_str("null");
                    Ok(())
                } else {
                    // N1: same guard as apply — defer before consuming a
                    // non-replayable iterator so a later fallback doesn't replay
                    // an exhausted generator against the original object.
                    if *many && is_one_shot_iterator(value) {
                        return Err(fallback());
                    }
                    serializer.write_json(buf, ctx, value, *many)
                }
            }
            Element::List(inner) => {
                if value.is_none() {
                    buf.push_str("null");
                    return Ok(());
                }
                // N1: same guard as apply.
                if is_one_shot_iterator(value) {
                    return Err(fallback());
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
            Element::TemporalNative(kind) => {
                let py = value.py();
                if value.is_none() {
                    buf.push_str("null");
                    return Ok(());
                }
                // ISO datetime strings contain only printable ASCII safe for JSON
                // (no '"' or '\'), so we write directly without json_escape_into.
                buf.push('"');
                write_temporal_native(buf, py, value, *kind)?;
                buf.push('"');
                Ok(())
            }
            _ => {
                let serialized = self.apply(ctx, value)?;
                write_json_value(buf, &serialized, 0)
            }
        }
    }
}
