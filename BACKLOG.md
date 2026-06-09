# Backlog

Flat, actionable todo list derived from [ROADMAP.md](ROADMAP.md). Work top to
bottom within a phase.

## Phase 0 — Measurement

- [x] Create `performance/` directory
- [x] Port `benchmark.py` from the fork; drive it via `install()`/`uninstall()`
- [x] Add flat-scalar schema benchmark case
- [x] Add nested schema benchmark case
- [x] Add list-heavy schema benchmark case
- [x] Add validator-heavy schema benchmark case
- [x] Benchmark `dump`, `load`, `dumps`, `loads` for each case
- [x] Print a stock-vs-core comparison table
- [x] Write a coverage probe: report per-field native vs callback for a schema
- [x] Document how to run the benchmark in README

## Phase 1 — Coverage gaps

### Hook-bearing load schemas
- [x] Map out marshmallow 4.x `_do_load` body (pre_load → deserialize → validators → post_load)
- [x] In `_patched_do_load`, run `pre_load` in Python before the core step
- [x] Call the core for the per-field deserialize when hooks are present
- [x] Run field validators in Python after the core step
- [x] Run schema validators (`validates_schema`) in Python
- [x] Run `post_load` in Python after the core step
- [x] Remove the `not _has_load_hooks(self)` guard once the split works
- [x] Add equivalence tests for pre_load + post_load + validates schemas

### Native validators
- [x] Add a validator tag space (compiler + lib.rs)
- [x] Native `Range` validator
- [x] Native `Length` validator
- [x] Native `OneOf` validator
- [x] Compile only recognized validators; fall back on any other
- [x] Equivalence tests: pass + fail inputs for each validator

### New field types
- [x] Native `Decimal` (dump + load)
- [x] Native `Dict`/`Mapping` (dump + load) — plain dict-copy case
- [x] Native `Constant`
- [x] Share `Number` base coercion with Integer/Float — Decimal reuses the
      field's own (`Number`-based) `_serialize`/`_deserialize` rather than
      duplicating coercion in Rust
- [x] Equivalence tests for each new field type (valid + error)

## Phase 2 — Fused JSON path

- [x] Add a dump→JSON-bytes core object in `lib.rs` (`DumpSerializer.run_json`)
- [x] Build the JSON-dump payload in `_compiler.py` (`build_dump_json_serializer`)
- [x] Wrap `Schema.dumps` in `_patch.py`
- [x] Equivalence tests: `dumps` output matches stock
- [~] Add a JSON→loaded core object in `lib.rs` — **implemented + benchmarked,
      not shipped.** A `serde_json`-backed single-pass `LoadDeserializer.run_json`
      was prototyped, but it is consistently *slower* than CPython's C `json.loads`
      followed by the already-accelerated `_do_load` (e.g. nested 2.12µs vs 1.96µs;
      100-record list 60µs vs 37µs). `loads` already benefits from the `_do_load`
      patch, so fusing it only adds a heavy dependency and a regression. Reverted
      per the roadmap's "confirm a real gain, not a regression" rule.
- [~] Build the JSON-load payload in `_compiler.py` — see above (reverted)
- [~] Wrap `Schema.loads` in `_patch.py` with `AccelFallback` — see above (reverted)
- [~] Equivalence tests: `loads` result + errors match stock — verified during the
      prototype (big-int/error/unknown-key parity held); dropped with the revert

## Phase 3 — Remaining deferrals + micro-opts

- [x] Support `unknown=INCLUDE`
- [x] Support collection/dotted `partial`
- [x] Support dotted attribute writes (`set_value`)
- [x] Cache `_has_load_hooks` on the class (per-class `WeakKeyDictionary`)
- [x] Precompute the known-keys frozenset once (built per compile, held by the
      Rust `LoadSerializer`)

## Phase 4 — Post-analysis speedup

Derived from the profiling pass (Opus, marshmallow 4.3.0, via
`performance/benchmark.py` + a layer-by-layer probe of public → patched →
raw-Rust). Ordered by measured ROI. The first item of that pass —
**native `Boolean` load** — is already done (list load 28.7µs → 9.5µs,
8.0x → 23.8x; it now matches list dump). The rest, top to bottom:

**Status: Phase 4 complete.** All tiers landed. The composite fields
(typed `Dict`, `Tuple`, `Pluck`) are accelerated on **load only** — the dump
core has no `AccelFallback` and these iterate arbitrary mappings / use
`zip(strict=True)`, so a provably-identical dump can't be guaranteed; their
dump stays on the callback path (a possible future item if the dump core grows
a fallback). Post-Phase-4 numbers are in [performance/RESULTS.md](performance/RESULTS.md).

