# qudv-iso80000

`iso80000_converter.py` reads the OMG SysML QUDV model library for ISO/IEC 80000
and writes its quantity kinds, dimensions, units, exact conversion factors and
unit symbols as YAML. Use `--format catalog` to retain the source declarations
and relationships; the default `scalars` format is the older multiplicative
unit-family projection.

The source is the machine-readable OMG library, not the ISO documents. It is not
checked in (92 MB). Download it once:

```bash
curl -L -o ISO-80000.xmi http://www.omg.org/spec/SysML/20150709/ISO-80000.xmi
```

## Usage

```bash
uv run python iso80000_converter.py ISO-80000.xmi -o iso80000.yml
uv run python iso80000_converter.py ISO-80000.xmi --format catalog -o catalog.yml
uv run python iso80000_converter.py ISO-80000.xmi --format catalog --corrections iso80000-corrections.yml -o corrected-catalog.yml
uv run python iso80000_converter.py ISO-80000.xmi --corrections iso80000-corrections.yml -o corrected-scalars.yml
uv run python iso80000_converter.py ISO-80000.xmi --strict
```

Without `-o` the YAML goes to stdout. Progress, applied corrections and problems
go to stderr. `--strict` exits 1 after writing output if the selected format has
gaps. Supported affine units are scalar exclusions, not catalog problems, so
they do not fail catalog strict mode. The seven unresolved generalized kinds
still fail strict mode in both formats. Invalid corrections exit 2 before any
output file is written. Diagnostic counts are not counts of distinct omissions.

## Source-preserving catalog (schema version 2)

The catalog is an intermediate source graph, not an executable Physica model.
It has these sections:

- `source`: input SHA-256 and format. IDs are scoped to this source hash; a
  same-looking name in another catalog does not establish identity.
- `declarations`: all XMI instance specifications keyed by their original IDs,
  including kinds, units, prefixes, constants, factors, systems and association
  instances. Each has its source class/name/classifier references, `slots` and
  full defining-feature URIs in `features`. Local slot names are conveniences.
- Slot values are `{ref: source_id}`, `{href: external_uri}`, typed literal
  text, or an opaque XML tree. Numeric specifications retain their original
  body/language tree. Expressions are data; arbitrary source code is never run.
- `resolved`: separately derived kind dimensions, factor expressions and
  unresolved dependency IDs; unit names, symbols, quantity-kind relationships,
  multiplicative SI factors and conversions; and source numbers.
  Exact numbers use `rational` and `pi_exponent`;
  approximate numbers use only `approximate`. Missing dimensions/factors are
  `null`, never a fabricated dimension-one value or identity conversion.
- `diagnostics`: `problems` and `scalar_exclusions` carry source IDs, categories,
  subjects and reasons. Categories distinguish missing relationships/references,
  cycles, unresolved dimensions, dimensional mismatches and unsupported
  conversions. `corrections` records inference decisions.
- `applied_corrections`: the full explicitly selected correction manifest and
  its SHA-256, or `null`. Changes affect the derived view, never source slots.

Affine conversions are resolved and composed through affine, linear, prefix
and unit-alias chains. Each resolved unit's `conversion` contains an explicit
terminal `reference_unit` ID, `scale` and `offset`:

```text
absolute value: reference_value = scale * value + offset
difference:     reference_difference = scale * difference
```

These factors target the named reference unit, which is not necessarily the SI
unit (a mass chain can terminate at gram). `si_factor` remains the separate
multiplicative SI factor. For corrected Celsius the reference is kelvin, scale
is `1` and offset is `5463/20`. For milli-Celsius the scale is `1/1000` and the
offset is unchanged. Zero-scale conversions are unresolved. Mixed exact offset
terms use `sum: [{rational: ..., pi_exponent: ...}, ...]`, preserving rational
plus pi expressions without rounding.

Affine point units have no multiplicative `si_factor`, even when reached through
a prefix or reference chain or used in a derived product. Differences use the
conversion scale; the exporter does not invent separate interval quantity kinds
or authorize conversions solely from matching dimensions.

General nonlinear conversions retain expression/language slots with a null
`conversion`; the exporter does not evaluate them.
Unsupported numeric expressions remain in declarations and are
reported. If one prevents derived unit analysis, that derived view is empty
rather than partially published; independently resolvable numbers remain.

The catalog includes unresolved kinds, fractional dimension exponents, and
exact powers of pi that cannot be represented by the legacy scalar format.
Duplicate XMI IDs or colliding local slot names are rejected, not overwritten.
Output has no timestamps or machine-local paths and is deterministic for the
same source and exporter. The source SHA-256 identifies bytes, not correctness.

## What it emits

Against the 2015-07-09 library, scalar output contains **317 of 325 quantity
kinds**: 7491 memberships without corrections, or **7680 memberships and 2774
distinct units** with `iso80000-corrections.yml`. The corrected catalog retains
all **325 kinds and 2795 units**, with conversions resolved for every unit.
The remaining 21 units are affine Celsius units; the remaining seven unresolved
kind dimensions depend on generalized coordinate.
Unit entries count memberships in families, not distinct units.
Every emitted family has resolved dimensions, at least one unit, and a canonical
unit whose factor is exactly 1. Nothing is emitted with `dimensions: {}` as a
placeholder; `{}` means dimension one.

```yaml
scalars:
- name: Mass
  dimensions:
    M: 1
  canonical: Kilogram
  units:
    Gram:
      factor: 1/1000
      symbol: g
    Kilogram:
      factor: 1
      symbol: kg
    Tonne:
      factor: 1000
      symbol: t
```

