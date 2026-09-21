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
uv run python iso80000_converter.py ISO-80000.xmi --strict   # exit 1 if anything was not emitted
```

Without `-o` the YAML goes to stdout. Progress, corrections and everything that
was not resolved into scalar families go to stderr. Diagnostics are repeated in
the scalar YAML header or in structured catalog fields. `--strict` exits 1 for
these resolution gaps in either format, after writing the output; a catalog
can preserve declarations even when its derived analysis is incomplete.

## Source-preserving catalog (schema version 1)

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
- `resolved`: separately derived kind dimensions, multiplicative SI unit
  factors, and numbers. Exact numbers use `rational` and `pi_exponent`;
  approximate numbers use only `approximate`. Missing dimensions/factors are
  `null`, never a fabricated dimension-one value or identity conversion.
- `diagnostics`: structured `problems` and `corrections`. Corrections made by
  the existing inference rules affect the derived view, never source slots.

Affine and general conversions retain their factor/offset/reference or
expression/language slots. They have no multiplicative `si_factor`, including
when reached through a prefix or reference chain. A consumer must interpret
the declared conversion semantics; this exporter does not evaluate nonlinear
conversions. Unsupported numeric expressions remain in declarations and are
reported. If one prevents derived unit analysis, that derived view is empty
rather than partially published; independently resolvable numbers remain.

The catalog includes unresolved kinds, fractional dimension exponents, and
exact powers of pi that cannot be represented by the legacy scalar format.
Duplicate XMI IDs or colliding local slot names are rejected, not overwritten.
Output has no timestamps or machine-local paths and is deterministic for the
same source and exporter. The source SHA-256 identifies bytes, not correctness.

## What it emits

Against the 2015-07-09 library: **316 of 325 quantity kinds**, 7490 unit entries
in scalar format; the catalog retains all **325 kinds and 2795 units**.
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

**Dimensions of a kind**, first source that answers: base quantity; the
`isQuantityOfDimensionOne` flag; the kind's own `factor` products; its `general`
kind; a `DerivedUnit` that measures it.

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
silently repair it. An attributed correction is needed before using this
definition for physical conversion. No Celsius-specific correction is embedded
in the exporter. The prior exporter skipped the affine unit and incorrectly
borrowed kelvin-scaled units for `CelsiusTemperature`; that family is now
excluded from scalar output. Regression tests preserve this distinction.

## What the library does not contain

Reported, never guessed:

- Seven `generalized *` kinds and `number of turns in a winding` have no factors,
  no general kind and no derived unit, so no dimensions.
- `weber per metre` is modelled as weber x metre; it disagrees with magnetic vector
  potential and is left out of that family.
- `kelvin to the power minus one`, `pascal to the power minus one`,
  and `square metre per second` name no kind (nor do their prefixes).
- No prefixed seconds (there is no millisecond), and no non-SI units beyond the
  few ISO 80000 accepts (minute, hour, day, tonne, degree, gon, byte, bel).
- No operation rules. A derived kind's factor product is the only statement of
  how kinds combine, and many kinds share dimensions.

## Tests

```bash
uv run pytest
```

The constant, factor, symbol and synthetic XMI catalog tests always run. The latter
cover affine/general definitions, prefix reference chains, source preservation,
unresolved kinds, exact decimal and pi values, unsupported expressions, duplicate
identities, CLI strict behavior and repeatability. The library-backed tests run
when `ISO-80000.xmi` is at the repository root or `ISO80000_XMI` points at it, and
are skipped otherwise.

## Licence

MIT for the converter. The OMG model library is OMG's; this repository does not
redistribute it or output generated from it.
