//! Construction of the dump value model from the Python spec tuples emitted by
//! ``marshmallow_core._compiler``. Tag integers here must stay in sync with the
//! compiler and ``tests/test_protocol.py``.

use pyo3::prelude::*;
use pyo3::types::{PyList, PyString, PyTuple};

use crate::dump::serializer::{Element, FieldSpec, Serializer};
use crate::json_writer::json_escape_into;
use crate::temporal::TemporalKind;

pub(crate) fn parse_serializer(py: Python<'_>, payload: &Bound<'_, PyAny>) -> PyResult<Serializer> {
    let t = payload.cast::<PyTuple>()?;
    let accessor = t.get_item(0)?.unbind();
    let specs_list = t.get_item(1)?.cast_into::<PyList>()?;
    let mut specs = Vec::with_capacity(specs_list.len());
    for item in specs_list.iter() {
        specs.push(parse_field_spec(py, &item)?);
    }
    Ok(Serializer { accessor, specs })
}

/// Build the pre-escaped JSON key prefix ``"\"name\": "`` for a field whose
/// output key is ``key_str``. Used by both ``FieldSpec`` variants for F_SPEEDUP F5.
fn make_json_key(key_str: &str) -> Box<str> {
    let mut s = String::new();
    json_escape_into(&mut s, key_str);
    s.push_str(": ");
    s.into_boxed_str()
}

fn parse_field_spec(py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<FieldSpec> {
    let t = item.cast::<PyTuple>()?;
    let is_callback: bool = t.get_item(0)?.extract()?;
    let output_key = t.get_item(1)?.cast_into::<PyString>()?;
    let json_key = make_json_key(output_key.to_str()?);
    let output_key = output_key.unbind();
    if is_callback {
        // (True, output_key, attr_name, field)
        Ok(FieldSpec::Callback {
            name: t.get_item(2)?.cast_into::<PyString>()?.unbind(),
            output_key,
            json_key,
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
            json_key,
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
        16 => Ok(Element::IpAddr),
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
        12 => Ok(Element::TimeDelta {
            // (12, bound _serialize)
            serialize: t.get_item(1)?.unbind(),
        }),
        13 => {
            // (13, key_element_or_None, value_element_or_None)
            let parse_opt = |item: Bound<'_, PyAny>| -> PyResult<Option<Box<Element>>> {
                if item.is_none() {
                    Ok(None)
                } else {
                    Ok(Some(Box::new(parse_element(py, &item)?)))
                }
            };
            Ok(Element::DictTyped {
                key_el: parse_opt(t.get_item(1)?)?,
                val_el: parse_opt(t.get_item(2)?)?,
            })
        }
        14 => {
            // (14, (element, element, ...))
            let specs = t.get_item(1)?.cast_into::<PyTuple>()?;
            let mut elements = Vec::with_capacity(specs.len());
            for item in specs.iter() {
                elements.push(parse_element(py, &item)?);
            }
            Ok(Element::Tuple(elements))
        }
        15 => {
            // (15, nested_payload, data_key, many)
            let serializer = parse_serializer(py, &t.get_item(1)?)?;
            Ok(Element::Pluck {
                serializer: Box::new(serializer),
                data_key: t.get_item(2)?.cast_into::<PyString>()?.unbind(),
                many: t.get_item(3)?.extract()?,
            })
        }
        17 => {
            // (17, kind) — TemporalNative: kind 0=DateTime, 1=Date, 2=Time
            let kind: u8 = t.get_item(1)?.extract()?;
            let temporal_kind = match kind {
                0 => TemporalKind::DateTime,
                1 => TemporalKind::Date,
                2 => TemporalKind::Time,
                _ => {
                    return Err(pyo3::exceptions::PyValueError::new_err(format!(
                        "unknown TemporalNative kind {kind}"
                    )))
                }
            };
            Ok(Element::TemporalNative(temporal_kind))
        }
        other => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "unknown element tag {other}"
        ))),
    }
}
