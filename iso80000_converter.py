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

from dependency_resolution import infer_alternatives, resolve_dependencies, unresolved_cycles

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


class CorrectionError(ConversionError):
    """An explicitly requested correction cannot be applied safely."""


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


class NumberSum:
    """An affine offset: a finite exact sum of rational powers of pi.

    Mixed powers stay symbolic; only already-approximate inputs become floats.
    """

    def __init__(self, *terms):
        self.terms = defaultdict(Fraction)
        self.approximate = None
        for term in terms:
            if term.inexact is not None:
                self.approximate = (self.approximate or 0.0) + term.inexact
            else:
                self.terms[term.pi_exp] += term.rational

    def factors(self):
        return [Factor(value, power) for power, value in sorted(self.terms.items()) if value]

    def as_float(self):
        return sum(f.as_float() for f in self.factors()) + (self.approximate or 0.0)

    def __add__(self, other):
        if self.approximate is not None or other.approximate is not None:
            return NumberSum(Factor.from_float(self.as_float() + other.as_float()))
        return NumberSum(*self.factors(), *other.factors())

    def __mul__(self, scale):
        if self.approximate is not None or scale.inexact is not None:
            return NumberSum(Factor.from_float(self.as_float() * scale.as_float()))
        return NumberSum(*(term * scale for term in self.factors()))

    def exact_record(self):
        if self.approximate is not None:
            return {'approximate': self.as_float()}
        terms = self.factors()
        if not terms:
            return Factor(Fraction(0)).exact_record()
        if len(terms) == 1:
            return terms[0].exact_record()
        return {'sum': [term.exact_record() for term in terms]}


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
            literal = ast.get_source_segment(source, node)
            if literal is None:
                raise ConversionError(f'missing source text for numeric literal in {expr!r}')
            return Factor(Fraction(literal))
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
    def __init__(self, xmi_file, corrections_file=None):
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
        self._unit_conversions = {}
        self._scalars = None
        self._diagnostics = []
        self.applied_corrections = None
        self.correction_manifest = None
        if corrections_file is not None:
            with open(corrections_file, 'rb') as handle:
                correction_bytes = handle.read()
            try:
                manifest = yaml.safe_load(correction_bytes)
            except yaml.YAMLError as error:
                raise CorrectionError(f'invalid corrections YAML: {error}') from error
            if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1
                    or manifest.get('source_sha256') != self.source_hash
                    or not isinstance(manifest.get('changes'), list)):
                raise CorrectionError('corrections require schema 1, matching source_sha256 and changes')
            self.correction_manifest = manifest
            self.correction_hash = hashlib.sha256(correction_bytes).hexdigest()

    def problem(self, identifier, subject, category, reason):
        item = {'id': identifier, 'subject': subject, 'category': category, 'reason': reason}
        if item not in self._diagnostics:
            self._diagnostics.append(item)
            self.problems.append((subject, reason))

    def apply_corrections(self):
        """Validate every edit before changing the derived model, never the XML."""
        if self.correction_manifest is None:
            return
        pending = []
        seen = set()
        for change in self.correction_manifest['changes']:
            if (not isinstance(change, dict)
                    or set(change) != {'target', 'field', 'expected', 'value', 'reason', 'citation'}
                    or not all(isinstance(change[k], str) and change[k].strip()
                               for k in ('target', 'field', 'reason', 'citation'))):
                raise CorrectionError('correction requires target, field, expected, value, reason and citation')
            target, field = change['target'], change['field']
            record = self.units.get(target, self.kinds.get(target))
            if record is None or (target, field) in seen:
                raise CorrectionError(f'correction has missing or duplicate target/field: {target}.{field}')
            seen.add((target, field))
            if field == 'factors':
                if target in self.units and record['class'] != 'DerivedUnit':
                    raise CorrectionError(f'correction factors require a derived unit: {target}')
                current = [[ref, str(power)] for ref, power in record[field]]
                targets = self.units if target in self.units else self.kinds
                try:
                    value = [(ref, Fraction(power)) for ref, power in change['value']]
                    if not value or any(ref not in targets for ref, _ in value):
                        raise ValueError('factor target is missing')
                except (TypeError, ValueError, ZeroDivisionError) as error:
                    raise CorrectionError(f'invalid correction factors: {target}') from error
            elif field == 'kinds' and target in self.units:
                current = record[field]
                value = change['value']
                if (not isinstance(value, list) or not all(isinstance(k, str) and k in self.kinds for k in value)
                        or len(value) != len(set(value))):
                    raise CorrectionError(f'invalid correction quantity kinds: {target}')
            elif field == 'offset' and record.get('class') == 'AffineConversionUnit':
                current = record[field].to_yaml()
                try:
                    value = evaluate_constant(str(change['value']))
                except (ConversionError, ValueError, ArithmeticError, SyntaxError) as error:
                    raise CorrectionError(f'invalid correction offset: {target}') from error
            elif field in {'name', 'symbol'} and target in self.units:
                current = record[field]
                value = change['value']
                if not isinstance(value, str) or not value.strip():
                    raise CorrectionError(f'invalid correction {field}: {target}')
            else:
                raise CorrectionError(f'unsupported correction field: {target}.{field}')
            if current != change['expected']:
                raise CorrectionError(f'correction precondition failed: {target}.{field}')
            pending.append((record, field, value, change))
        for record, field, value, change in pending:
            record[field] = value
            self.corrections.append((record['name'],
                                     f"{change['reason']} [{change['citation']}]; "
                                     f"{field}: {change['expected']} -> {change['value']}"))
        self.applied_corrections = {'sha256': self.correction_hash, **self.correction_manifest}

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

    @staticmethod
    def reference_id(elem):
        instance = elem.find('./instance')
        return (elem.get(XMI_ID) or elem.get(XMI_IDREF)
                or (instance.get(XMI_IDREF) if instance is not None else None))

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
                count_flag = s.get('isNumberOfEntities')
                self.kinds[elem.get(XMI_ID)] = {
                    'name': self.name_of(elem),
                    'factors': [self._factor_pair(f, 'quantityKind') for f in s.get('factor', [])],
                    'general': [self.reference_id(g) for g in s.get('general', [])],
                    'dimension_one': bool(flag) and self.literal(flag[0]).lower() == 'true',
                    'entity_count': bool(count_flag) and self.literal(count_flag[0]).lower() == 'true',
                    'units': [],
                }
            elif cls in UNIT_CLASSES:
                s = self.slots(elem)
                self.units[elem.get(XMI_ID)] = {
                    'name': self.name_of(elem),
                    'symbol': symbol_text(self.literal(s['symbol'][0])) if s.get('symbol') else '',
                    'class': cls,
                    'kinds': [self.reference_id(k) for k in s.get('quantityKind', [])],
                    'factors': [self._factor_pair(f, 'unit') for f in s.get('factor', [])]
                               if cls == 'DerivedUnit' else [],
                    'scale': self.constant(s['factor'][0])
                             if cls in {'LinearConversionUnit', 'AffineConversionUnit'} else None,
                    'offset': self.constant(s['offset'][0]) if cls == 'AffineConversionUnit' else None,
                    'prefix': self.constant(self.slots(s['prefix'][0])['factor'][0])
                              if cls == 'PrefixedUnit' else None,
                    'reference': self.reference_id(s['referenceUnit'][0]) if s.get('referenceUnit') else None,
                    'general': [self.reference_id(g) for g in s.get('general', [])],
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
        return self.reference_id(s[target_feature][0]), exponent

    # --- dimensions -------------------------------------------------------

    def report_dependencies(self, graph, values, stage):
        """Diagnose missing references and remaining cyclic components."""
        records = {**self.kinds, **self.units}
        for identifier, dependencies in graph.items():
            for dependency in dependencies:
                if dependency not in graph:
                    record = records[identifier]
                    self.problem(identifier, record['name'], 'missing_reference',
                                 f'{stage}: missing reference {dependency}')
        for cycle in unresolved_cycles(graph, values):
            names = [records[k]['name'] for k in cycle]
            for identifier in cycle:
                record = records[identifier]
                self.problem(identifier, record['name'], 'dependency_cycle',
                             f'{stage}: dependency cycle involving ' + ', '.join(names))

    def required_values(self, graph, evaluate, stage):
        values = resolve_dependencies(graph, evaluate)
        self.report_dependencies(graph, values, stage)
        return values

    def inferred_values(self, alternatives, evaluate, stage):
        values = infer_alternatives(alternatives, evaluate)
        # This union is only a diagnostic graph, never a scheduling graph.
        graph = {node: {parent for rule in rules for parent in rule}
                 for node, rules in alternatives.items()}
        self.report_dependencies(graph, values, stage)
        return values

    def resolve_dimensions(self):
        # Each option is a product of dependencies; copy is exponent one.
        options, constants = {}, {}
        for identifier, kind in self.kinds.items():
            if kind['dimension_one']:
                constants[identifier] = {}
            elif kind['name'] in DIM_MAP:
                constants[identifier] = {DIM_MAP[kind['name']]: Fraction(1)}
            elif kind['entity_count']:
                constants[identifier] = {}
            alternatives = [kind['factors']] if kind['factors'] else []
            alternatives += [[(k, Fraction(1))] for k in kind['general']]
            alternatives += [[(u, Fraction(1))] for u in kind['units']
                             if self.units[u]['class'] == 'DerivedUnit']
            options[identifier] = alternatives
        for identifier, unit in self.units.items():
            if unit['class'] == 'DerivedUnit':
                alternatives = [unit['factors']] if unit['factors'] else []
            elif unit['reference'] is not None:
                alternatives = [[(unit['reference'], Fraction(1))]]
            else:
                alternatives = [[(k, Fraction(1))] for k in unit['kinds'] + unit['general']]
            options[identifier] = alternatives
        rules = {k: [()] if k in constants else [tuple(d for d, _ in option) for option in opts]
                 for k, opts in options.items()}

        def evaluate(identifier, index, values):
            if identifier in constants:
                return constants[identifier]
            return self._sum_dims((values[k], power) for k, power in options[identifier][index])

        values = self.inferred_values(rules, evaluate, 'dimensions')
        self._kind_dims = {k: values[k] for k in self.kinds}
        self._unit_dims = {u: values[u] for u in self.units}
        for identifier, kind in self.kinds.items():
            factor_dims = self._sum_dims((values.get(k), e) for k, e in kind['factors'])
            if kind['dimension_one'] and factor_dims:
                self.corrections.append((kind['name'],
                    f'flagged dimension one but its factors give {_show(factor_dims)}; used the flag'))
            if kind['entity_count'] and values[identifier]:
                self.corrections.append((kind['name'],
                    f'entity-count flag conflicts with resolved dimensions {_show(values[identifier])}; retained dimensions'))

    def kind_dims(self, kind_id):
        return self._kind_dims.get(kind_id)

    def unit_dims(self, unit_id):
        return self._unit_dims.get(unit_id)

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
            seen = set()
            while unit_id is not None:
                if unit_id in seen:
                    raise ConversionError('cycle in SI base-unit reference chain')
                seen.add(unit_id)
                if self.units[unit_id]['class'] in {'AffineConversionUnit', 'GeneralConversionUnit'}:
                    raise ConversionError('non-multiplicative SI base-unit reference chain')
                self._unit_si[unit_id] = factor
                unit = self.units[unit_id]
                own = unit['prefix'] if unit['class'] == 'PrefixedUnit' else unit['scale']
                if unit['reference'] is None or own is None:
                    break
                factor = factor * own ** -1
                unit_id = unit['reference']

    def _compute_si_factor(self, unit_id, values):
        """Factor relative to the coherent SI unit of the same dimensions."""
        if unit_id in self._unit_si:
            return self._unit_si[unit_id]
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
                    part = values.get(factor_unit)
                    if part is None:
                        result = None
                        break
                    result = result * part ** exponent
        elif unit['class'] == 'SimpleUnit':
            # A simple unit that is another unit under a special name (watt is
            # joule per second) takes that unit's factor. Every other simple
            # unit in the library is the coherent unit of its kind.
            generals = unit['general']
            result = values.get(generals[0]) if generals else Factor()
        elif unit['reference'] is not None:
            reference = values.get(unit['reference'])
            own = unit['prefix'] if unit['class'] == 'PrefixedUnit' else unit['scale']
            result = None if reference is None else own * reference

        if result is not None and (result.inexact == 0 if result.inexact is not None else result.rational == 0):
            return None
        return result

    def _compute_conversion(self, unit_id, values):
        """Compose value_ref = scale * value + offset to a terminal unit.

        Differences use scale only. Never multiply affine point units as factors.
        A terminal unit is explicit, so equal dimensions alone do not authorize
        conversion between different quantity kinds.
        """
        unit = self.units[unit_id]
        cls = unit['class']
        result = None
        if cls == 'GeneralConversionUnit':
            return None
        if cls in {'AffineConversionUnit', 'LinearConversionUnit', 'PrefixedUnit'}:
            parent = values.get(unit['reference'])
            if parent is not None:
                reference, scale, offset = parent
                own = unit['prefix'] if cls == 'PrefixedUnit' else unit['scale']
                own_offset = NumberSum(unit['offset']) if cls == 'AffineConversionUnit' else NumberSum()
                if (own.inexact == 0 if own.inexact is not None else own.rational == 0):
                    return None
                result = (reference, scale * own, own_offset * scale + offset)
        elif cls == 'SimpleUnit' and unit['general']:
            parents = unit['general']
            if parents:
                result = values.get(parents[0])
        elif self.unit_dims(unit_id) is not None and self.unit_si_factor(unit_id) is not None:
            result = (unit_id, Factor(), NumberSum())
        return result

    def resolve_conversions(self):
        self.seed_base_units()
        graph = {}
        for identifier, unit in self.units.items():
            if identifier in self._unit_si or unit['class'] in {'AffineConversionUnit', 'GeneralConversionUnit'}:
                dependencies = []
            elif unit['class'] == 'DerivedUnit':
                dependencies = [u for u, _ in unit['factors']]
            elif unit['reference'] is not None:
                dependencies = [unit['reference']]
            else:
                dependencies = unit['general'][:1]
            graph[identifier] = set(dependencies)
        self._unit_si = self.required_values(graph, self._compute_si_factor, 'SI factors')
        graph = {identifier: ({unit['reference']} if unit['reference'] is not None
                             else set(unit['general'][:1]) if unit['class'] == 'SimpleUnit' else set())
                 for identifier, unit in self.units.items()}
        self._unit_conversions = self.required_values(graph, self._compute_conversion, 'conversions')

    def unit_si_factor(self, unit_id):
        return self._unit_si.get(unit_id)

    def unit_conversion(self, unit_id):
        return self._unit_conversions.get(unit_id)

    def unresolved_dependencies(self, kind_id):
        """Leaf quantity kinds that prevent a dimension expression resolving."""
        if self.kind_dims(kind_id) is not None:
            return []
        pending, visited, leaves = [kind_id], set(), set()
        while pending:
            identifier = pending.pop()
            if identifier in visited or self.kind_dims(identifier) is not None:
                continue
            visited.add(identifier)
            kind = self.kinds.get(identifier)
            dependencies = [k for k, _ in kind['factors']] + kind['general'] if kind else []
            if dependencies:
                pending.extend(dependencies)
            else:
                leaves.add(identifier)
        return sorted(leaves or visited)

    # --- assembly ---------------------------------------------------------

    def assign_units(self):
        """Attach each unit to the kinds it measures.

        A unit that names no kind takes the kinds of its reference unit, then
        of its general unit. A kind with no unit of its own takes the units of
        its nearest general kind that has some.
        """
        unit_parents = {u: ([unit['reference']] if unit['reference'] else []) + unit['general']
                        for u, unit in self.units.items()}

        unit_rules = {u: [()] if unit['kinds'] else [(p,) for p in unit_parents[u]]
                      for u, unit in self.units.items()}

        def unit_kinds(identifier, index, values):
            if self.units[identifier]['kinds']:
                return self.units[identifier]['kinds']
            return values[unit_rules[identifier][index][0]]

        assignments = self.inferred_values(unit_rules, unit_kinds, 'quantity-kind assignment')
        self._assigned_kinds = {}
        for unit_id, unit in sorted(self.units.items()):
            kinds = [k for k in assignments[unit_id] or [] if k in self.kinds]
            self._assigned_kinds[unit_id] = kinds
            if not kinds:
                self.problem(unit_id, unit['name'], 'missing_relationship',
                             'unit: measures no quantity kind in the library')
            for kind_id in kinds:
                self.kinds[kind_id]['units'].append(unit_id)

        kind_rules = {k: [()] if kind['units'] else [(g,) for g in kind['general']]
                      for k, kind in self.kinds.items()}

        def inherited(identifier, index, values):
            kind = self.kinds[identifier]
            return kind['units'] or values[kind_rules[identifier][index][0]]

        borrowed = self.inferred_values(kind_rules, inherited, 'unit inheritance')
        for kind_id, units in borrowed.items():
            self.kinds[kind_id]['units'] = list(units or [])

    def build_scalars(self):
        scalars = []
        for kind_id, kind in self.kinds.items():
            name = kind['name']
            dims = self.kind_dims(kind_id)
            if dims is None:
                dependencies = self.unresolved_dependencies(kind_id)
                names = [self.kinds[k]['name'] if k in self.kinds else k for k in dependencies]
                self.problem(kind_id, name, 'unresolved_dimensions',
                             'kind: dimensions depend on unresolved ' + ', '.join(names))
                for unit_id in kind['units']:
                    self.problem(unit_id, self.units[unit_id]['name'], 'unresolved_dimensions',
                                 f'unit: excluded because quantity kind {name!r} has unresolved dimensions')
                continue
            if any(power.denominator != 1 for power in dims.values()):
                self.problem(kind_id, name, 'scalar_format', f'kind: non-integer dimension exponents {dims}')
                continue

            members = {}
            for unit_id in kind['units']:
                unit = self.units[unit_id]
                unit_dims = self.unit_dims(unit_id)
                factor = self.unit_si_factor(unit_id)
                if unit_dims != dims:
                    self.problem(unit_id, unit['name'], 'dimension_mismatch',
                                 f'unit of {name!r}: dimensions {_show(unit_dims)} differ from kind {_show(dims)}')
                elif factor is None:
                    conversion = self.unit_conversion(unit_id)
                    category = 'scalar_format' if conversion is not None else 'unsupported_conversion'
                    self.problem(unit_id, unit['name'], category,
                                 f'unit of {name!r}: non-multiplicative conversion'
                                 if conversion is not None else f'unit of {name!r}: unresolved conversion')
                else:
                    members[unit_id] = factor
            if not members:
                affine_only = bool(kind['units']) and all(
                    self.unit_conversion(u) is not None and self.unit_dims(u) == dims
                    and self.unit_si_factor(u) is None for u in kind['units'])
                self.problem(kind_id, name, 'scalar_format' if affine_only else 'missing_usable_unit',
                             'kind: no usable scalar unit')
                continue

            canonical_id = self._choose_canonical(members)
            if canonical_id is None:
                self.problem(kind_id, name, 'scalar_format',
                             'kind: no unit with factor exactly 1 to serve as canonical')
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
                'dimensions': {symbol: int(power) for symbol, power in sorted(dims.items())},
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
        self.apply_corrections()
        self.assign_units()
        self.resolve_dimensions()
        self.resolve_conversions()
        scalars = self.build_scalars()
        print(f'  emitted {len(scalars)} of {len(self.kinds)} quantity kinds, '
              f"{sum(len(s['units']) for s in scalars)} unit entries; "
              f'{len(self.problems)} scalar diagnostics', file=sys.stderr)
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
            kinds = {identifier: {
                'dimensions': dimensions(self.kind_dims(identifier)),
                'factors': [{'kind': k, 'exponent': str(e)} for k, e in self.kinds[identifier]['factors']],
                'unresolved_dependencies': self.unresolved_dependencies(identifier),
            } for identifier in sorted(self.kinds)}
            for identifier in sorted(self.units):
                factor = self.unit_si_factor(identifier)
                conversion = self.unit_conversion(identifier)
                units[identifier] = {
                    'name': self.units[identifier]['name'],
                    'symbol': self.units[identifier]['symbol'],
                    'quantity_kinds': self._assigned_kinds[identifier],
                    'dimensions': dimensions(self.unit_dims(identifier)),
                    'si_factor': None if factor is None else factor.exact_record(),
                    'conversion': None if conversion is None else {
                        'reference_unit': conversion[0],
                        'scale': conversion[1].exact_record(),
                        'offset': conversion[2].exact_record(),
                    },
                }
        except CorrectionError:
            raise
        except (ConversionError, ValueError, ArithmeticError, SyntaxError) as error:
            kinds, units = {}, {}
            self.problem(None, 'derived analysis', 'derived_analysis', str(error))
        numbers = {}
        for identifier, record in sorted(declarations.items()):
            if record['class'] in {'Integer', 'Real', 'Rational'}:
                try:
                    numbers[identifier] = self.constant(self.by_id[identifier]).exact_record()
                except (ConversionError, ValueError, ArithmeticError, SyntaxError) as error:
                    self.problem(identifier, record['name'] or identifier, 'unsupported_number', f'number: {error}')
        return {
            'schema_version': 2,
            'source': {'sha256': self.source_hash, 'format': 'OMG-QUDV-XMI'},
            'declarations': dict(sorted(declarations.items())),
            'resolved': {'kinds': kinds, 'units': units, 'numbers': numbers},
            'applied_corrections': self.applied_corrections,
            'diagnostics': {
                'problems': sorted((p for p in self._diagnostics if p['category'] != 'scalar_format'),
                                   key=lambda p: (p['subject'], p['reason'])),
                'scalar_exclusions': sorted((p for p in self._diagnostics if p['category'] == 'scalar_format'),
                                            key=lambda p: (p['subject'], p['reason'])),
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
        f'# {len(scalars)} quantity kinds emitted; {len(problems)} scalar diagnostics (not distinct omissions):',
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
                        help='multiplicative unit families or source-preserving catalog (schema 2)')
    parser.add_argument('--corrections', help='apply an attributed, source-hash-pinned correction YAML')
    parser.add_argument('--strict', action='store_true',
                        help='exit 1 on resolution gaps, even if preserved in the catalog')
    args = parser.parse_args()

    try:
        converter = ISO80000Converter(args.xmi, corrections_file=args.corrections)
        if args.format == 'catalog':
            catalog = converter.catalog()
            text = yaml.safe_dump(catalog, sort_keys=False, allow_unicode=True)
            problems = [(p['subject'], p['reason']) for p in catalog['diagnostics']['problems']]
        else:
            scalars = converter.convert()
            text = render(scalars, converter.problems, converter.corrections)
            problems = converter.problems
    except ConversionError as error:
        print(f'conversion failed: {error}', file=sys.stderr)
        return 2
    if args.output:
        with open(args.output, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
    else:
        sys.stdout.buffer.write(text.encode('utf-8'))
    for subject, what in sorted(set(converter.corrections)):
        print(f'  corrected: {subject}: {what}', file=sys.stderr)
    for subject, reason in sorted(problems):
        label = 'unresolved in derived view' if args.format == 'catalog' else 'not emitted'
        print(f'  {label}: {subject}: {reason}', file=sys.stderr)
    return 1 if args.strict and problems else 0


if __name__ == '__main__':
    sys.exit(main())
