//! Recognized ``marshmallow.validate`` validators (modelling only their
//! pass/fail decision) and the [`Partial`] state threaded through a load.

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyList, PyString};

use crate::context::{fallback, to_fallback};

/// A recognized ``marshmallow.validate`` validator, modelled to reproduce only
/// its *pass/fail decision* (mirrors `marshmallow_core._compiler._build_validator`).
/// On failure the field raises ``AccelFallback`` and Python re-runs the validator
/// to emit the exact (possibly custom) error message.
pub(crate) enum Validator {
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
    /// ``Equal``: fail unless ``value == comparable``.
    Equal { comparable: Py<PyAny> },
    /// ``NoneOf``: fail if ``value in iterable`` (a ``TypeError`` from ``in``
    /// passes, mirroring marshmallow's ``except TypeError``).
    NoneOf { iterable: Py<PyAny> },
    /// ``ContainsOnly``: fail unless every element of ``value`` is in ``choices``.
    ContainsOnly { choices: Py<PyAny> },
    /// Any other validator (custom callable, ``Email``/``URL``/``Regexp``, ...):
    /// call it; a ``False`` return fails, a raise propagates — both become an
    /// ``AccelFallback`` so Python re-runs it for the exact message.
    Python { validator: Py<PyAny> },
}

impl Validator {
    /// Return ``Ok(true)`` if ``value`` passes, ``Ok(false)`` if it fails (the
    /// caller then raises ``AccelFallback``). A comparison/``len``/``in`` that
    /// itself raises is propagated as ``Err`` and turned into a fallback too.
    pub(crate) fn check(&self, value: &Bound<'_, PyAny>) -> PyResult<bool> {
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
            Validator::Equal { comparable } => value.eq(comparable.bind(py)),
            Validator::NoneOf { iterable } => match iterable.bind(py).contains(value) {
                Ok(found) => Ok(!found), // pass iff not present
                // ``NoneOf`` swallows only ``TypeError`` (unhashable/incomparable)
                // and passes; any other error propagates (-> fallback).
                Err(e) if e.is_instance_of::<PyTypeError>(py) => Ok(true),
                Err(e) => Err(e),
            },
            Validator::ContainsOnly { choices } => {
                let ch = choices.bind(py);
                for val in value.try_iter()? {
                    if !ch.contains(&val?)? {
                        return Ok(false);
                    }
                }
                Ok(true)
            }
            Validator::Python { validator } => {
                // ``validator(value)``: a raise propagates (caller -> fallback);
                // a literal ``False`` return fails (mirrors marshmallow's ``r is
                // False`` for plain callables). Anything else passes. When in
                // doubt this fails -> fallback, where Python is authoritative, so
                // we never *pass* something marshmallow would reject.
                let r = validator.bind(py).call1((value,))?;
                Ok(!r.is(PyBool::new(py, false)))
            }
        }
    }
}

/// Run a slice of validators against ``value``; any failure or raise becomes an
/// ``AccelFallback`` (mirrors the per-field validator loop).
pub(crate) fn check_validators(
    py: Python<'_>,
    validators: &[Validator],
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    for validator in validators {
        match validator.check(value) {
            Ok(true) => {}
            Ok(false) => return Err(fallback()),
            Err(e) => return Err(to_fallback(py, e)),
        }
    }
    Ok(())
}

/// How ``load(partial=...)`` is threaded through a load: not partial, fully
/// partial (``True``), or a collection of (possibly dotted) field names allowed
/// to be missing at this level. Mirrors marshmallow's ``_deserialize`` handling.
pub(crate) enum Partial<'py> {
    None,
    All,
    Coll(Bound<'py, PyAny>),
}

impl<'py> Partial<'py> {
    /// Interpret the ``partial`` argument the caller passed to ``run``:
    /// ``True`` -> all fields optional; a collection -> those names optional;
    /// anything falsy (``False``/``None``/empty) -> not partial.
    pub(crate) fn from_arg(arg: &Bound<'py, PyAny>) -> PyResult<Self> {
        if arg.is_instance_of::<PyBool>() {
            Ok(if arg.is_truthy()? {
                Partial::All
            } else {
                Partial::None
            })
        } else if arg.is_none() {
            Ok(Partial::None)
        } else {
            Ok(Partial::Coll(arg.clone())) // list/tuple/set/frozenset of names
        }
    }

    /// Whether a field with attribute name ``attr_name`` may be missing here
    /// (``partial is True or attr_name in partial``).
    pub(crate) fn allows_missing(&self, attr_name: &Bound<'py, PyString>) -> PyResult<bool> {
        match self {
            Partial::All => Ok(true),
            Partial::None => Ok(false),
            Partial::Coll(c) => c.contains(attr_name),
        }
    }

    /// The sub-partial to pass into a nested field named ``attr_name``: for a
    /// collection, the entries prefixed ``attr_name.`` with that prefix stripped.
    pub(crate) fn derive(
        &self,
        py: Python<'py>,
        attr_name: &Bound<'py, PyString>,
    ) -> PyResult<Partial<'py>> {
        match self {
            Partial::None => Ok(Partial::None),
            Partial::All => Ok(Partial::All),
            Partial::Coll(c) => {
                let prefix = format!("{}.", attr_name.to_str()?);
                let sub = PyList::empty(py);
                for name in c.try_iter()? {
                    let name = name?;
                    let s = name.cast::<PyString>().map_err(|_| fallback())?;
                    if let Some(rest) = s.to_str()?.strip_prefix(&prefix) {
                        sub.append(rest)?;
                    }
                }
                Ok(Partial::Coll(sub.into_any()))
            }
        }
    }

    /// The Python value to pass as a callback field's ``partial=`` kwarg, or
    /// ``None`` to omit it (matching marshmallow for a non-partial load).
    pub(crate) fn as_kwarg(&self, py: Python<'py>) -> Option<Bound<'py, PyAny>> {
        match self {
            Partial::All => Some(PyBool::new(py, true).to_owned().into_any()),
            Partial::Coll(c) => Some(c.clone()),
            Partial::None => None,
        }
    }
}
