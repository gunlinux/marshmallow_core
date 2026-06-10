# Benchmark results

Per-call microseconds for stock pure-Python marshmallow vs. the installed Rust
core, with the speedup ratio, from `python -m performance.benchmark`. Recorded
so a regression in a later change is visible at a glance — re-run and update
this table when the core changes.

- **Environment:** Apple Silicon (arm64, macOS), CPython 3.12, marshmallow 4.3.0,
  release build (`maturin build --release`), `--number 6000`.
- **Recorded:** after Phase 5 (dump `AccelFallback`; native typed Dict / Tuple /
  Pluck **dump**; exact-dict `get_one` fast path; native NaiveDateTime/
  AwareDateTime load; bulk-copy JSON string escaper). Phase 4 numbers are in the
  git history.
- These are indicative ratios, not guarantees; absolute numbers vary by machine.

| case      | op    | stock (µs) | core (µs) | speedup |
|-----------|-------|-----------:|----------:|--------:|
| flat      | dump  |       2.17 |      0.56 |   3.89x |
| flat      | load  |       4.75 |      0.42 |  11.27x |
| flat      | dumps |       3.26 |      0.97 |   3.36x |
| flat      | loads |       5.60 |      1.12 |   4.99x |
| nested    | dump  |       5.45 |      0.58 |   9.32x |
| nested    | load  |      15.61 |      0.72 |  21.60x |
| nested    | dumps |       7.23 |      1.19 |   6.10x |
| nested    | loads |      17.29 |      2.10 |   8.23x |
| list      | dump  |      83.56 |      6.20 |  13.49x |
| list      | load  |     227.66 |      8.73 |  26.06x |
| list      | dumps |     100.31 |     12.25 |   8.19x |
| list      | loads |     245.76 |     24.13 |  10.19x |
| validator | dump  |       1.61 |      0.28 |   5.81x |
| validator | load  |       4.21 |      0.40 |  10.60x |
| validator | dumps |       2.52 |      0.59 |   4.26x |
| validator | loads |       5.17 |      1.18 |   4.40x |
| hooks     | dump  |       1.23 |      0.24 |   5.14x |
| hooks     | load  |       4.58 |      2.13 |   2.15x |
| hooks     | dumps |       2.05 |      0.50 |   4.07x |
| hooks     | loads |       5.28 |      2.79 |   1.89x |
| api       | dump  |     119.88 |     15.80 |   7.59x |
| api       | load  |     314.43 |     12.42 |  25.31x |
| api       | dumps |     143.45 |     22.04 |   6.51x |
| api       | loads |     339.46 |     32.94 |  10.30x |

## Reading the table

- **`api`** is the realistic mixed payload (paginated list of records with bool /
  str / int / float / datetime / list / nested fields) — the case to watch.
- **load** is the strongest direction on collections (25–26x). After Phase 5,
  **dump** caught up substantially (list dump 13.5x, api dump 7.6x) via the
  exact-dict `get_one` fast path and the JSON escaper.
- **hooks** is the floor (~2x): a schema with `pre_load`/`post_load`/`validates`
  runs marshmallow's Python hook dispatch around the core's per-field step, and
  that dispatch can't move into Rust. See "Structural ceilings" in `BACKLOG.md` /
  `NEXTBACKLOG.md`.
- **dumps/loads** track dump/load; `loads` is bounded by CPython's C `json.loads`.

## Field-level wins not shown above (not in the standard cases)

Measured via targeted micro-benchmarks:

- typed `Dict` dump: 7.5x · `Tuple` dump: 4.1x · `Pluck(many)` dump: 6.5x
- NaiveDateTime/AwareDateTime load: 3.1x
