//! R2: GC traverse helpers for the dump side. CPython's cyclic GC can't see
//! through Rust-owned ``Py<T>`` refs, so without ``__traverse__``/``__clear__``
//! any schema that ever dumps leaks itself permanently (schema → instance dict →
//! DumpSerializer → bound-method/field → schema). These helpers make the
//! cycle visible so the collector can break it.

use crate::dump::serializer::{Element, FieldSpec, Serializer};

fn traverse_element(el: &Element, visit: &pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
    match el {
        Element::Temporal { func, format } => {
            if let Some(f) = func {
                visit.call(f)?;
            }
            visit.call(format)?;
        }
        Element::Enum { inner, .. } => traverse_element(inner, visit)?,
        Element::Decimal { serialize } => {
            visit.call(serialize)?;
        }
        Element::Constant { constant } => {
            visit.call(constant)?;
        }
        Element::TimeDelta { serialize } => {
            visit.call(serialize)?;
        }
        Element::Nested(s, _) => traverse_dump_serializer(s, visit)?,
        Element::Pluck {
            serializer,
            data_key,
            ..
        } => {
            traverse_dump_serializer(serializer, visit)?;
            visit.call(data_key)?;
        }
        Element::List(inner) => traverse_element(inner, visit)?,
        Element::DictTyped { key_el, val_el } => {
            if let Some(k) = key_el {
                traverse_element(k, visit)?;
            }
            if let Some(v) = val_el {
                traverse_element(v, visit)?;
            }
        }
        Element::Tuple(els) => {
            for e in els {
                traverse_element(e, visit)?;
            }
        }
        _ => {} // Passthrough, Str, Int, Float, Uuid, IpAddr, Dict
    }
    Ok(())
}

fn traverse_field_spec(
    spec: &FieldSpec,
    visit: &pyo3::PyVisit<'_>,
) -> Result<(), pyo3::PyTraverseError> {
    match spec {
        FieldSpec::Native {
            key,
            key_parts,
            output_key,
            dump_default,
            element,
            ..
        } => {
            visit.call(key)?;
            if let Some(parts) = key_parts {
                for p in parts {
                    visit.call(p)?;
                }
            }
            visit.call(output_key)?;
            visit.call(dump_default)?;
            traverse_element(element, visit)?;
        }
        FieldSpec::Callback {
            name,
            output_key,
            field,
            ..
        } => {
            visit.call(name)?;
            visit.call(output_key)?;
            visit.call(field)?;
        }
    }
    Ok(())
}

pub(crate) fn traverse_dump_serializer(
    s: &Serializer,
    visit: &pyo3::PyVisit<'_>,
) -> Result<(), pyo3::PyTraverseError> {
    visit.call(&s.accessor)?;
    for spec in &s.specs {
        traverse_field_spec(spec, visit)?;
    }
    Ok(())
}