### Tier 1 — hot-path Rust micro-opts (collections; highest absolute time)
- [x] Skip redundant scalar coercion when the input is already the exact target
      type. `int(x)`/`float(x)` for an *exact* `int`/`float` return `x`
      unchanged, so short-circuit `LoadElement::Int`/`Float` (and dump
      `Element::Int`/`Float`) to `value.clone()` when
      `value.is_exact_instance_of::<PyInt/PyFloat>()` — drops a Python C-API call
      per scalar on the common case. Especially helps `loads`, where
      `json.loads` already yields real ints/floats. Verify identity equivalence
      (int subclasses are excluded by `is_exact_instance_of`; bools already
      rejected before this point).
- [x] Skip `Partial::derive` per field for non-recursive elements. It is called
      on every field in `run_one`, but only `Nested`/`List`/callback consume the
      sub-partial; for scalar `Native` fields it is a wasted call + match. Gate it
      on element kind (or on `partial` being non-`None`).
- [x] Re-profile the post-Boolean list-load and record the next hot spot in this
      file *before* writing more micro-opts — don't guess twice.

### Tier 2 — shrink the fixed per-call Python overhead (small/flat schemas)
The `dump`/`load` entry prologue is ~20–30% of a small-schema call (flat load:
0.63µs raw Rust, 0.89µs through `_do_load`). It is constant per call, so it only
shows up when the payload is tiny — but those are exactly the tight-loop cases.
- [x] Cache the load dispatch decision per instance alongside the deserializer:
      precompute `_core_partial(self.partial)` and the has-hooks branch so the
      common `partial=None, unknown=default` call skips `_partial_is_supported` /
      `_has_load_hooks` / `_core_partial`.
- [x] Store the hook-aware vs direct runner as the cached object (chosen once at
      compile) so `_has_load_hooks` is not consulted on every load.
- [x] Trim the `_patched_serialize` / `_patched_do_load` prologue (avoid
      re-reading `self.many` / `self.unknown` once args are normalized).
- [x] Re-benchmark flat dump/load to confirm the fixed-overhead drop.

### Tier 3 — widen native load coverage (turn remaining callbacks native)
- [x] Run `performance/analyze_paths` over a realistic API schema; list every
      field still on the callback path (start the audit here, then pick targets).
- [x] Native `Integer(strict=True)` (`numbers.Integral` check).
- [x] Native typed `Dict` (key field + value field applied per entry) — currently
      only the untyped dict-copy case is native.
- [x] Native `Tuple` (fixed-length tuple of fields).
- [x] Native `Pluck` (the `Nested` subclass that plucks one field) — common in
      API schemas.
- [x] Native `TimeDelta`.
- [x] Equivalence tests (valid + error) for each.

### Tier 4 — measurement & guardrails
- [x] Add an API-response-shaped benchmark case (mixed bool/str/int/nested/list)
      so real-world regressions are visible, not just the four synthetic shapes.
- [x] Record the per-case speedup numbers (in the benchmark docstring or a
      committed results file) so a regression in a later change is obvious.

### Structural ceilings — analyzed, *not* worth accelerating
Documented so they are not re-investigated:
- **Hook-bearing loads cap at ~2x.** ~90% of the call is marshmallow's Python
  hook dispatch (`_invoke_load_processors` ×2, `_invoke_field_validators`)
  wrapping user callbacks; the core already runs the per-field step (~8%). Not
  movable into Rust short of reimplementing the hook system.
- **`loads` is bounded by CPython's C `json.loads`.** A `serde_json` parser was
  prototyped in Phase 2 and was slower; the per-field step is already
  accelerated. Don't retry without a fundamentally faster parser.
- **By-design pure-Python:** custom `dict_class` / `get_attribute`,
  self-referential schemas, custom strptime formats, callable defaults,
  `NaiveDateTime` / `AwareDateTime` on load, and any validator outside
  `Range` / `Length` / `OneOf` (correctness over speed).

## Per-change checklist (every code item)

- [x] Implemented in **both** `_compiler.py` and `lib.rs`
- [x] Dump/load tags kept in sync
- [x] Bumped `PROTOCOL_VERSION` + `_EXPECTED_PROTOCOL` if payload/tags changed
- [x] Equivalence test added (valid + error inputs)
- [x] Re-ran benchmark to confirm a real gain
