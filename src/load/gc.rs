//! R2: GC traverse helpers for the load side — mirror every ``Py<T>`` ref held
//! in the load value model so CPython's cyclic collector can see (and break) the
//! schema → LoadDeserializer → field → schema cycle.

use crate::load::element::{LoadElement, LoadFieldSpec};
use crate::load::serializer::LoadSerializer;
use crate::load::validators::Validator;

fn traverse_validator(
    v: &Validator,
    visit: &pyo3::PyVisit<'_>,
) -> Result<(), pyo3::PyTraverseError> {
    match v {
        Validator::Range { min, max, .. } => {
            if let Some(m) = min {
                visit.call(m)?;
            }
            if let Some(m) = max {
                visit.call(m)?;
            }
        }
        Validator::OneOf { choices } => {
            visit.call(choices)?;
        }
        Validator::Equal { comparable } => {
            visit.call(comparable)?;
        }
        Validator::NoneOf { iterable } => {
            visit.call(iterable)?;
        }
        Validator::ContainsOnly { choices } => {
            visit.call(choices)?;
        }
        Validator::Python { validator } => {
            visit.call(validator)?;
        }
        Validator::Length { .. } => {}
    }
    Ok(())
}

fn traverse_load_element(
    el: &LoadElement,
    visit: &pyo3::PyVisit<'_>,
) -> Result<(), pyo3::PyTraverseError> {
    match el {
        LoadElement::Enum {
            enum_class, inner, ..
        } => {
            visit.call(enum_class)?;
            traverse_load_element(inner, visit)?;
        }
        LoadElement::Uuid { uuid_class } => {
            visit.call(uuid_class)?;
        }
        LoadElement::Temporal {
            internal_type,
            func,
        } => {
            visit.call(internal_type)?;
            visit.call(func)?;
        }
        LoadElement::Decimal { deserialize }
        | LoadElement::TimeDelta { deserialize }
        | LoadElement::DatetimeAwareness { deserialize }
        | LoadElement::IpAddr { deserialize } => {
            visit.call(deserialize)?;
        }
        LoadElement::Constant { constant } => {
            visit.call(constant)?;
        }
        LoadElement::Boolean { truthy, falsy } => {
            visit.call(truthy)?;
            visit.call(falsy)?;
        }
        LoadElement::Nested(s) => traverse_load_serializer_inner(s, visit)?,
        LoadElement::List(inner, _) => traverse_load_element(inner, visit)?,
        LoadElement::Tuple(els) => {
            for e in els {
                traverse_load_element(e, visit)?;
            }
        }
        LoadElement::DictTyped {
            key_el,
            val_el,
            key_validators,
            val_validators,
        } => {
            if let Some(k) = key_el {
                traverse_load_element(k, visit)?;
            }
            if let Some(v) = val_el {
                traverse_load_element(v, visit)?;
            }
            for v in key_validators {
                traverse_validator(v, visit)?;
            }
            for v in val_validators {
                traverse_validator(v, visit)?;
            }
        }
        LoadElement::Pluck {
            serializer,
            data_key,
            ..
        } => {
            traverse_load_serializer_inner(serializer, visit)?;
            visit.call(data_key)?;
        }
        LoadElement::NestedPostLoad {
            serializer,
            post_load_fn,
        } => {
            traverse_load_serializer_inner(serializer, visit)?;
            visit.call(post_load_fn)?;
        }
        _ => {} // Passthrough, Str, Int, IntStrict, Float, Dict
    }
    Ok(())
}

fn traverse_load_field_spec(
    spec: &LoadFieldSpec,
    visit: &pyo3::PyVisit<'_>,
) -> Result<(), pyo3::PyTraverseError> {
    match spec {
        LoadFieldSpec::Native {
            data_key,
            out_key,
            out_key_parts,
            attr_name,
            load_default,
            element,
            validators,
            ..
        } => {
            visit.call(data_key)?;
            visit.call(out_key)?;
            if let Some(parts) = out_key_parts {
                for p in parts {
                    visit.call(p)?;
                }
            }
            visit.call(attr_name)?;
            visit.call(load_default)?;
            traverse_load_element(element, visit)?;
            for v in validators {
                traverse_validator(v, visit)?;
            }
        }
        LoadFieldSpec::Callback {
            data_key,
            attr_name,
            out_key,
            out_key_parts,
            field,
        } => {
            visit.call(data_key)?;
            visit.call(attr_name)?;
            visit.call(out_key)?;
            if let Some(parts) = out_key_parts {
                for p in parts {
                    visit.call(p)?;
                }
            }
            visit.call(field)?;
        }
    }
    Ok(())
}

pub(crate) fn traverse_load_serializer_inner(
    s: &LoadSerializer,
    visit: &pyo3::PyVisit<'_>,
) -> Result<(), pyo3::PyTraverseError> {
    visit.call(&s.known_keys)?;
    for spec in &s.specs {
        traverse_load_field_spec(spec, visit)?;
    }
    Ok(())
}
