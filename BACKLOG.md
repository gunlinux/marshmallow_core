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

## Per-change checklist (every code item)

- [ ] Implemented in **both** `_compiler.py` and `lib.rs`
- [ ] Dump/load tags kept in sync
- [ ] Bumped `PROTOCOL_VERSION` + `_EXPECTED_PROTOCOL` if payload/tags changed
- [ ] Equivalence test added (valid + error inputs)
- [ ] Re-ran benchmark to confirm a real gain
