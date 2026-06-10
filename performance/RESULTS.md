# Benchmark results

Per-call microseconds for stock pure-Python marshmallow vs. the installed Rust
core, with the speedup ratio, from `python -m performance.benchmark`. Recorded
so a regression in a later change is visible at a glance — re-run and update
this table when the core changes.

- **Environment:** Apple Silicon (arm64, macOS), CPython 3.12, marshmallow 4.3.0,
  release build (`maturin build --release`), `--number 6000`.
- **Recorded:** after Phase 6 (native int formatting in the JSON writer; native
  `Equal`/`NoneOf`/`ContainsOnly` validators). Earlier-phase numbers are in the
  git history.
- These are indicative ratios, not guarantees; absolute numbers vary by machine.

| case      | op    | stock (µs) | core (µs) | speedup |
|-----------|-------|-----------:|----------:|--------:|
| flat      | dump  |       2.16 |      0.55 |   3.95x |
| flat      | load  |       4.61 |      0.41 |  11.21x |
| flat      | dumps |       3.16 |      0.90 |   3.51x |
| flat      | loads |       5.47 |      1.05 |   5.21x |
| nested    | dump  |       5.39 |      0.56 |   9.63x |
| nested    | load  |      15.13 |      0.69 |  21.99x |
| nested    | dumps |       7.00 |      1.04 |   6.72x |
| nested    | loads |      16.89 |      1.97 |   8.57x |
| list      | dump  |      82.04 |      5.29 |  15.52x |
| list      | load  |     223.52 |      7.95 |  28.11x |
| list      | dumps |      99.80 |     10.95 |   9.12x |
| list      | loads |     241.75 |     22.48 |  10.75x |
| validator | dump  |       1.62 |      0.26 |   6.11x |
| validator | load  |       4.23 |      0.39 |  10.98x |
| validator | dumps |       2.44 |      0.53 |   4.60x |
| validator | loads |       5.01 |      1.12 |   4.47x |
| hooks     | dump  |       1.23 |      0.23 |   5.41x |
| hooks     | load  |       4.52 |      2.05 |   2.21x |
| hooks     | dumps |       2.06 |      0.48 |   4.25x |
| hooks     | loads |       5.23 |      2.76 |   1.90x |
| api       | dump  |     122.28 |     14.88 |   8.22x |
| api       | load  |     316.69 |     12.21 |  25.94x |
| api       | dumps |     144.03 |     21.06 |   6.84x |
| api       | loads |     339.28 |     31.70 |  10.70x |

## Reading the table

- **`api`** is the realistic mixed payload (paginated list of records with bool /
  str / int / float / datetime / list / nested fields) — the case to watch.
- **load** is the strongest direction on collections (26–28x). **dump** (Phase 5)
  and **dumps** (Phase 6 native int formatting) have closed much of the gap.
- **`dumps` is still ~2x `dump`** on float-heavy payloads — the remaining cost is
  `float.__repr__` per value, which can't be matched byte-for-byte in Rust
  (see `FAIRBACKLOG.md` Tier 1 / "not landed").
- **hooks** is the floor (~2x): marshmallow's Python hook dispatch around the
  core's per-field step, not movable into Rust.
- **`loads`** is bounded by CPython's C `json.loads` (~54% of the call is parsing
  + Python object construction, identical cost for any parser).

## Field-level wins not shown above (not in the standard cases)

Measured via targeted micro-benchmarks:

- typed `Dict` dump 7.5x · `Tuple` dump 4.1x · `Pluck(many)` dump 6.5x
- NaiveDateTime/AwareDateTime load 3.1x
- a schema using `Equal`/`NoneOf`/`ContainsOnly` validators: load 10.4x
  (previously callback)
