//! JSON string escaping and value encoding for the fused ``dumps`` path.
//!
//! Output is byte-identical to CPython's ``json.dumps`` (default options,
//! ``ensure_ascii=True``). [`write_json_value`] raises [`AccelFallback`] for any
//! type it cannot reproduce exactly, so the caller defers to stdlib ``json.dumps``.

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::context::fallback;

/// Hex digit table for fast ``\uXXXX`` emission without ``core::fmt`` overhead.
const HEX: &[u8; 16] = b"0123456789abcdef";

/// Push a single ``\uXXXX`` escape for a 16-bit code point (avoids ``write!``).
#[inline]
fn push_u4(buf: &mut String, cp: u32) {
    // SAFETY: all pushed bytes are valid ASCII.
    buf.push_str("\\u");
    buf.push(HEX[((cp >> 12) & 0xF) as usize] as char);
    buf.push(HEX[((cp >> 8) & 0xF) as usize] as char);
    buf.push(HEX[((cp >> 4) & 0xF) as usize] as char);
    buf.push(HEX[(cp & 0xF) as usize] as char);
}

/// Per-byte classification for [`json_escape_into`] (serde_json's table-driven
/// approach). ``0`` = clean (copy verbatim); any other code selects the escape
/// handling. Bytes ``0x80..=0xFF`` are all ``NON`` (multi-byte UTF-8, decoded at
/// the call site). This replaces the old four-comparison range test per byte
/// with a single table load + compare-to-zero.
const CL: u8 = 0; // clean
const QU: u8 = 1; // \"
const BS: u8 = 2; // \\
const BB: u8 = 3; // \b   (0x08)
const TT: u8 = 4; // \t   (0x09)
const NN: u8 = 5; // \n   (0x0a)
const FF: u8 = 6; // \f   (0x0c)
const RR: u8 = 7; // \r   (0x0d)
const UU: u8 = 8; // \u00XX (other control char, incl. 0x7f)
const NON: u8 = 9; // non-ASCII byte (>= 0x80): decode and emit \uXXXX

const ESCAPE_TABLE: [u8; 256] = {
    let mut t = [CL; 256];
    let mut b = 0u8;
    while b < 0x20 {
        t[b as usize] = UU; // control chars default to \u00XX ...
        b += 1;
    }
    t[0x08] = BB;
    t[0x09] = TT;
    t[0x0a] = NN;
    t[0x0c] = FF;
    t[0x0d] = RR;
    t[b'"' as usize] = QU;
    t[b'\\' as usize] = BS;
    t[0x7f] = UU; // DEL: outside 0x20..=0x7e, escaped as
    let mut i = 0x80usize;
    while i < 256 {
        t[i] = NON;
        i += 1;
    }
    t
};

/// Append ``s`` to ``buf`` as a JSON string literal, matching CPython's
/// ``json.encoder.py_encode_basestring_ascii`` (``ensure_ascii=True``):
/// short escapes for ``" \\ \n \r \t \b \f``, ``\u00XX`` for other control
/// characters, raw bytes for printable ASCII (``0x20..=0x7E``, ``/`` unescaped),
/// and ``\uXXXX`` (surrogate pairs above the BMP) for everything else.
///
/// Non-ASCII characters are emitted via ``push_u4`` (a nibble table) rather
/// than ``write!`` (which invokes the full ``core::fmt`` machinery per char).
/// For Cyrillic/CJK/Arabic text this is the hot path and the saving is ~14×
/// per char (measured; F_SPEEDUP F1).
pub(crate) fn json_escape_into(buf: &mut String, s: &str) {
    buf.push('"');
    // Scan bytes (not chars) via ``ESCAPE_TABLE``: a "clean" byte (table entry
    // ``CL``) extends the current run; any other byte flushes the run with a
    // single ``push_str`` and emits its escape. Output is byte-identical to the
    // stdlib ``ensure_ascii=True`` encoding; multi-byte UTF-8 is decoded only at
    // the rare non-ASCII byte.
    let bytes = s.as_bytes();
    let mut last = 0;
    let mut i = 0;
    while i < bytes.len() {
        let b = bytes[i];
        let code = ESCAPE_TABLE[b as usize];
        if code == CL {
            i += 1;
            continue; // clean: extend the run
        }
        if last < i {
            buf.push_str(&s[last..i]); // flush the clean run
        }
        match code {
            QU => buf.push_str("\\\""),
            BS => buf.push_str("\\\\"),
            BB => buf.push_str("\\b"),
            TT => buf.push_str("\\t"),
            NN => buf.push_str("\\n"),
            FF => buf.push_str("\\f"),
            RR => buf.push_str("\\r"),
            UU => push_u4(buf, b as u32), // other control char
            _ => {
                // NON: decode the one char and emit ``\uXXXX`` (surrogate pair
                // above the BMP), matching ``ensure_ascii=True``.
                let ch = s[i..].chars().next().unwrap();
                let cp = ch as u32;
                if cp <= 0xFFFF {
                    push_u4(buf, cp);
                } else {
                    let v = cp - 0x10000;
                    let hi = 0xD800 + (v >> 10);
                    let lo = 0xDC00 + (v & 0x3FF);
                    push_u4(buf, hi);
                    push_u4(buf, lo);
                }
                i += ch.len_utf8();
                last = i;
                continue;
            }
        }
        i += 1;
        last = i;
    }
    if last < bytes.len() {
        buf.push_str(&s[last..]);
    }
    buf.push('"');
}

