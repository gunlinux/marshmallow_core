# Benchmark results

Per-call microseconds for stock pure-Python marshmallow vs. the installed Rust
core, with the speedup ratio, from `python -m performance.benchmark`. Recorded
so a regression in a later change is visible at a glance — re-run and update
this table when the core changes.

- **Environment:** Apple Silicon (arm64, macOS), CPython 3.12, marshmallow 4.3.0,
  release build (`maturin build --release`), `--number 6000`.
- **Recorded:** after the `itoa` + table-driven JSON escape change (faster
  integer formatting and string escaping in the dump JSON writer). Earlier-phase
  numbers, and the ARCH.md/F_SPEEDUP.md reviews the B*/F* item IDs refer to, are
  in git history; the current review is `F_REAL_REVIEW.md`. This session's
  machine baseline runs faster than the prior B1/B2 recording across the board
  (compare same-op rows with care — the attributable win is isolated below).
- These are indicative ratios, not guarantees; absolute numbers vary by machine.

| case      | op    | stock (µs) | core (µs) | speedup |
|-----------|-------|-----------:|----------:|--------:|
| flat      | dump  |       2.31 |      0.37 |   6.28x |
| flat      | load  |       4.81 |      0.42 |  11.47x |
| flat      | dumps |       3.44 |      0.65 |   5.29x |
| flat      | loads |       5.70 |      0.58 |   9.80x |
| nested    | dump  |       5.53 |      0.50 |  11.15x |
| nested    | load  |      16.24 |      0.64 |  25.50x |
| nested    | dumps |       7.41 |      0.93 |   7.94x |
| nested    | loads |      17.97 |      1.07 |  16.85x |
| list      | dump  |      86.90 |      4.31 |  20.16x |
| list      | load  |     235.43 |      6.56 |  35.87x |
| list      | dumps |     104.18 |      8.46 |  12.32x |
| list      | loads |     253.94 |     11.02 |  23.05x |
| validator | dump  |       1.66 |      0.25 |   6.70x |
| validator | load  |       4.30 |      0.39 |  11.11x |
| validator | dumps |       2.58 |      0.51 |   5.10x |
| validator | loads |       5.23 |      0.52 |  10.01x |
| hooks     | dump  |       1.28 |      0.23 |   5.44x |
| hooks     | load  |       4.66 |      2.16 |   2.16x |
| hooks     | dumps |       2.19 |      0.47 |   4.71x |
| hooks     | loads |       5.45 |      3.09 |   1.76x |
| api       | dump  |     122.42 |      9.39 |  13.03x |
| api       | load  |     319.14 |      9.55 |  33.43x |
| api       | dumps |     145.45 |     12.12 |  12.00x |
| api       | loads |     340.34 |     17.05 |  19.96x |

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
