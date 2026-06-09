# Next backlog — Phase 5 speedup plan

Successor to [BACKLOG.md](BACKLOG.md) Phase 4 (now complete). Derived from the
post-Phase-4 numbers in [performance/RESULTS.md](performance/RESULTS.md). Ordered
by measured ROI.

**The new signal:** Phase 4 optimised *load* heavily, so **dump is now the
slower direction on collections** — `api` dump 20.1µs vs load 12.2µs; `list` dump
8.6µs vs load 7.7µs. Dump is where the absolute time now sits, and it is the most
constrained path (no `AccelFallback`). Both facts point Phase 5 at the dump core.

---

## Tier 1 — unlock the dump core with an `AccelFallback` (highest value)

Today the dump core has **no fallback**, so every native dump element must be
*provably identical* to `Field._serialize`. That rule is conservative, not
fundamental: dump has no side effects (it reads the object and builds a fresh
output), so on an edge case it could **discard the partial output and re-run the
pure-Python dump** — exactly what the load core already does. Adding this single
mechanism unlocks a whole class of deferred work.

- [ ] Add `AccelFallback` to the dump path: `DumpSerializer.run` raises it on any
      element it can't handle; `_patched_serialize` catches it and calls
      `_orig_serialize`. Propagate `KeyboardInterrupt`/`SystemExit` unchanged (as
      load does via `to_fallback`).
- [ ] Audit every dump `Element::apply` arm: an unhandled shape must raise
      `AccelFallback`, **never** a wrong/partial result. This is the correctness
      crux — write equivalence tests that force each defer.
- [ ] With the fallback in place, make **typed `Dict` / `Tuple` / `Pluck` dump
      native** (the three Phase 4 load-only fields) — apply key/value/positional
      elements, defer on a non-mapping / length-mismatch / per-entry error.
- [ ] Re-benchmark `api`/`list` dump; this is the main Phase 5 win.

## Tier 2 — dump hot-path micro-opts (collections)

- [ ] `get_one` runs `obj.hasattr("__getitem__")` **per field** (in `get_value`).
      For the common dict source that is a wasted attribute lookup on every field.
      Detect "this object is a dict" **once** per `run_one` and take a direct
      `get_item` path, falling back to the `hasattr`/`getattr` probe only for
      non-dict objects. Likely the single biggest dump micro-win.
- [ ] Profile `api` dump (samply / cargo-flamegraph against a Python harness) and
      record the next hot spot here **before** further guessing.
- [ ] Preallocate output `PyDict`/`PyList` capacity where the size is known
      (record count, field count) to cut rehashing/reallocation.

## Tier 3 — amortise first-call compilation

The per-schema payload is built lazily on first `dump`/`load` and cached **per
instance**. Apps that create a fresh `Schema()` per request pay the compile every
time.

- [ ] Add a per-**class** payload cache keyed on the structural inputs that change
      the payload (`only`, `exclude`, `dict_class`, `partial`-shape, field
      `data_key`s, `unknown`). Reuse across instances when the key matches; fall
      back to per-instance build on any miss. Measure cold-start (first-call) time,
      not just steady-state.
- [ ] Guard against context-dependent schemas (custom `get_attribute`,
      self-referential) — they must not share a cached payload.

## Tier 4 — widen native coverage (remaining callbacks)

- [ ] `NaiveDateTime` / `AwareDateTime` on **load** (currently deferred for the
      timezone normalisation) — model the common tz cases, defer the rest.
- [ ] `Email` / `URL` (String subclasses): the value passes through; only their
      regex validator differs. A native "regex validator" element (compile the
      pattern once, match in Rust) would turn these native — *spike first*, regex
      parity with Python `re` is the risk.
- [ ] `IPv4` / `IPv6` / `IP` / `IPInterface` — niche; do only if a real workload
      asks.
- [ ] Equivalence tests (valid + error) for each.

## Tier 5 — fused JSON writer (`dumps`)

`dumps` is fused but trails `dump`+overhead by the JSON pass (`list` dumps 15.5µs
vs dump 8.6µs). `loads` stays bounded by C `json.loads` (a Rust parser lost in
Phase 2 — don't retry).

- [ ] Profile the JSON writer; check float formatting (must match `json.dumps`
      exactly — `repr`-style shortest round-trip). If it is the cost, evaluate a
      shortest-round-trip formatter (e.g. ryu) **only if** byte-identical to
      `json.dumps`.
- [ ] Tighten string escaping (bulk-copy ASCII runs, escape only when needed).

---

## Structural ceilings — still not worth chasing

Carried over from Phase 4 (see [BACKLOG.md](BACKLOG.md)); re-confirmed:

- **Hook-bearing loads cap at ~2x.** ~90% of the call is marshmallow's Python
  hook dispatch (`_invoke_load_processors`, `_invoke_field_validators`) wrapping
  user callbacks; the core already runs the per-field step. Not movable into Rust
  without reimplementing the hook system. `@validates` methods are arbitrary
  Python (unlike `validate=Range/Length/OneOf`, which are already native).
- **`loads` is bounded by CPython's C `json.loads`.** A `serde_json` parser was
  slower in Phase 2.
- **By-design pure-Python:** custom `dict_class` / `get_attribute`,
  self-referential schemas, custom strptime formats, callable defaults.

## Recommended order

1. **Tier 1** (dump fallback) — unlocks the most and de-risks all future dump
   work; it is also the slow direction now.
2. **Tier 2 `get_one` dict fast-path** — small, cheap, measurable dump win.
3. Re-profile, then pick Tier 3/4/5 from evidence.
