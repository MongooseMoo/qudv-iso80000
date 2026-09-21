#!/usr/bin/env python3
"""
ISO-80000 XMI to YAML converter.

Reads the OMG SysML QUDV model library
(http://www.omg.org/spec/SysML/20150709/ISO-80000.xmi). ``--format catalog``
preserves source declarations and separates derived information and diagnostics.
The default emits a ``scalars`` list of unit families. Every family has resolved dimensions, a canonical unit
whose factor is exactly 1, and unit factors relative to that canonical.
Quantity kinds the library does not define well enough are never emitted with
empty placeholders; they are listed, with the reason, in the header comment and
on stderr.

Usage:
    python iso80000_converter.py [ISO-80000.xmi] [-o iso80000.yml] [--strict]
"""

import argparse
import ast
import hashlib
import html
import math
import re
import sys
from collections import defaultdict
from fractions import Fraction

import yaml
from lxml import etree

XMI_NS = 'http://www.omg.org/spec/XMI/20131001'
XMI_ID = f'{{{XMI_NS}}}id'
XMI_IDREF = f'{{{XMI_NS}}}idref'
XMI_TYPE = f'{{{XMI_NS}}}type'

DIM_MAP = {
    'length': 'L',
    'mass': 'M',
    'time': 'T',
    'electric current': 'I',
    'thermodynamic temperature': 'Θ',
    'amount of substance': 'N',
    'luminous intensity': 'J',
}

KIND_CLASSES = {'SimpleQuantityKind', 'DerivedQuantityKind'}
UNIT_CLASSES = {'SimpleUnit', 'DerivedUnit', 'PrefixedUnit', 'LinearConversionUnit',
                'AffineConversionUnit', 'GeneralConversionUnit'}


class ConversionError(Exception):
    """The XMI says something this converter cannot represent."""


def to_pascal_case(s):
    """Convert a QUDV name to a PascalCase identifier."""
    words = re.sub(r'[^0-9A-Za-z]+', ' ', s).split()
    return ''.join(w[0].upper() + w[1:] for w in words)


def symbol_text(markup):
    """Plain text of a unit symbol the library stores as text or as HTML fragments."""
    if '<' not in markup:
        return markup.strip()
    text = re.sub(r'<style.*?</style>', '', markup, flags=re.S | re.I)
    text = re.sub(r'<sup>\s*(.*?)\s*</sup>', r'^\1', text, flags=re.S | re.I)
    text = re.sub(r'<sub>\s*(.*?)\s*</sub>', r'_\1', text, flags=re.S | re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\s+', '', html.unescape(text))


class Factor:
    """A conversion factor: an exact rational times pi**pi_exp, or a float.

    QUDV states factors as integers, rationals and a few reals (Pi/180,
    ln(10)). Rationals and powers of pi stay exact and render as
    "n/d" and "n*pi/d"; anything else degrades to float once, here.
    """

    __slots__ = ('rational', 'pi_exp', 'inexact')

    def __init__(self, rational=Fraction(1), pi_exp=0, inexact=None):
        self.rational = rational
        self.pi_exp = pi_exp
        self.inexact = inexact  # float, set only when exactness was lost

    @classmethod
    def from_float(cls, value):
        return cls(inexact=float(value))

    def as_float(self):
        if self.inexact is not None:
            return self.inexact
        return float(self.rational) * math.pi ** self.pi_exp

    def __mul__(self, other):
        if self.inexact is not None or other.inexact is not None:
            return Factor.from_float(self.as_float() * other.as_float())
        return Factor(self.rational * other.rational, self.pi_exp + other.pi_exp)

    def __pow__(self, exponent):
        exponent = Fraction(exponent)
        if self.inexact is None and exponent.denominator == 1:
            n = exponent.numerator
            return Factor(self.rational ** n, self.pi_exp * n)
        if self.inexact is None and self.rational == 1 and self.pi_exp == 0:
            return Factor()
        return Factor.from_float(self.as_float() ** float(exponent))

    def is_one(self):
        return self.inexact is None and self.rational == 1 and self.pi_exp == 0

    def to_yaml(self):
        """Render as an int, 'n/d', 'pi', 'n*pi', 'pi/d', 'n*pi/d', or a float."""
        if self.inexact is not None:
            return self.inexact
        r = self.rational
        if self.pi_exp == 0:
            return r.numerator if r.denominator == 1 else f'{r.numerator}/{r.denominator}'
        if self.pi_exp == 1:
            head = 'pi' if r.numerator == 1 else f'{r.numerator}*pi'
            return head if r.denominator == 1 else f'{head}/{r.denominator}'
        return self.as_float()

    def exact_record(self):
        """Catalog representation; arbitrary powers of pi remain symbolic."""
        if self.inexact is not None:
            return {'approximate': self.inexact}
        return {'rational': str(self.rational), 'pi_exponent': self.pi_exp}


def evaluate_constant(expr):
    """Evaluate a QUDV literal body such as '10^3', '(2^10)^2', 'Pi/180', 'ln(10)'."""
    source = expr.replace('^', '**')
    tree = ast.parse(source, mode='eval')

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            return Factor(Fraction(node.value))
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            return Factor(Fraction(ast.get_source_segment(source, node)))
        if isinstance(node, ast.Name) and node.id == 'Pi':
            return Factor(pi_exp=1)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return Factor(Fraction(-1)) * walk(node.operand)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            return walk(node.left) * walk(node.right)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return walk(node.left) * walk(node.right) ** -1
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            exponent = walk(node.right)
            if exponent.inexact is not None or exponent.pi_exp != 0:
                raise ConversionError(f'non-rational exponent in constant {expr!r}')
            return walk(node.left) ** exponent.rational
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'ln' and len(node.args) == 1 and not node.keywords):
            return Factor.from_float(math.log(walk(node.args[0]).as_float()))
        raise ConversionError(f'unsupported constant expression {expr!r}')

    return walk(tree)


