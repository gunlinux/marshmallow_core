//! Construction of the load value model from the Python spec tuples emitted by
//! ``marshmallow_core._compiler``. Tag integers here (load element tags 0–20,
//! validator tags 0–6) must stay in sync with the compiler and
//! ``tests/test_protocol.py``.

use pyo3::prelude::*;
use pyo3::types::{PyList, PyString, PyTuple};
use std::collections::HashMap;

use crate::load::element::{LoadElement, LoadFieldSpec};
use crate::load::json_tree::element_consumes_partial;
use crate::load::serializer::{split_key_parts, LoadSerializer};
use crate::load::validators::Validator;

pub(crate) fn parse_load_serializer(
    py: Python<'_>,
    payload: &Bound<'_, PyAny>,
) -> PyResult<LoadSerializer> {
    let t = payload.cast::<PyTuple>()?;
    let many: bool = t.get_item(0)?.extract()?;
    let unknown: u8 = t.get_item(1)?.extract()?;
    let known_keys = t.get_item(2)?.unbind();
    let specs_list = t.get_item(3)?.cast_into::<PyList>()?;
    let mut specs = Vec::with_capacity(specs_list.len());
    for item in specs_list.iter() {
        specs.push(parse_load_field_spec(py, &item)?);
    }
    // Index ``data_key`` -> spec position once, for the single-pass JSON loader.
    let mut data_key_index = HashMap::with_capacity(specs.len());
    let mut distinct_data_keys = true;
    for (i, spec) in specs.iter().enumerate() {
        let data_key = match spec {
            LoadFieldSpec::Native { data_key, .. } | LoadFieldSpec::Callback { data_key, .. } => {
                data_key
            }
        };
        let key = data_key.bind(py).to_str()?.to_owned();
        if data_key_index.insert(key, i).is_some() {
            distinct_data_keys = false; // two specs share a data_key
        }
    }
    Ok(LoadSerializer {
        specs,
        many,
        unknown,
        known_keys,
        data_key_index,
        distinct_data_keys,
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
        // (False, data_key, out_key, attr_name, load_default, required,
        //  allow_none, element, [validator, ...])
        let validators_list = t.get_item(8)?.cast_into::<PyList>()?;
        let mut validators = Vec::with_capacity(validators_list.len());
        for item in validators_list.iter() {
            validators.push(parse_validator(py, &item)?);
        }
        let out_key = t.get_item(2)?.cast_into::<PyString>()?;
        let out_key_parts = split_key_parts(py, &out_key)?;
        let element = parse_load_element(py, &t.get_item(7)?)?;
        let consumes_partial = element_consumes_partial(&element);
        Ok(LoadFieldSpec::Native {
            data_key: t.get_item(1)?.cast_into::<PyString>()?.unbind(),
            out_key: out_key.unbind(),
            out_key_parts,
            attr_name: t.get_item(3)?.cast_into::<PyString>()?.unbind(),
            load_default: t.get_item(4)?.unbind(),
            required: t.get_item(5)?.extract()?,
            allow_none: t.get_item(6)?.extract()?,
            element,
            consumes_partial,
            validators,
        })
    }
}

/// Parse a Python list/tuple of validator specs into ``Vec<Validator>``.
fn parse_validator_list(py: Python<'_>, list: &Bound<'_, PyAny>) -> PyResult<Vec<Validator>> {
    let mut out = Vec::new();
    for item in list.try_iter()? {
        out.push(parse_validator(py, &item?)?);
    }
    Ok(out)
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
        3 => Ok(Validator::Equal {
            // (3, comparable)
            comparable: t.get_item(1)?.unbind(),
        }),
        4 => Ok(Validator::NoneOf {
            // (4, iterable)
            iterable: t.get_item(1)?.unbind(),
        }),
        5 => Ok(Validator::ContainsOnly {
            // (5, choices)
            choices: t.get_item(1)?.unbind(),
        }),
        6 => Ok(Validator::Python {
            // (6, validator_callable)
            validator: t.get_item(1)?.unbind(),
        }),
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
        12 => Ok(LoadElement::Boolean {
            // (12, truthy_set, falsy_set)
            truthy: t.get_item(1)?.unbind(),
            falsy: t.get_item(2)?.unbind(),
        }),
        13 => Ok(LoadElement::IntStrict), // (13,)
        17 => Ok(LoadElement::TimeDelta {
            // (17, bound _deserialize)
            deserialize: t.get_item(1)?.unbind(),
        }),
        18 => Ok(LoadElement::DatetimeAwareness {
            // (18, bound _deserialize)
            deserialize: t.get_item(1)?.unbind(),
        }),
        19 => Ok(LoadElement::IpAddr {
            // (19, bound _deserialize)
            deserialize: t.get_item(1)?.unbind(),
        }),
        16 => {
            // (16, nested_payload, data_key, many)
            let serializer = parse_load_serializer(py, &t.get_item(1)?)?;
            Ok(LoadElement::Pluck {
                serializer: Box::new(serializer),
                data_key: t.get_item(2)?.cast_into::<PyString>()?.unbind(),
                many: t.get_item(3)?.extract()?,
            })
        }
        15 => {
            // (15, (element, element, ...))
            let specs = t.get_item(1)?.cast_into::<PyTuple>()?;
            let mut elements = Vec::with_capacity(specs.len());
            for item in specs.iter() {
                elements.push(parse_load_element(py, &item)?);
            }
            Ok(LoadElement::Tuple(elements))
        }
        14 => {
            // (14, key_el_or_None, key_validators, value_el_or_None, val_validators)
            let parse_opt = |item: Bound<'_, PyAny>| -> PyResult<Option<Box<LoadElement>>> {
                if item.is_none() {
                    Ok(None)
                } else {
                    Ok(Some(Box::new(parse_load_element(py, &item)?)))
                }
            };
            Ok(LoadElement::DictTyped {
                key_el: parse_opt(t.get_item(1)?)?,
                key_validators: parse_validator_list(py, &t.get_item(2)?)?,
                val_el: parse_opt(t.get_item(3)?)?,
                val_validators: parse_validator_list(py, &t.get_item(4)?)?,
            })
        }
        20 => {
            // (20, payload, post_load_fn)
            let serializer = parse_load_serializer(py, &t.get_item(1)?)?;
            let post_load_fn = t.get_item(2)?.unbind();
            Ok(LoadElement::NestedPostLoad {
                serializer: Box::new(serializer),
                post_load_fn,
            })
        }
        other => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "unknown load element tag {other}"
        ))),
    }
}
