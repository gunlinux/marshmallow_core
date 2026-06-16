# Benchmark results

Per-call microseconds for stock pure-Python marshmallow vs. the installed Rust
core, with the speedup ratio, from `python -m performance.benchmark`. Recorded
so a regression in a later change is visible at a glance — re-run and update
this table when the core changes.

- **Environment:** Apple Silicon (arm64, macOS), CPython 3.12, marshmallow 4.3.0,
  release build (`maturin build --release`), `--number 6000`.
- **Recorded:** after the PyList pre-sizing change (dump/load list builders now
  construct the result list in one allocation from a capacity-hinted `Vec`
  instead of `empty` + per-element `append`): on the `list` case this moved
  `dump` ~4.31→3.97µs and `load` ~6.56→5.93µs (~5% each); `loads` ~11.0→10.7µs;
  `dumps` unchanged (it writes a string buffer, not a list). Earlier-phase
  numbers, and the ARCH.md/F_SPEEDUP.md reviews the B*/F* item IDs refer to, are
  in git history; the current review is `F_REAL_REVIEW.md`. This session's
  machine baseline runs faster than the prior B1/B2 recording across the board
  (compare same-op rows with care — the attributable win is isolated below).
- These are indicative ratios, not guarantees; absolute numbers vary by machine.

| case      | op    | stock (µs) | core (µs) | speedup |
|-----------|-------|-----------:|----------:|--------:|
| flat      | dump  |       2.23 |      0.36 |   6.14x |
| flat      | load  |       4.76 |      0.41 |  11.75x |
| flat      | dumps |       3.32 |      0.64 |   5.16x |
| flat      | loads |       5.63 |      0.57 |   9.95x |
| nested    | dump  |       5.50 |      0.53 |  10.40x |
| nested    | load  |      16.09 |      0.65 |  24.75x |
| nested    | dumps |       7.21 |      0.95 |   7.56x |
| nested    | loads |      17.65 |      1.07 |  16.50x |
| list      | dump  |      86.53 |      3.97 |  21.77x |
| list      | load  |     234.87 |      5.93 |  39.58x |
| list      | dumps |     103.49 |      8.63 |  11.99x |
| list      | loads |     250.93 |     10.68 |  23.50x |
| validator | dump  |       1.62 |      0.25 |   6.41x |
| validator | load  |       4.32 |      0.39 |  11.14x |
| validator | dumps |       2.59 |      0.51 |   5.09x |
| validator | loads |       5.20 |      0.52 |   9.96x |
| hooks     | dump  |       1.27 |      0.23 |   5.48x |
| hooks     | load  |       4.70 |      2.18 |   2.16x |
| hooks     | dumps |       2.12 |      0.48 |   4.40x |
| hooks     | loads |       5.41 |      3.04 |   1.78x |
| api       | dump  |     123.37 |      9.93 |  12.42x |
| api       | load  |     317.65 |      9.94 |  31.96x |
| api       | dumps |     147.00 |     12.33 |  11.92x |
| api       | loads |     340.13 |     17.63 |  19.29x |

The **`itoa` + table-escape** change targets only the dump JSON writer, so the
clean way to read its effect is the **JSON-writing overhead = `dumps` − `dump`
on the same run** (dump builds a Python dict and never touches the writer, so it
isolates the writer cost). On the int/string-heavy `api` payload that overhead
fell from ~5.55µs to ~2.5µs (roughly halved): `api dumps` went 6.8x→12.0x and
`flat dumps` 3.5x→5.3x. String-only payloads (`non_ascii`) and float-heavy
payloads are unaffected by `itoa`; floats remain the `dumps` floor (`repr()`
via Python, by design).

The **B1 single-pass loader** lifted every `loads` case, not just wide schemas
(it also dropped the per-field `data_key.to_str()` and the per-key frozenset
unknown probe): flat `loads` 1.05→0.57µs (5.2→9.6x), list `loads` 22.5→13.4µs
(10.8→18.0x), api `loads` 31.7→21.2µs (10.7→15.8x). Dump/dumps are unchanged
(B1/B2 touch only load); `hooks loads` stays ~2x (it runs stock `loads`, not the
fused path — see below). A wide-schema (50–100 field) case isn't in the standard
set; per the B1 measurements the fused path is now flat at ~0.44x of `json.loads`+load
across 5–100 fields, where it was 2.45x *slower* at 100 before.

## Reading the table

- **`api`** is the realistic mixed payload (paginated list of records with bool /
  str / int / float / datetime / list / nested fields) — the case to watch.
- **load** is the strongest direction on collections (26–28x). **dump** (Phase 5)
  and **dumps** (Phase 6 native int formatting) have closed much of the gap.
- **`dumps` is still ~2x `dump`** on float-heavy payloads — the remaining cost is
  `float.__repr__` per value, which can't be matched byte-for-byte in Rust
  (see the won't-do list in `F_REAL_REVIEW.md`).
- **hooks** is the floor (~2x): marshmallow's Python hook dispatch around the
  core's per-field step, not movable into Rust. Hook-bearing schemas also can't
  fuse `loads` (the hooks run in Python around the per-field step), so `hooks
  loads` runs stock `json.loads` + the accelerated hook load — hence the ~2x.
- **`loads`** for a fusable schema parses with jiter straight into the kept
  fields (no intermediate Python dict). The remaining floor is the unavoidable
  Python object construction for the values it keeps; the fused path is now
  ~2.3x faster than `json.loads` + accelerated load (was the bottleneck B1
  removed). Non-fusable (callback/hook) schemas fall back to stock `json.loads`.

## Field-level wins not shown above (not in the standard cases)

Measured via targeted micro-benchmarks:

- typed `Dict` dump 7.5x · `Tuple` dump 4.1x · `Pluck(many)` dump 6.5x
- NaiveDateTime/AwareDateTime load 3.1x
- a schema using `Equal`/`NoneOf`/`ContainsOnly` validators: load 10.4x
  (previously callback)
