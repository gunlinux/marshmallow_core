# Fair backlog — Phase 6 speedup plan

Successor to [NEXTBACKLOG.md](NEXTBACKLOG.md) Phase 5 (now complete). Derived from
the post-Phase-5 numbers in [performance/RESULTS.md](performance/RESULTS.md).
Ordered by measured ROI.

We are now in **diminishing-returns territory**: the big per-element loops (load
and dump) and the JSON string escaper are done. The remaining work is incremental
formatting/coverage micro-opts plus standing ceilings. **Profile before building.**

**Status: Phase 6 complete.** Landed: native **int** formatting in the JSON
writer (Tier 1); native **Equal/NoneOf/ContainsOnly** validators (Tier 3, listed
below as part of "more native validators"). **Investigated, not landed:** native
float formatting (Tier 1 — no byte-identical parity with `repr`), native datetime
isoformat dump (Tier 2 — blocked by abi3: datetime C accessors aren't in the
limited ABI), preallocation (Tier 4 — presized-dict ctor is private), SIMD parser
(spike — object construction dominates `loads`, same as Phase 2). See each item's
note. Numbers: list `dumps` 12.25→10.95µs; api `dumps` 22.04→21.06µs.

**The new signal:** `dumps` is consistently **~2x `dump`** even though the fused
writer skips the intermediate dict — list `dumps` 12.25µs vs `dump` 6.20µs; api
22.04 vs 15.80; flat 0.97 vs 0.56. Building the JSON *string* costs more than
building the dict, which means the per-scalar **formatting** in the writer
(`int.__str__` / `float.__repr__` called per value, ~200 Python calls for the
50-record list) is the dominant remaining `dumps` cost. That is where Phase 6
starts.

---

## Tier 1 — native scalar formatting in the JSON writer (`dumps`)

`write_json_value` formats each number by calling back into Python
(`value.str()` for ints, `value.repr()` for floats). For a record list that is
hundreds of Python calls. Format in Rust instead.

- [x] **Profile `run_json`** (api/list) to confirm the per-scalar formatting cost
      before writing code — record the split (formatting vs structure vs escaping).
- [x] **Native `int` formatting** (safe): if the value fits `i64`/`u64`, format
      with `itoa` directly; arbitrary-precision ints fall back to `value.str()`.
      Integer text is identical across Python and Rust, so this is byte-safe.
- [~] **Native `float` formatting** (*spike — NOT LANDED*): no byte-identical parity
      with Python `repr`/json (`1.0` vs `1`, `1e+16` vs ryu `1e16`, `1e-05` vs `1e-5`,
      fixed/scientific threshold); a mismatch is wrong-but-valid JSON with no fallback.
      Floats keep `value.repr()`. Original note: Python/`json` use
      `float.__repr__` (shortest round-trip). `ryu` is shortest round-trip too but
      its *formatting* differs (`1e20` vs Python `1e+20`, exponent threshold,
      `-0.0`, `inf`/`nan` already special-cased). Land only if a normalization
      layer is **byte-identical** to `repr` across a fuzz corpus; otherwise keep
      `value.repr()` (no regression — the win is the int path).
- [x] Equivalence: the existing `dumps` tests already assert byte-identity; add a
      numeric fuzz case (large ints, negatives, floats incl. `1e16`, `0.1`, `-0.0`).

## Tier 2 — native datetime `isoformat` dump (with fallback)

DateTime dump calls the field's serialization func (`utils.isoformat` →
`value.isoformat()`) once per value — a Python call per datetime (the api `created`
field is one per record). The dump core now has an `AccelFallback`, so this can be
attempted safely.

- [~] (NOT LANDED — blocked by abi3: datetime C accessors absent from the limited
      ABI; getattr-based extraction would be slower than `isoformat()`.) Build the ISO string in Rust from `PyDateTime` components for the common
      case (naive or fixed-offset, default format), and **defer** (fallback) on
      anything fiddly (custom format, unusual tz, microsecond-trimming edge cases)
      so the output stays byte-identical to `isoformat()`.
- [ ] Equivalence (valid + each deferred edge): naive, aware, microseconds=0,
      microseconds!=0, non-UTC offset.