/// ``depth`` guards against unbounded runtime recursion (R3): past
/// ``JSON_DEPTH_LIMIT`` levels we raise ``AccelFallback`` and the caller's
/// stock ``json.dumps`` raises the catchable ``RecursionError``.
const JSON_DEPTH_LIMIT: usize = 512;

/// JSON-encode an arbitrary Python value into ``buf`` exactly as stdlib
/// ``json.dumps`` (default options) would, or raise ``AccelFallback`` for a type
/// it cannot reproduce byte-for-byte (so the caller defers to ``json.dumps``,
/// which then either encodes it or raises the identical ``TypeError``).
pub(crate) fn write_json_value(
    buf: &mut String,
    value: &Bound<'_, PyAny>,
    depth: usize,
) -> PyResult<()> {
    if depth > JSON_DEPTH_LIMIT {
        return Err(fallback());
    }
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
        // Format in Rust when it fits a machine integer (byte-identical to
        // ``int.__repr__``); arbitrary-precision ints fall back to ``str()``.
        // ``itoa`` writes into a stack buffer, faster than ``core::fmt``.
        if let Ok(n) = value.extract::<i64>() {
            buf.push_str(itoa::Buffer::new().format(n));
        } else if let Ok(n) = value.extract::<i128>() {
            buf.push_str(itoa::Buffer::new().format(n));
        } else {
            buf.push_str(value.str()?.to_str()?); // big int -> ``int.__repr__``
        }
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
            write_json_value(buf, &item?, depth + 1)?;
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
            write_json_value(buf, &v, depth + 1)?;
        }
        buf.push('}');
        return Ok(());
    }
    Err(fallback()) // unencodable type (Decimal, datetime, custom, ...) -> defer
}

#[cfg(test)]
mod tests {
    //! Unit tests for the Python-free escaping function. These run under
    //! `cargo test` (which links libpython because the `extension-module` feature
    //! is off — see Cargo.toml); the cross-language parity story lives in
    //! `tests/test_equivalence.py`.
    use super::*;

    fn esc(s: &str) -> String {
        let mut buf = String::new();
        json_escape_into(&mut buf, s);
        buf
    }

    #[test]
    fn json_escape_plain_and_slash() {
        assert_eq!(esc(""), "\"\"");
        assert_eq!(esc("abc"), "\"abc\"");
        // ``ensure_ascii=True`` leaves ``/`` unescaped, like stdlib json.
        assert_eq!(esc("a/b"), "\"a/b\"");
    }

    #[test]
    fn json_escape_short_escapes() {
        // Quote and backslash -> ``\"`` and ``\\``.
        assert_eq!(esc("\"\\"), "\"\\\"\\\\\"");
        assert_eq!(esc("\n\r\t"), "\"\\n\\r\\t\"");
        // 0x08 backspace, 0x0c form feed.
        assert_eq!(esc("\u{08}\u{0c}"), "\"\\b\\f\"");
    }

    #[test]
    fn json_escape_other_control_chars_are_uxxxx() {
        assert_eq!(esc("\u{01}"), "\"\\u0001\"");
        assert_eq!(esc("\u{1f}"), "\"\\u001f\"");
    }

    #[test]
    fn json_escape_non_ascii_and_surrogate_pairs() {
        assert_eq!(esc("é"), "\"\\u00e9\""); // BMP -> single \uXXXX
        assert_eq!(esc("😀"), "\"\\ud83d\\ude00\""); // above BMP -> surrogate pair
    }

    #[test]
    fn json_escape_mixed_clean_runs() {
        assert_eq!(esc("hello\nworld"), "\"hello\\nworld\"");
        assert_eq!(esc("aé b"), "\"a\\u00e9 b\"");
    }
}