class ISO80000Converter:
    def __init__(self, xmi_file):
        print(f'Loading {xmi_file}...', file=sys.stderr)
        with open(xmi_file, 'rb') as handle:
            source = handle.read()
        self.source_hash = hashlib.sha256(source).hexdigest()
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        root = etree.fromstring(source, parser=parser)
        self.by_id = {}
        self.instances = []
        for elem in root.iter():
            elem_id = elem.get(XMI_ID)
            if elem_id:
                if elem_id in self.by_id:
                    raise ConversionError(f'duplicate XMI id {elem_id!r}')
                self.by_id[elem_id] = elem
            if elem.get(XMI_TYPE) == 'uml:InstanceSpecification':
                self.instances.append(elem)

        self.kinds = {}       # id -> kind record
        self.units = {}       # id -> unit record
        self.base_units = set()
        self.problems = []    # (subject name, reason) for everything not emitted
        self.corrections = [] # (subject name, what) where the library contradicts itself
        self._const_cache = {}
        self._kind_dims = {}
        self._unit_dims = {}
        self._unit_si = {}
        self._scalars = None

    # --- XMI access -------------------------------------------------------

    @staticmethod
    def class_of(elem):
        classifier = elem.find('./classifier')
        if classifier is None:
            return ''
        return classifier.get('href', '').rsplit('_', 1)[-1]

    @staticmethod
    def name_of(elem):
        name = elem.find('name')
        return name.text if name is not None else elem.get('name')

    def slots(self, elem):
        """feature name -> list of referenced elements, or literal value elements."""
        result = defaultdict(list)
        for slot in elem.findall('./slot'):
            feature = slot.find('./definingFeature')
            if feature is None:
                continue
            feature_name = feature.get('href', '').rsplit('.', 1)[-1]
            for value in slot.findall('./value'):
                idref = value.get(XMI_IDREF)
                if idref is None:
                    instance = value.find('./instance')
                    if instance is not None:
                        idref = instance.get(XMI_IDREF)
                result[feature_name].append(self.by_id[idref] if idref in self.by_id else value)
        return result

    @staticmethod
    def literal(value_elem):
        text = value_elem.get('value')
        if text is None:
            inner = value_elem.find('./value')
            text = inner.text if inner is not None else value_elem.text
        return (text or '').strip()

    def constant(self, elem):
        elem_id = elem.get(XMI_ID)
        if elem_id in self._const_cache:
            return self._const_cache[elem_id]
        cls = self.class_of(elem)
        if cls == 'Rational':
            s = self.slots(elem)
            result = self.constant(s['numerator'][0]) * self.constant(s['denominator'][0]) ** -1
        else:
            body = elem.find('./specification/body')
            match = re.search(r'\((.*)\)\s*$', body.text if body is not None and body.text else '')
            if not match:
                raise ConversionError(f'constant {self.name_of(elem)!r} has no literal body')
            result = evaluate_constant(match.group(1))
        self._const_cache[elem_id] = result
        return result

    def exponent(self, elem):
        value = self.constant(elem)
        if value.inexact is not None or value.pi_exp != 0:
            raise ConversionError(f'exponent {self.name_of(elem)!r} is not rational')
        return value.rational

    # --- model ------------------------------------------------------------

    def load_model(self):
        """Read kinds, units and the link instances that relate them."""
        for elem in self.instances:
            cls = self.class_of(elem)
            if cls in KIND_CLASSES:
                s = self.slots(elem)
                flag = s.get('isQuantityOfDimensionOne')
                self.kinds[elem.get(XMI_ID)] = {
                    'name': self.name_of(elem),
                    'factors': [self._factor_pair(f, 'quantityKind') for f in s.get('factor', [])],
                    'general': [g.get(XMI_ID) for g in s.get('general', [])],
                    'dimension_one': bool(flag) and self.literal(flag[0]).lower() == 'true',
                    'units': [],
                }
            elif cls in UNIT_CLASSES:
                s = self.slots(elem)
                self.units[elem.get(XMI_ID)] = {
                    'name': self.name_of(elem),
                    'symbol': symbol_text(self.literal(s['symbol'][0])) if s.get('symbol') else '',
                    'class': cls,
                    'kinds': [k.get(XMI_ID) for k in s.get('quantityKind', [])],
                    'factors': [self._factor_pair(f, 'unit') for f in s.get('factor', [])]
                               if cls == 'DerivedUnit' else [],
                    'scale': self.constant(s['factor'][0]) if cls == 'LinearConversionUnit' else None,
                    'prefix': self.constant(self.slots(s['prefix'][0])['factor'][0])
                              if cls == 'PrefixedUnit' else None,
                    'reference': s['referenceUnit'][0].get(XMI_ID) if s.get('referenceUnit') else None,
                    'general': [g.get(XMI_ID) for g in s.get('general', [])],
                }

        for elem in self.instances:
            cls = self.class_of(elem)
            if cls == 'measurementUnit':  # A_quantityKind_measurementUnit link
                s = self.slots(elem)
                for unit in s.get('measurementUnit', []):
                    for kind in s.get('quantityKind', []):
                        record = self.units.get(unit.get(XMI_ID))
                        if record is not None and kind.get(XMI_ID) not in record['kinds']:
                            record['kinds'].append(kind.get(XMI_ID))
            elif cls == 'baseUnit':  # A_systemOfUnits_baseUnit link
                for unit in self.slots(elem).get('baseUnit', []):
                    self.base_units.add(unit.get(XMI_ID))

        print(f'  {len(self.kinds)} quantity kinds, {len(self.units)} units', file=sys.stderr)

    def _factor_pair(self, factor_elem, target_feature):
        """(target id, exponent) of a QuantityKindFactor or UnitFactor.

        The library names every factor 'target^exponent'. Where that name and
        the exponent slot disagree the library contradicts itself; the name is
        used, because the units then agree with the kinds they measure, and the
        disagreement is reported as a correction.
        """
        s = self.slots(factor_elem)
        exponent = self.exponent(s['exponent'][0])
        name = self.name_of(factor_elem) or ''
        named = re.search(r'\^(-?\d+(?:/\d+)?)$', name)
        if named and Fraction(named.group(1)) != exponent:
            self.corrections.append(
                (name, f'exponent slot says {exponent}, name says {named.group(1)}; used the name'))
            exponent = Fraction(named.group(1))
        return s[target_feature][0].get(XMI_ID), exponent

    # --- dimensions -------------------------------------------------------

    def kind_dims(self, kind_id, _active=None):
        """Dimensions of a quantity kind, or None when the library does not determine them.

        Sources, in order: base quantity; the dimension-one flag; the kind's
        own factors; its general kind; a derived unit that measures it. The
        flag is the library's direct statement, so it wins over factors; where
        the two disagree that is reported as a correction.
        """
        if kind_id in self._kind_dims:
            return self._kind_dims[kind_id]
        active = _active or set()
        if kind_id in active:
            return None
        active = active | {kind_id}
        kind = self.kinds[kind_id]

        dims = None
        if kind['name'] in DIM_MAP:
            dims = {DIM_MAP[kind['name']]: Fraction(1)}
        elif kind['factors']:
            dims = self._sum_dims(
                (self.kind_dims(k, active), e) for k, e in kind['factors'])
        if kind['dimension_one']:
            if dims:
                self.corrections.append(
                    (kind['name'], f'flagged dimension one but its factors give {_show(dims)}; used the flag'))
            dims = {}
        if dims is None:
            for general_id in kind['general']:
                dims = self.kind_dims(general_id, active)
                if dims is not None:
                    break
        if dims is None:
            for unit_id in kind['units']:
                if self.units[unit_id]['class'] == 'DerivedUnit':
                    dims = self.unit_dims(unit_id, active)
                    if dims is not None:
                        break

        if _active is None or dims is not None:
            self._kind_dims[kind_id] = dims
        return dims

    def unit_dims(self, unit_id, _active=None):
        if unit_id in self._unit_dims:
            return self._unit_dims[unit_id]
        active = _active or set()
        if unit_id in active:
            return None
        active = active | {unit_id}
        unit = self.units[unit_id]

        if unit['class'] == 'DerivedUnit':
            dims = self._sum_dims((self.unit_dims(u, active), e) for u, e in unit['factors'])
        elif unit['reference'] is not None:
            dims = self.unit_dims(unit['reference'], active)
        else:
            dims = None
            for kind_id in unit['kinds']:
                dims = self.kind_dims(kind_id, active)
                if dims is not None:
                    break
            if dims is None:
                for general_id in unit['general']:
                    if general_id in self.units:
                        dims = self.unit_dims(general_id, active)
                        if dims is not None:
                            break

        if _active is None or dims is not None:
            self._unit_dims[unit_id] = dims
        return dims

    @staticmethod
    def _sum_dims(parts):
        total = defaultdict(Fraction)
        for dims, exponent in parts:
            if dims is None:
                return None
            for symbol, power in dims.items():
                total[symbol] += power * exponent
        return {symbol: power for symbol, power in total.items() if power != 0}

    # --- factors ----------------------------------------------------------

    def seed_base_units(self):
        """SI base units have factor 1; so a base unit's reference is its inverse.

        The library makes gram the simple unit of mass and kilogram a prefixed
        unit of it, so gram must come out as 1/1000, not 1.
        """
        for unit_id in self.base_units:
            factor = Factor()
            while unit_id is not None:
                self._unit_si[unit_id] = factor
                unit = self.units[unit_id]
                own = unit['prefix'] if unit['class'] == 'PrefixedUnit' else unit['scale']
                if unit['reference'] is None or own is None:
                    break
                factor = factor * own ** -1
                unit_id = unit['reference']

    def unit_si_factor(self, unit_id, _active=frozenset()):
        """Factor relative to the coherent SI unit of the same dimensions."""
        if unit_id in self._unit_si:
            return self._unit_si[unit_id]
        if unit_id in _active:
            return None
        active = _active | {unit_id}
        unit = self.units[unit_id]

        result = None
        if unit['class'] in {'AffineConversionUnit', 'GeneralConversionUnit'}:
            # These definitions are preserved in the catalog, never flattened
            # to a multiplicative factor (including through reference chains).
            result = None
        elif unit['class'] == 'DerivedUnit':
            if unit['factors']:
                result = Factor()
                for factor_unit, exponent in unit['factors']:
                    part = self.unit_si_factor(factor_unit, active)
                    if part is None:
                        result = None
                        break
                    result = result * part ** exponent
        elif unit['class'] == 'SimpleUnit':
            # A simple unit that is another unit under a special name (watt is
            # joule per second) takes that unit's factor. Every other simple
            # unit in the library is the coherent unit of its kind.
            generals = [g for g in unit['general'] if g in self.units]
            result = self.unit_si_factor(generals[0], active) if generals else Factor()
        elif unit['reference'] is not None:
            reference = self.unit_si_factor(unit['reference'], active)
            own = unit['prefix'] if unit['class'] == 'PrefixedUnit' else unit['scale']
            result = None if reference is None else own * reference

        self._unit_si[unit_id] = result
        return result

    # --- assembly ---------------------------------------------------------

    def assign_units(self):
        """Attach each unit to the kinds it measures.

        A unit that names no kind takes the kinds of its reference unit, then
        of its general unit. A kind with no unit of its own takes the units of
        its nearest general kind that has some.
        """
        def kinds_of(unit_id, seen=frozenset()):
            unit = self.units[unit_id]
            if unit['kinds'] or unit_id in seen:
                return unit['kinds']
            for parent in ([unit['reference']] if unit['reference'] else []) + unit['general']:
                if parent in self.units:
                    found = kinds_of(parent, seen | {unit_id})
                    if found:
                        return found
            return []

        for unit_id, unit in self.units.items():
            kinds = [k for k in kinds_of(unit_id) if k in self.kinds]
            if not kinds:
                self.problems.append((unit['name'], 'unit: measures no quantity kind in the library'))
            for kind_id in kinds:
                self.kinds[kind_id]['units'].append(unit_id)

        def inherited(kind_id, seen=frozenset()):
            kind = self.kinds[kind_id]
            if kind['units'] or kind_id in seen:
                return kind['units']
            for general_id in kind['general']:
                if general_id in self.kinds:
                    found = inherited(general_id, seen | {kind_id})
                    if found:
                        return found
            return []

        borrowed = {k: list(inherited(k)) for k, kind in self.kinds.items() if not kind['units']}
        for kind_id, units in borrowed.items():
            self.kinds[kind_id]['units'] = units

    def build_scalars(self):
        self.seed_base_units()
        scalars = []
        for kind_id, kind in self.kinds.items():
            name = kind['name']
            dims = self.kind_dims(kind_id)
            if dims is None:
                self.problems.append((name, 'kind: no factors, general kind or derived unit gives its dimensions'))
                continue
            if any(power.denominator != 1 for power in dims.values()):
                self.problems.append((name, f'kind: non-integer dimension exponents {dims}'))
                continue

            members = {}
            for unit_id in kind['units']:
                unit = self.units[unit_id]
                unit_dims = self.unit_dims(unit_id)
                factor = self.unit_si_factor(unit_id)
                if factor is None:
                    self.problems.append((unit['name'], f'unit of {name!r}: non-multiplicative conversion or unresolved factor'))
                elif unit_dims != dims:
                    self.problems.append((unit['name'], f'unit of {name!r}: dimensions {_show(unit_dims)} differ from kind {_show(dims)}'))
                else:
                    members[unit_id] = factor
            if not members:
                self.problems.append((name, 'kind: no usable unit'))
                continue

            canonical_id = self._choose_canonical(members)
            if canonical_id is None:
                self.problems.append((name, 'kind: no unit with factor exactly 1 to serve as canonical'))
                continue

            units = {}
            for unit_id, factor in members.items():
                pretty = to_pascal_case(self.units[unit_id]['name'])
                if pretty in units:
                    raise ConversionError(f'unit name collision {pretty!r} in {name!r}')
                units[pretty] = {'factor': factor.to_yaml()}
                if self.units[unit_id]['symbol']:
                    units[pretty]['symbol'] = self.units[unit_id]['symbol']
            scalars.append({
                'name': to_pascal_case(name),
                'dimensions': {symbol: int(power) for symbol, power in dims.items()},
                'canonical': to_pascal_case(self.units[canonical_id]['name']),
                'units': units,
            })

        names = [s['name'] for s in scalars]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ConversionError(f'quantity name collisions after PascalCase: {duplicates}')
        scalars.sort(key=lambda s: s['name'])
        return scalars

    def _choose_canonical(self, members):
        """The coherent SI unit: factor exactly 1, preferring an unprefixed unit."""
        rank = {'SimpleUnit': 0, 'DerivedUnit': 0, 'LinearConversionUnit': 1, 'PrefixedUnit': 2}
        candidates = [u for u, factor in members.items() if factor.is_one()]
        if not candidates:
            return None
        return min(candidates, key=lambda u: (
            0 if u in self.base_units else 1,
            rank[self.units[u]['class']],
            len(self.units[u]['name']),
            self.units[u]['name'],
        ))

    def convert(self):
        if self._scalars is not None:
            return self._scalars
        self.load_model()
        self.assign_units()
        scalars = self.build_scalars()
        print(f'  emitted {len(scalars)} of {len(self.kinds)} quantity kinds, '
              f"{sum(len(s['units']) for s in scalars)} unit entries; "
              f'{len(self.problems)} problems', file=sys.stderr)
        self._scalars = scalars
        return scalars

    @staticmethod
    def _source_tree(elem):
        """Preserve opaque specification syntax without evaluating its language."""
        result = {'tag': elem.tag}
        if elem.attrib:
            result['attributes'] = dict(sorted(elem.attrib.items()))
        if elem.text and elem.text.strip():
            result['text'] = elem.text
        if elem.tail and elem.tail.strip():
            result['tail'] = elem.tail
        children = [ISO80000Converter._source_tree(child) for child in elem
                    if isinstance(child.tag, str)]
        if children:
            result['children'] = children
        return result

    @classmethod
    def _source_value(cls, elem):
        reference = elem.get(XMI_IDREF)
        instance = elem.find('./instance')
        if reference is None and instance is not None:
            reference = instance.get(XMI_IDREF)
        if reference is not None:
            return {'ref': reference}
        if elem.get('href') is not None:
            return {'href': elem.get('href')}
        if elem.get(XMI_TYPE, '').startswith('uml:Literal'):
            value = elem.get('value')
            if value is None:
                inner = elem.find('./value')
                value = inner.text if inner is not None else elem.text
            return {'type': elem.get(XMI_TYPE), 'value': value or ''}
        return {'xml': cls._source_tree(elem)}

    def catalog(self):
        """Source declarations plus separately labelled derived information.

        IDs are scoped to the source SHA-256. Source slots retain their full
        feature URIs; local feature names are conveniences, not global identity.
        Unresolved declarations and unknown expression languages remain data.
        """
        declarations = {}
        for elem in self.instances:
            identifier = elem.get(XMI_ID)
            if not identifier:
                raise ConversionError('instance specification has no XMI id')
            record = {'class': self.class_of(elem), 'name': self.name_of(elem),
                      'classifiers': [dict(c.attrib) for c in elem.findall('./classifier')],
                      'slots': {}, 'features': {}}
            for slot in elem.findall('./slot'):
                feature = slot.find('./definingFeature')
                if feature is None or not feature.get('href'):
                    raise ConversionError(f'{identifier}: slot has no defining feature URI')
                uri = feature.get('href')
                name = uri.rsplit('.', 1)[-1]
                if name in record['slots']:
                    raise ConversionError(f'{identifier}: duplicate slot name {name!r}')
                record['features'][name] = uri
                record['slots'][name] = [self._source_value(v) for v in slot.findall('./value')]
            specification = elem.find('./specification')
            if specification is not None:
                record['specification'] = self._source_tree(specification)
            declarations[identifier] = record

        def dimensions(dims):
            return None if dims is None else {
                key: value.numerator if value.denominator == 1 else str(value)
                for key, value in sorted(dims.items())}

        kinds = {}
        units = {}
        # Catalog extraction must survive an expression the scalar projection
        # cannot interpret. Never publish a partially completed derived view.
        try:
            self.convert()
            kinds = {identifier: {'dimensions': dimensions(self.kind_dims(identifier))}
                     for identifier in sorted(self.kinds)}
            for identifier in sorted(self.units):
                factor = self.unit_si_factor(identifier)
                units[identifier] = {
                    'dimensions': dimensions(self.unit_dims(identifier)),
                    'si_factor': None if factor is None else factor.exact_record(),
                }
        except (ConversionError, ValueError, ArithmeticError, SyntaxError) as error:
            kinds, units = {}, {}
            self.problems.append(('derived analysis', str(error)))
        numbers = {}
        for identifier, record in sorted(declarations.items()):
            if record['class'] in {'Integer', 'Real', 'Rational'}:
                try:
                    numbers[identifier] = self.constant(self.by_id[identifier]).exact_record()
                except (ConversionError, ValueError, ArithmeticError, SyntaxError) as error:
                    self.problems.append((record['name'] or identifier, f'number: {error}'))
        return {
            'schema_version': 1,
            'source': {'sha256': self.source_hash, 'format': 'OMG-QUDV-XMI'},
            'declarations': dict(sorted(declarations.items())),
            'resolved': {'kinds': kinds, 'units': units, 'numbers': numbers},
            'diagnostics': {
                'problems': [{'subject': subject, 'reason': reason}
                             for subject, reason in sorted(set(self.problems))],
                'corrections': [{'subject': subject, 'reason': reason}
                                for subject, reason in sorted(set(self.corrections))],
            },
        }