Dimension keys are `L M T I Θ N J`. Factors are exact where the library is exact:
integers, `n/d` strings, and `n*pi/d` strings (`DegreeAngle: pi/180`). Only
`ln(10)` (bel) becomes a float.

## How the library is read

Everything is keyed by `xmi:id`, from the class instances and two kinds of link
instance (`A_quantityKind_measurementUnit`, `A_systemOfUnits_baseUnit`).

**Resolution order**: quantity-kind assignments, inherited units, dimensions,
SI factors and conversions use topological dependency passes. Cycles are
reported separately from missing references; independent nodes still resolve.
For dimension alternatives, an available definition can anchor a cyclic group.
There is no recursion-depth limit on conversion chains.

**Dimensions of a kind**: the explicit dimension-one flag; base quantity;
entity-count flag; the kind's own `factor` products; its `general` kind; a
`DerivedUnit` that measures it. The count flag resolves winding counts. It is
also set on amount of substance in this source: its established base dimension
is retained and the contradiction reported. This follows the distinction
between counts and amount of substance, not a winding-specific rule.

**Units of a kind**: a unit lists zero or more kinds in its `quantityKind` slot
and through measurement-unit links (newton measures both force and weight). A
unit that names no kind takes the kinds of its reference unit, then of its
`general` unit. A kind with no unit of its own takes the units of its nearest
`general` kind that has some.

**Factors** are relative to the coherent SI unit of the same dimensions. SI base
units are 1, so a base unit's reference is its inverse: the library makes gram the
simple unit of mass and kilogram a prefixed unit of it, and gram comes out as
1/1000. Prefixed and linear-conversion units multiply along their reference
chain (day -> hour -> minute -> second). Derived units multiply their unit
factors. A simple unit that is another unit under a special name (watt is joule
per second) takes that unit's factor; every other simple unit in the library
(volt, ohm, henry, tesla, weber ...) is the coherent unit of its kind.

A unit whose dimensions differ from the kind it claims to measure is not emitted
under that kind.

## Contradictions in the library

Where the library contradicts itself the converter resolves it, says how, and
lists it under "contradictions" in the output header:

- The factor instance named `electric charge^-1` has an exponent slot of `1`.
  Every factor is named `target^exponent`; the name is used. With the slot value,
  electric field strength, potential, flux, power and every kind built on them
  get dimensions that disagree with their own units; with the name they agree.
- `initial phase of electric current` and `initial phase of electric voltage` are
  flagged dimension one but carry factors (current, voltage). The flag is used.

The library's Celsius affine unit refers to the offset **273.16**, whereas the
[BIPM SI Brochure](https://www.bipm.org/en/publications/si-brochure) specifies
273.15. The catalog preserves the source value (exactly `6829/25`); it does not
silently repair it. Selecting `iso80000-corrections.yml` applies the attributed
273.15 offset to derived conversions while retaining the original constant.
Celsius remains excluded from multiplicative scalar output.

## Reviewed source corrections

`iso80000-corrections.yml` is data, explicitly selected with `--corrections`.
It is pinned to the source SHA-256 and each edit includes a target XMI ID,
model field, expected old value, replacement, reason and citation. Every edit
is validated before any is applied. Unknown targets, duplicate target/field
pairs, stale values and a different source hash are errors. Supported fields
are kind/unit `factors`, unit `kinds`, affine `offset`, and unit `name`/`symbol`.
Numbers in the manifest should be quoted exact expressions, not YAML floats.

The reviewed manifest corrects:

- Celsius offset, following the BIPM SI Brochure.
- Weber per metre's length exponent, following the source name and Wb/m symbol.
- Missing reciprocal-kelvin, reciprocal-pascal and square-metre-per-second
  quantity relationships, explicitly enumerated rather than inferred by dimension.
- Kinematic viscosity's density exponent and the corresponding derived unit.
  The source multiplies viscosity by density; it should divide. The affected
  unit and prefixes also receive corrected names and symbols. See
  [NIST's viscosity definition](https://www.nist.gov/programs-projects/gas-properties-flow-metering)
  and [SI units for viscosity](https://www.nist.gov/pml/special-publication-811/nist-guide-si-chapter-8).

Original names, relationships, factor declarations and constants stay in
`declarations`; consumers of corrected data use `resolved` and the correction
manifest together. The exporter contains no name-specific correction code.

## What the library does not contain

Reported, never guessed:

- Generalized coordinate has no dimension definition. Six other generalized
  kinds do have factors, but depend on that undefined coordinate. Their factor
  expressions and unresolved dependency IDs are emitted instead of assigning
  arbitrary dimensions or units.
- No prefixed seconds (there is no millisecond), and no non-SI units beyond the
  few ISO 80000 accepts (minute, hour, day, tonne, degree, gon, byte, bel).
- No operation rules. A derived kind's factor product is the only statement of
  how kinds combine, and many kinds share dimensions.

## Tests

```bash
uv run pytest
```

The constant, factor, symbol and synthetic XMI catalog tests always run. The latter
cover affine composition and interval semantics, deep chains, cycles, missing
references, declaration ordering, correction preconditions, source preservation,
unresolved kinds, exact decimal and pi values, unsupported expressions, duplicate
identities, CLI strict behavior and repeatability. The library-backed tests run
when `ISO-80000.xmi` is at the repository root or `ISO80000_XMI` points at it, and
are skipped otherwise.

## Licence

MIT for the converter. The OMG model library is OMG's; this repository does not
redistribute it or output generated from it.
