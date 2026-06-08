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
- [ ] Map out marshmallow 4.x `_do_load` body (pre_load → deserialize → validators → post_load)
- [ ] In `_patched_do_load`, run `pre_load` in Python before the core step
- [ ] Call the core for the per-field deserialize when hooks are present
- [ ] Run field validators in Python after the core step
- [ ] Run schema validators (`validates_schema`) in Python
- [ ] Run `post_load` in Python after the core step
- [ ] Remove the `not _has_load_hooks(self)` guard once the split works
- [ ] Add equivalence tests for pre_load + post_load + validates schemas

### Native validators
- [x] Add a validator tag space (compiler + lib.rs)
- [x] Native `Range` validator
- [x] Native `Length` validator
- [x] Native `OneOf` validator
- [x] Compile only recognized validators; fall back on any other
- [x] Equivalence tests: pass + fail inputs for each validator

### New field types
- [ ] Native `Decimal` (dump + load)
- [ ] Native `Dict`/`Mapping` (dump + load)
- [ ] Native `Constant`
- [ ] Share `Number` base coercion with Integer/Float
- [ ] Equivalence tests for each new field type (valid + error)

## Phase 2 — Fused JSON path

- [ ] Add a dump→JSON-bytes core object in `lib.rs`
- [ ] Build the JSON-dump payload in `_compiler.py`
- [ ] Wrap `Schema.dumps` in `_patch.py`
- [ ] Equivalence tests: `dumps` output matches stock
- [ ] Add a JSON→loaded core object in `lib.rs`
- [ ] Build the JSON-load payload in `_compiler.py`
- [ ] Wrap `Schema.loads` in `_patch.py` with `AccelFallback`
- [ ] Equivalence tests: `loads` result + errors match stock

## Phase 3 — Remaining deferrals + micro-opts

- [ ] Support `unknown=INCLUDE`
- [ ] Support collection/dotted `partial`
- [ ] Support dotted attribute writes (`set_value`)
- [ ] Cache `_has_load_hooks` on the class
- [ ] Precompute the known-keys frozenset once

## Per-change checklist (every code item)

- [ ] Implemented in **both** `_compiler.py` and `lib.rs`
- [ ] Dump/load tags kept in sync
- [ ] Bumped `PROTOCOL_VERSION` + `_EXPECTED_PROTOCOL` if payload/tags changed
- [ ] Equivalence test added (valid + error inputs)
- [ ] Re-ran benchmark to confirm a real gain