## Tier 3 — more native validators

Only `Range`/`Length`/`OneOf` are native; the rest force the field onto the
callback path. These are cheap set/equality checks; on failure they defer so the
exact (possibly custom) message is reproduced.

- [x] `Equal` (`value == comparable`), `NoneOf` (`value not in iterable`),
      `ContainsOnly` (`set(value) <= set(choices)`). All decision-only, like the
      existing validators.
- [ ] `Regexp` — only if the pattern is trivial enough for guaranteed parity;
      otherwise skip (cf. the Email/URL spike in NEXTBACKLOG).
- [x] Equivalence: pass + fail inputs for each.

## Tier 4 — profile-guided micro-opts

- [ ] Profile api `load` (12.4µs) and `dump` (15.8µs); record the next hot spot
      here before guessing.
- [~] (NOT LANDED — presized-dict ctor `_PyDict_NewPresized` is private/not in
      pyo3; list preallocation marginal.) Preallocate output `PyDict`/`PyList` capacity where the size is known
      (record count, field count) — the item deferred from Phase 5 Tier 2. Only if
      the profile shows allocation/rehash cost.

## Spikes — probably not, but bounded by measurement

- [x] **Fused `loads` via jiter (Design A) — LANDED.** The earlier spikes had the
      framing wrong: they measured a *tokenizer* swap (Phase 2 `serde_json`, the
      SIMD idea) that still materialised the full Python dict `json.loads` builds,
      so they could not beat C `json.loads`. The actual cost is that intermediate
      dict — built once by `json.loads`, read back out and discarded by
      `_do_load`. pydantic-core avoids it by parsing to a cheap *Rust* tree
      (jiter's `JsonValue`, keys borrowed as `Cow<str>`, no `PyObject`s) and
      constructing only the kept fields. We mirror that: `_patched_loads` parses
      with `jiter::JsonValue` and deserialises off the tree
      (`LoadDeserializer.run_json`), threading the tree through `Nested`/`List` so
      a list-of-records never materialises an intermediate. Output keys come from
      the schema (already-interned `out_key`s), so it allocates **zero** per-record
      key strings — the win json.loads can't match. Scalars convert leaf→Python via
      `json_to_py` and run the unchanged `apply`, so parity holds by construction.
      Measured **1.2–1.8× over the prior accelerated `loads`** (flat 1.04→0.64µs
      1.62×; api 32.4→27.4µs 1.21×; list 25→18µs ~1.3×); ~8.5–14× over stock.
      Defers to stock `loads` for callback fields, hooks, non-`json` render module,
      extra kwargs, and big ints (jiter built without `num-bigint` to keep its
      optional pyo3 0.28 dep out of our pyo3 0.27 build). Equivalence + error
      parity covered in `tests/test_equivalence.py` (`test_loads_*`).

## Structural ceilings — still not worth chasing

Re-confirmed from Phases 4–5 (see [BACKLOG.md](BACKLOG.md) / [NEXTBACKLOG.md](NEXTBACKLOG.md)):

- **Hook-bearing loads cap at ~2x** — marshmallow's Python hook dispatch around
  the core's per-field step; not movable into Rust.
- **`loads` is bounded by CPython's C `json.loads`** (see the SIMD spike above).
- **Email/URL native regex** — false-positive validation risk, no byte-identical
  parity, heavy dep. Not landed (NEXTBACKLOG).
- **Per-class payload cache** — shares instance-bound references; correctness
  hazard. Not landed (NEXTBACKLOG).
- **By-design pure-Python:** custom `dict_class` / `get_attribute`, self-referential
  schemas, custom strptime formats, callable defaults, `Function`/`Method` fields.

## Recommended order

1. **Profile `run_json`**, then **Tier 1 native int formatting** — the clearest
   remaining win (`dumps` is ~2x `dump`), and byte-safe.
2. **Tier 2 datetime isoformat** if the profile shows the datetime call is hot.
3. **Tier 3 validators** — small, safe, helps validator-heavy schemas.
4. Treat the float formatter and SIMD parser as *spikes*: land only on proven,
   byte-identical gains, exactly as Phase 5 handled the regex validator and cache.
