# Roadmap — extending the speedup

A prioritized plan for pushing marshmallow_core's acceleration further. Ordered
so each phase is measurable and lands the highest return-on-effort first.

**Guiding rule:** measure before and after, and every new native path needs an
accel-on == accel-off equivalence case in `tests/test_equivalence.py` across
**valid and error** inputs.

## Phase 0 — Measurement foundation (do this first)

You can't tune a speedup you can't see; there's no benchmark in the package yet.

- [ ] **Port a benchmark harness.** Adapt the fork's `performance/benchmark.py`
      and `performance/analyze_paths.py` into a `performance/` dir here, driven
      through `marshmallow_core.install()` / `uninstall()` (not
      `MARSHMALLOW_NO_ACCEL`). Output a table of stock vs. core for `dump`,
      `load`, `dumps`, `loads` on representative schemas (flat scalars, nested,
      list-heavy, validator-heavy).
- [ ] **Add a coverage probe.** A helper that, for a given schema, reports
      per-field whether it compiled **native** vs **callback** (inspect the
      payload). Tells you exactly which fields still fall back in a real
      workload, and directs every later phase.

## Phase 1 — Close the biggest coverage gaps (highest hit-rate)

Where real schemas currently fall back to Python. Phase 0 confirms the ordering.

- [ ] **Accelerate hook-bearing load schemas.** Today any `pre_load` /
      `post_load` / `validates` / `validates_schema` sends the whole load to
      Python (the v1 cut in `_patch.py`). Reproduce the upstream branch's split:
      run `pre_load` in Python → call the core for the per-field deserialize →
      run field/schema validators and `post_load` in Python around it. Requires
      partially reimplementing `_do_load`'s body in the wrapper rather than a
      clean try/fallback. *Highest value, highest risk* — pin to marshmallow 4.x
      internals and lean on the equivalence suite.
- [ ] **Native common validators on load.** A field with any validator currently
      becomes a callback. Model the frequent ones — `Range`, `Length`, `OneOf` —
      natively in `lib.rs`; unrecognized validators still fall back. Validators
      are very common, so this likely beats the item above on aggregate impact.
- [ ] **More native field types.** Add `Decimal`, `Dict` / `Mapping`
      (dict-of-values), and `Constant`. Each goes in **both** `_compiler.py`
      (build the element tuple) and `lib.rs` (parse + apply), tags kept in sync.
      `Number` base coercion shared with Integer/Float.

## Phase 2 — Fused JSON path (biggest ceiling, biggest effort)

Only `_serialize` / `_deserialize` are accelerated today; `dumps` / `loads`
still go through stdlib `json`, usually the dominant cost for the string APIs.

- [ ] **Dump → JSON bytes in Rust.** A fused serializer that writes JSON
      directly, skipping the intermediate Python dict; wrap `Schema.dumps`. A
      new core object, not just a new field type.
- [ ] **JSON → loaded structure in Rust.** The symmetric `loads` path: parse
      JSON in Rust straight into the deserialized result, with the same
      `AccelFallback` safety net for any edge case.

Large architectural addition, but by far the highest ceiling for the `*s` APIs
most apps actually call.

## Phase 3 — Remaining deferrals + micro-opts

- [ ] Pick off remaining pure-Python cases as workloads demand: `unknown=INCLUDE`,
      collection/dotted `partial`, dotted attribute writes (needs `set_value`).
- [ ] Micro-opts: cache `_has_load_hooks` on the class instead of recomputing per
      `_do_load` call; precompute the known-keys frozenset once.

## Cross-cutting invariants (every code step)

- Add native support in **both** `_compiler.py` and `lib.rs`; keep the **distinct
  dump/load tag spaces in sync**.
- Any payload/tag shape change → bump `PROTOCOL_VERSION` (`lib.rs`) **and**
  `_EXPECTED_PROTOCOL` (`_compiler.py`) together.
- The **dump core has no `AccelFallback`** — a native dump element must be
  provably identical to `Field._serialize` for *every* input. When unsure, leave
  it a callback.
- Every change: extend `tests/test_equivalence.py` (valid + error inputs) and
  re-run the Phase 0 benchmark to confirm a real gain, not a regression from
  added compile overhead.