def _show(dims):
    if dims is None:
        return 'unresolved'
    return '{' + ', '.join(f'{k}: {v}' for k, v in dims.items()) + '}'


def render(scalars, problems, corrections):
    lines = [
        '# Generated from ISO-80000 XMI by iso80000_converter.py',
        f'# {len(scalars)} quantity kinds emitted; {len(problems)} entries not emitted:',
    ]
    lines += [f'#   {subject}: {reason}' for subject, reason in sorted(problems)]
    lines.append(f'# {len(corrections)} contradictions in the library, resolved as stated:')
    lines += [f'#   {subject}: {what}' for subject, what in sorted(set(corrections))]
    body = yaml.dump({'scalars': scalars}, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return '\n'.join(lines) + '\n\n' + body


def main():
    parser = argparse.ArgumentParser(description='ISO-80000 XMI to YAML converter.')
    parser.add_argument('xmi', nargs='?', default='ISO-80000.xmi')
    parser.add_argument('-o', '--output', help='write YAML here instead of stdout')
    parser.add_argument('--format', choices=('scalars', 'catalog'), default='scalars',
                        help='legacy unit families or source-preserving catalog (schema 1)')
    parser.add_argument('--strict', action='store_true',
                        help='exit 1 on resolution gaps, even if preserved in the catalog')
    args = parser.parse_args()

    converter = ISO80000Converter(args.xmi)
    if args.format == 'catalog':
        text = yaml.safe_dump(converter.catalog(), sort_keys=False, allow_unicode=True)
    else:
        scalars = converter.convert()
        text = render(scalars, converter.problems, converter.corrections)
    if args.output:
        with open(args.output, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
    else:
        sys.stdout.buffer.write(text.encode('utf-8'))
    for subject, what in sorted(set(converter.corrections)):
        print(f'  corrected: {subject}: {what}', file=sys.stderr)
    for subject, reason in sorted(converter.problems):
        label = 'unresolved in derived view' if args.format == 'catalog' else 'not emitted'
        print(f'  {label}: {subject}: {reason}', file=sys.stderr)
    return 1 if args.strict and converter.problems else 0


if __name__ == '__main__':
    sys.exit(main())
