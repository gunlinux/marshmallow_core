# Benchmark results

Per-call microseconds for stock pure-Python marshmallow vs. the installed Rust
core, with the speedup ratio, from `python -m performance.benchmark`. Recorded
so a regression in a later change is visible at a glance — re-run and update
this table when the core changes.

- **Environment:** Apple Silicon (arm64, macOS), CPython 3.12, marshmallow 4.3.0,
  release build (`maturin build --release`), `--number 6000`.
- **Recorded:** after Phase 4 (exact-type coercion skip, per-call prologue cache,
  native Boolean / strict Integer / typed Dict / Tuple / Pluck / TimeDelta loads).
- These are indicative ratios, not guarantees; absolute numbers vary by machine.

| case      | op    | stock (µs) | core (µs) | speedup |
|-----------|-------|-----------:|----------:|--------:|
| flat      | dump  |       2.09 |      0.63 |   3.33x |
| flat      | load  |       4.62 |      0.41 |  11.33x |
| flat      | dumps |       3.10 |      1.02 |   3.04x |
| flat      | loads |       5.49 |      1.08 |   5.09x |
| nested    | dump  |       5.24 |      0.76 |   6.88x |
| nested    | load  |      15.24 |      0.70 |  21.81x |
| nested    | dumps |       6.89 |      1.36 |   5.05x |
| nested    | loads |      16.79 |      2.05 |   8.20x |
| list      | dump  |      81.99 |      8.64 |   9.49x |
| list      | load  |     222.98 |      7.71 |  28.93x |
| list      | dumps |      97.84 |     15.49 |   6.31x |
| list      | loads |     242.05 |     22.63 |  10.70x |
| validator | dump  |       1.59 |      0.33 |   4.86x |
| validator | load  |       4.14 |      0.38 |  10.93x |
| validator | dumps |       2.51 |      0.64 |   3.93x |
| validator | loads |       5.00 |      1.15 |   4.36x |
| hooks     | dump  |       1.24 |      0.29 |   4.27x |
| hooks     | load  |       4.56 |      2.13 |   2.14x |
| hooks     | dumps |       2.01 |      0.58 |   3.44x |
| hooks     | loads |       5.36 |      2.82 |   1.90x |
| api       | dump  |     119.57 |     20.07 |   5.96x |
| api       | load  |     310.43 |     12.16 |  25.53x |
| api       | dumps |     142.55 |     26.91 |   5.30x |
| api       | loads |     334.91 |     32.34 |  10.36x |

## Reading the table

- **`api`** is the realistic mixed payload (paginated list of records with bool /
  str / int / float / datetime / list / nested fields) — the case to watch.
- **load** is the strongest direction on collections (25–29x) — the per-field
  Python loop is what the core removes most completely.
- **hooks** is the floor (~2x): a schema with `pre_load`/`post_load`/`validates`
  runs marshmallow's Python hook dispatch around the core's per-field step, and
  that dispatch can't move into Rust. See `ROADMAP.md` / `BACKLOG.md` "Structural
  ceilings".
- **dumps/loads** track dump/load; `loads` is bounded by CPython's C `json.loads`.
