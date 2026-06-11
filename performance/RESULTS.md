# Benchmark results

Per-call microseconds for stock pure-Python marshmallow vs. the installed Rust
core, with the speedup ratio, from `python -m performance.benchmark`. Recorded
so a regression in a later change is visible at a glance — re-run and update
this table when the core changes.

- **Environment:** Apple Silicon (arm64, macOS), CPython 3.12, marshmallow 4.3.0,
  release build (`maturin build --release`), `--number 6000`.
- **Recorded:** after the B1/B2 loader rework (single-pass fused `loads`; skip
  the jiter parse for callback schemas). Earlier-phase numbers, and the
  ARCH.md/F_SPEEDUP.md reviews the B*/F* item IDs refer to, are in git history;
  the current review is `F_REAL_REVIEW.md`.
- These are indicative ratios, not guarantees; absolute numbers vary by machine.

| case      | op    | stock (µs) | core (µs) | speedup |
|-----------|-------|-----------:|----------:|--------:|
| flat      | dump  |       2.08 |      0.54 |   3.86x |
| flat      | load  |       4.54 |      0.44 |  10.44x |
| flat      | dumps |       3.08 |      0.89 |   3.46x |
| flat      | loads |       5.46 |      0.57 |   9.64x |
| nested    | dump  |       5.24 |      0.57 |   9.15x |
| nested    | load  |      15.34 |      0.72 |  21.20x |
| nested    | dumps |       6.92 |      1.05 |   6.61x |
| nested    | loads |      16.94 |      1.14 |  14.84x |
| list      | dump  |      81.31 |      5.42 |  15.01x |
| list      | load  |     223.58 |      8.17 |  27.38x |
| list      | dumps |      97.38 |     11.01 |   8.84x |
| list      | loads |     242.18 |     13.42 |  18.04x |
| validator | dump  |       1.58 |      0.27 |   5.80x |
| validator | load  |       4.16 |      0.43 |   9.75x |
| validator | dumps |       2.46 |      0.54 |   4.57x |
| validator | loads |       5.11 |      0.57 |   9.03x |
| hooks     | dump  |       1.22 |      0.24 |   5.15x |
| hooks     | load  |       4.55 |      2.18 |   2.09x |
| hooks     | dumps |       2.06 |      0.50 |   4.09x |
| hooks     | loads |       5.37 |      3.19 |   1.68x |
| api       | dump  |     119.39 |     15.30 |   7.80x |
| api       | load  |     311.72 |     12.29 |  25.37x |
| api       | dumps |     142.09 |     20.85 |   6.81x |
| api       | loads |     334.59 |     21.16 |  15.81x |

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
