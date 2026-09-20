# qudv-iso80000

`iso80000_converter.py` reads the OMG SysML QUDV model library for ISO/IEC 80000
and writes its quantity kinds, dimensions, units, exact conversion factors and
unit symbols as YAML: one `scalars` list of unit families, shown below.

The source is the machine-readable OMG library, not the ISO documents. It is not
checked in (92 MB). Download it once:

```bash
curl -L -o ISO-80000.xmi http://www.omg.org/spec/SysML/20150709/ISO-80000.xmi
```

## Usage

```bash
uv run python iso80000_converter.py ISO-80000.xmi -o iso80000.yml
uv run python iso80000_converter.py ISO-80000.xmi --strict   # exit 1 if anything was not emitted
```

Without `-o` the YAML goes to stdout. Progress, corrections and everything that
was not emitted go to stderr, and are repeated in the header comment of the YAML.

## What it emits

Against the 2015-07-09 library: **317 of 325 quantity kinds**, 7511 unit entries.
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

## What the library does not contain

Reported, never guessed:

- Seven `generalized *` kinds and `number of turns in a winding` have no factors,
  no general kind and no derived unit, so no dimensions.
- `weber per metre` is modelled as weber x metre; it disagrees with magnetic vector
  potential and is left out of that family.
- `kelvin to the power minus one`, `pascal to the power minus one`,
  `square metre per second` and the prefixed `degree celsius` units name no kind.
- No affine units. `CelsiusTemperature` is emitted with kelvin-scaled units and no
  offset; the 273.15 has to be authored by the consumer.
- No prefixed seconds (there is no millisecond), and no non-SI units beyond the
  few ISO 80000 accepts (minute, hour, day, tonne, degree, gon, byte, bel).
- No operation rules. A derived kind's factor product is the only statement of
  how kinds combine, and many kinds share dimensions.

## Tests

```bash
uv run pytest
```

The constant, factor and symbol tests always run. The library-backed tests run
when `ISO-80000.xmi` is at the repository root or `ISO80000_XMI` points at it, and
are skipped otherwise.

## Licence

MIT for the converter. The OMG model library is OMG's; this repository does not
redistribute it or output generated from it.
