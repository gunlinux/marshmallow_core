//! Native ISO formatting for ``datetime``/``date``/``time`` (and UTC offsets),
//! written directly off CPython's C-level struct accessors instead of calling
//! the Python ``isoformat()``/``_format_offset`` machinery. Requires a non-abi3
//! build (the struct accessors are not in the limited API).

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDate, PyDateAccess, PyDateTime, PyDelta, PyDeltaAccess, PyTime, PyTimeAccess};

use crate::context::fallback;

/// Discriminant for ``Element::TemporalNative`` — which Python temporal type to expect.
#[derive(Clone, Copy)]
pub(crate) enum TemporalKind {
    DateTime,
    Date,
    Time,
}

/// Append ``±HH:MM[:SS[.ffffff]]`` for a UTC offset timedelta, matching
/// CPython's ``datetime._format_offset`` exactly.
fn write_utcoffset(buf: &mut String, offset: &Bound<'_, PyAny>) -> PyResult<()> {
    let delta = offset.cast::<PyDelta>().map_err(|_| fallback())?;
    let days = delta.get_days() as i64;
    let secs = delta.get_seconds() as i64;
    let us = delta.get_microseconds() as i64;
    let total_us = days * 86_400_000_000_i64 + secs * 1_000_000_i64 + us;
    let (sign, abs_us) = if total_us < 0 {
        ('-', (-total_us) as u64)
    } else {
        ('+', total_us as u64)
    };
    let us_part = (abs_us % 1_000_000) as u32;
    let abs_s = abs_us / 1_000_000;
    let ss = (abs_s % 60) as u32;
    let mm = ((abs_s / 60) % 60) as u32;
    let hh = (abs_s / 3600) as u32;
    use std::fmt::Write as _;
    let _ = write!(buf, "{}{:02}:{:02}", sign, hh, mm);
    if ss != 0 || us_part != 0 {
        let _ = write!(buf, ":{:02}", ss);
        if us_part != 0 {
            let _ = write!(buf, ".{:06}", us_part);
        }
    }
    Ok(())
}

/// Write the ISO parts shared by ``format_datetime_native`` and the
/// ``dumps``-fused path into ``buf``.
pub(crate) fn write_temporal_native(
    buf: &mut String,
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    kind: TemporalKind,
) -> PyResult<()> {
    use std::fmt::Write as _;
    match kind {
        TemporalKind::DateTime => {
            let dt = value.cast::<PyDateTime>().map_err(|_| fallback())?;
            let _ = write!(
                buf,
                "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
                dt.get_year(),
                dt.get_month() as u32,
                dt.get_day() as u32,
                dt.get_hour() as u32,
                dt.get_minute() as u32,
                dt.get_second() as u32,
            );
            let us = dt.get_microsecond();
            if us != 0 {
                let _ = write!(buf, ".{:06}", us);
            }
            let offset = value.call_method0(intern!(py, "utcoffset"))?;
            if !offset.is_none() {
                write_utcoffset(buf, &offset)?;
            }
        }
        TemporalKind::Date => {
            // datetime.datetime is a subclass of datetime.date; if value is a
            // datetime, fall back so Python produces the full isoformat string.
            if value.is_instance_of::<PyDateTime>() {
                return Err(fallback());
            }
            let d = value.cast::<PyDate>().map_err(|_| fallback())?;
            let _ = write!(
                buf,
                "{:04}-{:02}-{:02}",
                d.get_year(),
                d.get_month() as u32,
                d.get_day() as u32,
            );
        }
        TemporalKind::Time => {
            let t = value.cast::<PyTime>().map_err(|_| fallback())?;
            let _ = write!(
                buf,
                "{:02}:{:02}:{:02}",
                t.get_hour() as u32,
                t.get_minute() as u32,
                t.get_second() as u32,
            );
            let us = t.get_microsecond();
            if us != 0 {
                let _ = write!(buf, ".{:06}", us);
            }
            let offset = value.call_method0(intern!(py, "utcoffset"))?;
            if !offset.is_none() {
                write_utcoffset(buf, &offset)?;
            }
        }
    }
    Ok(())
}
