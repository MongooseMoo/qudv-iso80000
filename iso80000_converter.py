#!/usr/bin/env python3
"""
ISO-80000 XMI to YAML converter.

Reads the OMG SysML QUDV model library
(http://www.omg.org/spec/SysML/20150709/ISO-80000.xmi) and writes a
source-preserving catalog (schema 2): every source declaration, a separately
labelled derived view of dimensions, unit relationships and conversions, and a
structured diagnostic for everything the derived view cannot resolve.

Usage:
    python iso80000_converter.py [ISO-80000.xmi] [-o catalog.yml]
                                 [--corrections iso80000-corrections.yml] [--strict]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import html
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Iterable, Mapping, Optional, Union

import yaml
from lxml import etree

from dependency_resolution import infer_alternatives, resolve_dependencies, unresolved_cycles

XMI_NS = 'http://www.omg.org/spec/XMI/20131001'
XMI_ID = f'{{{XMI_NS}}}id'
XMI_IDREF = f'{{{XMI_NS}}}idref'
XMI_TYPE = f'{{{XMI_NS}}}type'

# ISO 80000-1 dimension symbols of the ISQ base quantities. The library says
# which kinds are base quantities (A_systemOfQuantities_baseQuantityKind links)
# but records only their quantity symbols (l, m, t, ...), never a dimension
# symbol, so this one fact is supplied here and checked against those links.
BASE_DIMENSION_SYMBOLS = {
    'length': 'L',
    'mass': 'M',
    'time': 'T',
    'electric current': 'I',
    'thermodynamic temperature': 'Θ',
    'amount of substance': 'N',
    'luminous intensity': 'J',
}

KIND_CLASSES = frozenset({'SimpleQuantityKind', 'DerivedQuantityKind'})
CHAIN_CLASSES = frozenset({'PrefixedUnit', 'LinearConversionUnit', 'AffineConversionUnit'})
UNIT_CLASSES = CHAIN_CLASSES | {'SimpleUnit', 'DerivedUnit', 'GeneralConversionUnit'}
NUMBER_CLASSES = frozenset({'Integer', 'Real', 'Rational'})


class ConversionError(Exception):
    """The XMI says something this converter cannot represent.

    ``declaration`` is the source ID whose content caused the failure, when known.
    """

    def __init__(self, reason: str, declaration: Optional[str] = None):
        super().__init__(reason if declaration is None else f'{declaration}: {reason}')
        self.reason = reason
        self.declaration = declaration


class CorrectionError(ConversionError):
    """An explicitly requested correction cannot be applied safely."""


def symbol_text(markup: str) -> str:
    """Plain text of a unit symbol the library stores as text or as HTML fragments."""
    if '<' not in markup:
        return markup.strip()
    text = re.sub(r'<style.*?</style>', '', markup, flags=re.S | re.I)
    text = re.sub(r'<sup>\s*(.*?)\s*</sup>', r'^\1', text, flags=re.S | re.I)
    text = re.sub(r'<sub>\s*(.*?)\s*</sub>', r'_\1', text, flags=re.S | re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\s+', '', html.unescape(text))


# --- numbers ------------------------------------------------------------------

@dataclass(frozen=True)
class Exact:
    """An exact factor: rational * pi**pi_exp."""
    rational: Fraction
    pi_exp: int = 0

    def as_float(self) -> float:
        return float(self.rational) * math.pi ** self.pi_exp

    def is_zero(self) -> bool:
        return self.rational == 0

    def record(self) -> dict[str, Any]:
        return {'rational': str(self.rational), 'pi_exponent': self.pi_exp}


@dataclass(frozen=True)
class Approximate:
    """A factor whose exactness was lost once, where the source uses a real (ln 10)."""
    value: float

    def as_float(self) -> float:
        return self.value

    def is_zero(self) -> bool:
        return self.value == 0

    def record(self) -> dict[str, Any]:
        return {'approximate': self.value}


Factor = Union[Exact, Approximate]
ONE = Exact(Fraction(1))


def multiply(a: Factor, b: Factor) -> Factor:
    if isinstance(a, Exact) and isinstance(b, Exact):
        return Exact(a.rational * b.rational, a.pi_exp + b.pi_exp)
    return Approximate(a.as_float() * b.as_float())


def power(base: Factor, exponent: Fraction) -> Factor:
    if exponent < 0 and base.is_zero():
        raise ConversionError('zero has no reciprocal')
    if isinstance(base, Exact):
        if exponent.denominator == 1:
            n = exponent.numerator
            return Exact(base.rational ** n, base.pi_exp * n)
        if base == ONE:
            return ONE
    return Approximate(base.as_float() ** float(exponent))


@dataclass(frozen=True)
class ExactSum:
    """An exact affine offset: rational multiples of distinct powers of pi, ascending."""
    terms: tuple[Exact, ...] = ()

    def is_zero(self) -> bool:
        return not self.terms

    def record(self) -> dict[str, Any]:
        if not self.terms:
            return Exact(Fraction(0)).record()
        if len(self.terms) == 1:
            return self.terms[0].record()
        return {'sum': [term.record() for term in self.terms]}


Offset = Union[ExactSum, Approximate]
ZERO_OFFSET = ExactSum()


def offset_of(terms: Iterable[Factor]) -> Offset:
    """Sum factors; mixed powers of pi stay symbolic, an approximate term makes the sum approximate."""
    terms = list(terms)
    if any(isinstance(term, Approximate) for term in terms):
        return Approximate(sum(term.as_float() for term in terms))
    by_power: dict[int, Fraction] = defaultdict(Fraction)
    for term in terms:
        if isinstance(term, Exact):
            by_power[term.pi_exp] += term.rational
    return ExactSum(tuple(Exact(value, p) for p, value in sorted(by_power.items()) if value))


def offset_terms(offset: Offset) -> tuple[Factor, ...]:
    return offset.terms if isinstance(offset, ExactSum) else (offset,)


def add_offsets(a: Offset, b: Offset) -> Offset:
    return offset_of([*offset_terms(a), *offset_terms(b)])


def scale_offset(offset: Offset, scale: Factor) -> Offset:
    return offset_of(multiply(term, scale) for term in offset_terms(offset))


def evaluate_constant(expr: str) -> Factor:
    """Evaluate a QUDV literal body such as '10^3', '(2^10)^2', 'Pi/180', 'ln(10)'."""
    source = expr.replace('^', '**')
    try:
        tree = ast.parse(source, mode='eval')
    except SyntaxError as error:
        raise ConversionError(f'unparseable constant {expr!r}') from error

    def walk(node: ast.AST) -> Factor:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            return Exact(Fraction(node.value))
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            literal = ast.get_source_segment(source, node)
            if literal is None:
                raise ConversionError(f'missing source text for numeric literal in {expr!r}')
            return Exact(Fraction(literal))
        if isinstance(node, ast.Name) and node.id == 'Pi':
            return Exact(Fraction(1), 1)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return multiply(Exact(Fraction(-1)), walk(node.operand))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            return multiply(walk(node.left), walk(node.right))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return multiply(walk(node.left), power(walk(node.right), Fraction(-1)))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            exponent = walk(node.right)
            if not isinstance(exponent, Exact) or exponent.pi_exp != 0:
                raise ConversionError(f'non-rational exponent in constant {expr!r}')
            return power(walk(node.left), exponent.rational)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'ln' and len(node.args) == 1 and not node.keywords):
            return Approximate(math.log(walk(node.args[0]).as_float()))
        raise ConversionError(f'unsupported constant expression {expr!r}')

    try:
        return walk(tree)
    except (ValueError, OverflowError) as error:
        raise ConversionError(f'cannot evaluate constant {expr!r}: {error}') from error


# --- source declarations ------------------------------------------------------

@dataclass(frozen=True)
class Ref:
    """A slot value naming another declaration; it always resolves."""
    id: str

    def record(self) -> dict[str, Any]:
        return {'ref': self.id}


@dataclass(frozen=True)
class Href:
    uri: str

    def record(self) -> dict[str, Any]:
        return {'href': self.uri}


@dataclass(frozen=True)
class Literal:
    """A UML literal; ``text`` is the source text, unstripped."""
    type: str
    text: str

    def record(self) -> dict[str, Any]:
        return {'type': self.type, 'value': self.text}


@dataclass(frozen=True)
class Opaque:
    """Any other value, preserved as an XML tree and never interpreted."""
    xml: dict[str, Any]

    def record(self) -> dict[str, Any]:
        return {'xml': self.xml}


Value = Union[Ref, Href, Literal, Opaque]


def source_tree(elem: Any) -> dict[str, Any]:
    """Preserve opaque specification syntax without evaluating its language."""
    result: dict[str, Any] = {'tag': elem.tag}
    if elem.attrib:
        result['attributes'] = dict(sorted(elem.attrib.items()))
    if elem.text and elem.text.strip():
        result['text'] = elem.text
    if elem.tail and elem.tail.strip():
        result['tail'] = elem.tail
    children = [source_tree(child) for child in elem if isinstance(child.tag, str)]
    if children:
        result['children'] = children
    return result


def parse_value(elem: Any) -> Value:
    reference = elem.get(XMI_IDREF)
    instance = elem.find('./instance')
    if reference is None and instance is not None:
        reference = instance.get(XMI_IDREF)
    if reference is not None:
        return Ref(reference)
    if elem.get('href') is not None:
        return Href(elem.get('href'))
    value_type = elem.get(XMI_TYPE, '')
    if value_type.startswith('uml:Literal'):
        text = elem.get('value')
        if text is None:
            inner = elem.find('./value')
            text = inner.text if inner is not None else elem.text
        return Literal(value_type, text or '')
    return Opaque(source_tree(elem))


@dataclass
class Declaration:
    """One XMI instance specification, parsed once."""
    id: str
    cls: str
    name: Optional[str]
    classifiers: list[dict[str, str]]
    features: dict[str, str]
    slots: dict[str, list[Value]]
    specification: Optional[dict[str, Any]]
    body: Optional[str]

    @staticmethod
    def parse(elem: Any) -> Declaration:
        identifier = elem.get(XMI_ID)
        if not identifier:
            raise ConversionError('instance specification has no XMI id')
        features: dict[str, str] = {}
        slots: dict[str, list[Value]] = {}
        for slot in elem.findall('./slot'):
            feature = slot.find('./definingFeature')
            uri = feature.get('href') if feature is not None else None
            if not uri:
                raise ConversionError('slot has no defining feature URI', identifier)
            name = uri.rsplit('.', 1)[-1]
            if name in slots:
                raise ConversionError(f'duplicate slot name {name!r}', identifier)
            features[name] = uri
            slots[name] = [parse_value(value) for value in slot.findall('./value')]
        classifier = elem.find('./classifier')
        name_elem = elem.find('name')
        specification = elem.find('./specification')
        body = elem.find('./specification/body')
        return Declaration(
            id=identifier,
            cls=classifier.get('href', '').rsplit('_', 1)[-1] if classifier is not None else '',
            name=name_elem.text if name_elem is not None else elem.get('name'),
            classifiers=[dict(c.attrib) for c in elem.findall('./classifier')],
            features=features,
            slots=slots,
            specification=source_tree(specification) if specification is not None else None,
            body=body.text if body is not None else None,
        )

    def record(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            'class': self.cls, 'name': self.name, 'classifiers': self.classifiers,
            'slots': {name: [value.record() for value in values] for name, values in self.slots.items()},
            'features': self.features,
        }
        if self.specification is not None:
            result['specification'] = self.specification
        return result

    def refs(self, feature: str) -> list[str]:
        result = []
        for value in self.slots.get(feature, []):
            if not isinstance(value, Ref):
                raise ConversionError(f'slot {feature!r} holds a {type(value).__name__}, not a reference', self.id)
            result.append(value.id)
        return result

    def ref(self, feature: str) -> Optional[str]:
        refs = self.refs(feature)
        if len(refs) > 1:
            raise ConversionError(f'slot {feature!r} has {len(refs)} references, expected at most one', self.id)
        return refs[0] if refs else None

    def required_ref(self, feature: str) -> str:
        reference = self.ref(feature)
        if reference is None:
            raise ConversionError(f'slot {feature!r} is missing', self.id)
        return reference

    def text(self, feature: str) -> Optional[str]:
        values = self.slots.get(feature, [])
        if len(values) > 1:
            raise ConversionError(f'slot {feature!r} has {len(values)} values, expected at most one', self.id)
        if not values:
            return None
        value = values[0]
        if not isinstance(value, Literal):
            raise ConversionError(f'slot {feature!r} holds a {type(value).__name__}, not a literal', self.id)
        return value.text.strip()

    def flag(self, feature: str) -> bool:
        text = (self.text(feature) or '').lower()
        if text in ('', 'false'):
            return False
        if text == 'true':
            return True
        raise ConversionError(f'slot {feature!r} is not a boolean: {text!r}', self.id)


# --- derived model ------------------------------------------------------------

@dataclass(frozen=True)
class FactorLink:
    """One factor of a derived kind or unit: target ** exponent.

    ``source`` is the QUDV factor instance and ``named_exponent`` the exponent its
    'target^exponent' name states. Both are provenance, not part of the value, and
    are absent for a factor introduced by a correction.
    """
    target: str
    exponent: Fraction
    source: Optional[str] = field(default=None, compare=False)
    named_exponent: Optional[Fraction] = field(default=None, compare=False)


@dataclass(frozen=True)
class Diagnostic:
    """Something the derived view does not resolve, and the declaration it concerns."""
    declaration: Optional[str]
    subject: str
    category: str
    reason: str

    def record(self) -> dict[str, Any]:
        return {'id': self.declaration, 'subject': self.subject, 'category': self.category, 'reason': self.reason}


Conversion = tuple[str, Factor, Offset]  # (terminal reference unit, scale, offset)
Record = dict[str, Any]


def _correction_text(converter: ISO80000Converter, record: Record, raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('expected non-empty text')
    return raw


def _correction_bool(converter: ISO80000Converter, record: Record, raw: Any) -> bool:
    if not isinstance(raw, bool):
        raise ValueError('expected true or false')
    return raw


def _correction_number(converter: ISO80000Converter, record: Record, raw: Any) -> Factor:
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError('expected an exact number expression')
    return evaluate_constant(str(raw))


def _correction_kinds(converter: ISO80000Converter, record: Record, raw: Any) -> list[str]:
    if (not isinstance(raw, list) or not all(isinstance(k, str) and k in converter.kinds for k in raw)
            or len(raw) != len(set(raw))):
        raise ValueError('expected distinct quantity kind IDs')
    return list(raw)


def _correction_factors(converter: ISO80000Converter, record: Record, raw: Any) -> list[FactorLink]:
    targets = converter.kinds if record['class'] in KIND_CLASSES else converter.units
    if not isinstance(raw, list):
        raise ValueError('expected a list of [target ID, exponent] pairs')
    links = []
    for item in raw:
        if (not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str) or item[0] not in targets
                or isinstance(item[1], bool) or not isinstance(item[1], (str, int))):
            raise ValueError(f'expected [target ID, exponent] naming an existing declaration, not {item!r}')
        links.append(FactorLink(item[0], Fraction(item[1])))
    return links


# Every correctable field: the record classes it applies to, and the parser that
# turns both its `expected` and its `value` into the record's typed value.
CORRECTABLE_FIELDS: dict[str, tuple[frozenset[str], Callable[[ISO80000Converter, Record, Any], Any]]] = {
    'name': (UNIT_CLASSES, _correction_text),
    'symbol': (UNIT_CLASSES, _correction_text),
    'kinds': (UNIT_CLASSES, _correction_kinds),
    'factors': (KIND_CLASSES | {'DerivedUnit'}, _correction_factors),
    'scale': (CHAIN_CLASSES, _correction_number),
    'offset': (frozenset({'AffineConversionUnit'}), _correction_number),
    'entity_count': (KIND_CLASSES, _correction_bool),
}


def _compose(prefix: Optional[str], reference: Optional[str]) -> Optional[str]:
    return None if prefix is None or reference is None else prefix + reference


class ISO80000Converter:
    def __init__(self, xmi_file: str, corrections_file: Optional[str] = None):
        print(f'Loading {xmi_file}...', file=sys.stderr)
        with open(xmi_file, 'rb') as handle:
            source = handle.read()
        self.source_hash = hashlib.sha256(source).hexdigest()
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        root = etree.fromstring(source, parser=parser)
        seen_ids: set[str] = set()
        self.declarations: dict[str, Declaration] = {}
        for elem in root.iter():
            elem_id = elem.get(XMI_ID)
            if elem_id:
                if elem_id in seen_ids:
                    raise ConversionError(f'duplicate XMI id {elem_id!r}')
                seen_ids.add(elem_id)
            if elem.get(XMI_TYPE) == 'uml:InstanceSpecification':
                declaration = Declaration.parse(elem)
                self.declarations[declaration.id] = declaration
        for declaration in self.declarations.values():
            for feature, values in declaration.slots.items():
                for value in values:
                    if isinstance(value, Ref) and value.id not in self.declarations:
                        raise ConversionError(
                            f'slot {feature!r} refers to {value.id!r}, which is not an instance specification',
                            declaration.id)

        self.kinds: dict[str, Record] = {}
        self.units: dict[str, Record] = {}
        self.prefixes: dict[str, Record] = {}
        self.base_units: set[str] = set()
        self.base_kinds: set[str] = set()
        self.kind_dimensions: dict[str, Optional[dict[str, Fraction]]] = {}
        self.unit_dimensions: dict[str, Optional[dict[str, Fraction]]] = {}
        self.si_factors: dict[str, Optional[Factor]] = {}
        self.conversions: dict[str, Optional[Conversion]] = {}
        self.assigned_kinds: dict[str, list[str]] = {}
        self._constants: dict[str, Factor] = {}
        self._diagnostics: dict[Diagnostic, None] = {}
        self._derived: Optional[tuple[dict[str, Any], dict[str, Any]]] = None
        self.applied_corrections: Optional[dict[str, Any]] = None
        self.correction_manifest: Optional[dict[str, Any]] = None
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

    def problem(self, identifier: Optional[str], subject: str, category: str, reason: str) -> None:
        self._diagnostics[Diagnostic(identifier, subject, category, reason)] = None

    # --- numbers ----------------------------------------------------------

    def constant(self, identifier: str) -> Factor:
        if identifier in self._constants:
            return self._constants[identifier]
        declaration = self.declarations[identifier]
        try:
            if declaration.cls == 'Rational':
                result = multiply(self.constant(declaration.required_ref('numerator')),
                                  power(self.constant(declaration.required_ref('denominator')), Fraction(-1)))
            else:
                match = re.search(r'\((.*)\)\s*$', declaration.body or '')
                if not match:
                    raise ConversionError('constant has no literal body')
                result = evaluate_constant(match.group(1))
        except ConversionError as error:
            if error.declaration is not None:
                raise
            raise ConversionError(error.reason, identifier) from error
        self._constants[identifier] = result
        return result

    def exponent(self, identifier: str) -> Fraction:
        value = self.constant(identifier)
        if not isinstance(value, Exact) or value.pi_exp != 0:
            raise ConversionError('exponent is not rational', identifier)
        return value.rational

    # --- model ------------------------------------------------------------

    def load_model(self) -> None:
        """Read prefixes, kinds, units and the link instances that relate them."""
        for d in self.declarations.values():
            if d.cls == 'Prefix':
                symbol = d.text('symbol')
                self.prefixes[d.id] = {
                    'name': d.name,
                    'symbol': symbol_text(symbol) or None if symbol is not None else None,
                    'scale': self.constant(d.required_ref('factor')),
                }
        for d in self.declarations.values():
            if d.cls in KIND_CLASSES:
                self.kinds[d.id] = {
                    'class': d.cls,
                    'name': d.name,
                    'factors': [self._factor_link(f, 'quantityKind') for f in d.refs('factor')],
                    'general': d.refs('general'),
                    'dimension_one': d.flag('isQuantityOfDimensionOne'),
                    'entity_count': d.flag('isNumberOfEntities'),
                    'units': [],
                }
            elif d.cls in UNIT_CLASSES:
                symbol = d.text('symbol')
                prefix = d.required_ref('prefix') if d.cls == 'PrefixedUnit' else None
                if prefix is not None and prefix not in self.prefixes:
                    raise ConversionError('prefix slot does not name a prefix', d.id)
                scale = (self.prefixes[prefix]['scale'] if prefix is not None
                         else self.constant(d.required_ref('factor')) if d.cls in CHAIN_CLASSES else None)
                self.units[d.id] = {
                    'class': d.cls,
                    'name': d.name,
                    'symbol': symbol_text(symbol) or None if symbol is not None else None,
                    'kinds': d.refs('quantityKind'),
                    'factors': [self._factor_link(f, 'unit') for f in d.refs('factor')]
                               if d.cls == 'DerivedUnit' else [],
                    'prefix': prefix,
                    'scale': scale,
                    'offset': self.constant(d.required_ref('offset')) if d.cls == 'AffineConversionUnit' else None,
                    'reference': d.ref('referenceUnit'),
                    'general': d.refs('general'),
                }

        for d in self.declarations.values():
            if d.cls == 'measurementUnit':  # A_quantityKind_measurementUnit link
                for unit in d.refs('measurementUnit'):
                    for kind in d.refs('quantityKind'):
                        record = self.units.get(unit)
                        if record is not None and kind not in record['kinds']:
                            record['kinds'].append(kind)
            elif d.cls == 'baseUnit':  # A_systemOfUnits_baseUnit link
                self.base_units.update(d.refs('baseUnit'))
            elif d.cls == 'baseQuantityKind':  # A_systemOfQuantities_baseQuantityKind link
                self.base_kinds.update(d.refs('baseQuantityKind'))

        print(f'  {len(self.kinds)} quantity kinds, {len(self.units)} units', file=sys.stderr)

    def _factor_link(self, factor_id: str, target_feature: str) -> FactorLink:
        """A QuantityKindFactor or UnitFactor, with the exponent its name states."""
        factor = self.declarations[factor_id]
        named = re.search(r'\^(-?\d+(?:/\d+)?)$', factor.name or '')
        return FactorLink(factor.required_ref(target_feature), self.exponent(factor.required_ref('exponent')),
                          factor_id, Fraction(named.group(1)) if named else None)

    def apply_corrections(self) -> None:
        """Validate every edit before changing the derived model, never the source."""
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
            target, name = change['target'], change['field']
            record = self.units.get(target, self.kinds.get(target))
            if record is None or (target, name) in seen:
                raise CorrectionError(f'correction has missing or duplicate target/field: {target}.{name}')
            seen.add((target, name))
            rule = CORRECTABLE_FIELDS.get(name)
            if rule is None or record['class'] not in rule[0]:
                raise CorrectionError(f'unsupported correction field {name!r} for a {record["class"]}', target)
            parse = rule[1]
            try:
                expected = parse(self, record, change['expected'])
                value = parse(self, record, change['value'])
            except (ConversionError, ValueError, ZeroDivisionError) as error:
                raise CorrectionError(f'invalid correction {name}: {error}', target) from error
            if record[name] != expected:
                raise CorrectionError(f'correction precondition failed: {name} is {record[name]!r}', target)
            pending.append((target, record, name, value))
        previous = {}
        for target, record, name, value in pending:
            previous[(target, name)] = record[name]
            record[name] = value
        self._derive_prefixed_text(previous)
        self.applied_corrections = {'sha256': self.correction_hash, **self.correction_manifest}

    def _derive_prefixed_text(self, previous: dict[tuple[str, str], Any]) -> None:
        """A prefixed unit's name and symbol are its prefix's followed by its reference's.

        When a correction changes a reference unit's name or symbol, each prefixed
        unit of it follows, provided its source text was that same composition.
        Any other disagreement must be declared as its own correction.
        """
        changed = {key: old for key, old in previous.items() if key[1] in ('name', 'symbol')}
        derived = set()
        while changed:
            following = {}
            for unit_id, unit in self.units.items():
                if unit['class'] != 'PrefixedUnit':
                    continue
                for text_field in ('name', 'symbol'):
                    key = (unit['reference'], text_field)
                    if key not in changed or (unit_id, text_field) in previous or (unit_id, text_field) in derived:
                        continue
                    prefix = self.prefixes[unit['prefix']][text_field]
                    if unit[text_field] != _compose(prefix, changed[key]):
                        raise CorrectionError(
                            f'{text_field} {unit[text_field]!r} is not prefix {prefix!r} followed by the '
                            f'corrected reference {text_field} {changed[key]!r}; declare its correction', unit_id)
                    following[(unit_id, text_field)] = unit[text_field]
                    derived.add((unit_id, text_field))
                    unit[text_field] = _compose(prefix, self.units[unit['reference']][text_field])
            changed = following

    def check_factor_names(self) -> None:
        """Every factor is named 'target^exponent'; a name that disagrees must be corrected."""
        for record in [*self.kinds.values(), *self.units.values()]:
            for link in record['factors']:
                if link.named_exponent is not None and link.named_exponent != link.exponent:
                    raise ConversionError(
                        f'exponent slot says {link.exponent} but the name says {link.named_exponent}; '
                        f'declare a correction of {record["name"]!r} factors', link.source)

    # --- dependencies -----------------------------------------------------

    def report_dependencies(self, graph: dict[str, set[str]], values: Mapping[str, Any], stage: str) -> None:
        """Diagnose missing references and remaining cyclic components."""
        records = {**self.kinds, **self.units}
        for identifier, dependencies in graph.items():
            for dependency in dependencies:
                if dependency not in graph:
                    self.problem(identifier, records[identifier]['name'], 'missing_reference',
                                 f'{stage}: missing reference {dependency}')
        for cycle in unresolved_cycles(graph, values):
            names = [records[k]['name'] for k in cycle]
            for identifier in cycle:
                self.problem(identifier, records[identifier]['name'], 'dependency_cycle',
                             f'{stage}: dependency cycle involving ' + ', '.join(names))

    def required_values(self, graph: dict[str, set[str]], evaluate: Callable[[str, Mapping[str, Any]], Any],
                        stage: str) -> dict[str, Any]:
        values = resolve_dependencies(graph, evaluate)
        self.report_dependencies(graph, values, stage)
        return values

    def inferred_values(self, alternatives: dict[str, list[tuple[str, ...]]],
                        evaluate: Callable[[str, int, Mapping[str, Any]], Any], stage: str) -> dict[str, Any]:
        values = infer_alternatives(alternatives, evaluate)
        # This union is only a diagnostic graph, never a scheduling graph.
        graph = {node: {parent for rule in rules for parent in rule}
                 for node, rules in alternatives.items()}
        self.report_dependencies(graph, values, stage)
        return values

    # --- dimensions -------------------------------------------------------

    def base_dimensions(self) -> dict[str, str]:
        """The dimension symbol of each kind the library links as an ISQ base quantity."""
        result = {}
        for kind_id in sorted(self.base_kinds):
            kind = self.kinds.get(kind_id)
            if kind is None:
                raise ConversionError('base quantity link names a declaration that is not a quantity kind', kind_id)
            symbol = BASE_DIMENSION_SYMBOLS.get(kind['name'])
            if symbol is None:
                raise ConversionError(f'base quantity {kind["name"]!r} has no ISO 80000-1 dimension symbol', kind_id)
            result[kind_id] = symbol
        if len(set(result.values())) != len(result):
            raise ConversionError('two base quantities share a dimension symbol')
        return result

    def resolve_dimensions(self) -> None:
        base = self.base_dimensions()
        # A kind's dimensions are stated directly by being a base quantity or by
        # a flag; statements that disagree must be resolved by a correction.
        constants: dict[str, dict[str, Fraction]] = {}
        for identifier, kind in self.kinds.items():
            claims = []
            if identifier in base:
                claims.append(('base quantity', {base[identifier]: Fraction(1)}))
            if kind['dimension_one']:
                claims.append(('dimension-one flag', {}))
            if kind['entity_count']:
                claims.append(('entity-count flag', {}))
            for source, dims in claims[1:]:
                if dims != claims[0][1]:
                    raise ConversionError(f'{claims[0][0]} gives {_show(claims[0][1])} but {source} gives '
                                          f'{_show(dims)}; declare a correction', identifier)
            if claims:
                constants[identifier] = claims[0][1]

        # Each option is a product of dependencies; copy is exponent one.
        options: dict[str, list[list[tuple[str, Fraction]]]] = {}
        for identifier, kind in self.kinds.items():
            alternatives = [[(link.target, link.exponent) for link in kind['factors']]] if kind['factors'] else []
            alternatives += [[(k, Fraction(1))] for k in kind['general']]
            alternatives += [[(u, Fraction(1))] for u in kind['units']
                             if self.units[u]['class'] == 'DerivedUnit']
            options[identifier] = alternatives
        for identifier, unit in self.units.items():
            if unit['class'] == 'DerivedUnit':
                alternatives = [[(link.target, link.exponent) for link in unit['factors']]] if unit['factors'] else []
            elif unit['reference'] is not None:
                alternatives = [[(unit['reference'], Fraction(1))]]
            else:
                alternatives = [[(k, Fraction(1))] for k in unit['kinds'] + unit['general']]
            options[identifier] = alternatives
        rules = {k: [()] if k in constants else [tuple(d for d, _ in option) for option in opts]
                 for k, opts in options.items()}

        def evaluate(identifier: str, index: int, values: Mapping[str, Any]) -> Optional[dict[str, Fraction]]:
            if identifier in constants:
                return constants[identifier]
            return _sum_dims((values[k], power) for k, power in options[identifier][index])

        values = self.inferred_values(rules, evaluate, 'dimensions')
        self.kind_dimensions = {k: values[k] for k in self.kinds}
        self.unit_dimensions = {u: values[u] for u in self.units}
        for identifier, dims in constants.items():
            factors = self.kinds[identifier]['factors']
            factor_dims = _sum_dims((values.get(link.target), link.exponent) for link in factors)
            if factors and factor_dims is not None and factor_dims != dims:
                raise ConversionError(f'stated dimensions {_show(dims)} disagree with factors giving '
                                      f'{_show(factor_dims)}; declare a correction', identifier)

    def unresolved_dependencies(self, kind_id: str) -> list[str]:
        """Leaf quantity kinds that prevent a dimension expression resolving."""
        if self.kind_dimensions.get(kind_id) is not None:
            return []
        pending, visited, leaves = [kind_id], set(), set()
        while pending:
            identifier = pending.pop()
            if identifier in visited or self.kind_dimensions.get(identifier) is not None:
                continue
            visited.add(identifier)
            kind = self.kinds.get(identifier)
            dependencies = [link.target for link in kind['factors']] + kind['general'] if kind else []
            if dependencies:
                pending.extend(dependencies)
            else:
                leaves.add(identifier)
        return sorted(leaves or visited)

    # --- conversions ------------------------------------------------------

    def resolve_conversions(self) -> None:
        """Compose each unit's own scale and offset once; SI factors derive from the result.

        A conversion is value_ref = scale * value + offset to a terminal unit, a
        simple or derived unit that is not defined from another. SI base units
        have SI factor 1, which fixes their terminal's factor: the library makes
        kilogram a prefixed gram, so gram is 1/1000. Any other simple terminal is
        the coherent unit of its kind and a derived terminal multiplies its
        factors. A unit whose conversion is linear has SI factor scale times its
        terminal's; an affine one has none. A terminal is explicit, so equal
        dimensions alone never authorize conversion between different kinds.
        """
        def chain(unit_id: str, values: Mapping[str, Any]) -> Optional[Conversion]:
            unit = self.units[unit_id]
            cls = unit['class']
            if cls == 'GeneralConversionUnit':
                return None
            if cls in CHAIN_CLASSES:
                parent = values.get(unit['reference']) if unit['reference'] is not None else None
                if parent is None or unit['scale'].is_zero():
                    return None
                reference, parent_scale, parent_offset = parent
                own_offset = offset_of([unit['offset']]) if cls == 'AffineConversionUnit' else ZERO_OFFSET
                return (reference, multiply(parent_scale, unit['scale']),
                        add_offsets(scale_offset(own_offset, parent_scale), parent_offset))
            if cls == 'SimpleUnit' and unit['general']:
                return values.get(unit['general'][0])
            return (unit_id, ONE, ZERO_OFFSET)

        graph = {identifier: ({unit['reference']} if unit['reference'] is not None
                              else set(unit['general'][:1]) if unit['class'] == 'SimpleUnit' else set())
                 for identifier, unit in self.units.items()}
        chains: dict[str, Optional[Conversion]] = self.required_values(graph, chain, 'conversions')

        seeds: dict[str, Factor] = {}
        for base_id in sorted(self.base_units):
            base_chain = chains.get(base_id)
            if base_chain is None:
                raise ConversionError('SI base unit has no resolved reference chain', base_id)
            terminal, scale, offset = base_chain
            if not offset.is_zero():
                raise ConversionError('SI base unit is not a multiple of its reference unit', base_id)
            seed = power(scale, Fraction(-1))
            if seeds.setdefault(terminal, seed) != seed:
                raise ConversionError('SI base units disagree about their common reference unit', base_id)

        def si_factor(unit_id: str, values: Mapping[str, Any]) -> Optional[Factor]:
            unit_chain = chains.get(unit_id)
            if unit_chain is None:
                return None
            terminal, scale, offset = unit_chain
            if terminal != unit_id:
                coherent = values.get(terminal)
                return multiply(scale, coherent) if coherent is not None and offset.is_zero() else None
            if unit_id in seeds:
                return seeds[unit_id]
            unit = self.units[unit_id]
            if unit['class'] == 'SimpleUnit':
                return ONE
            if not unit['factors']:
                return None
            result: Factor = ONE
            for link in unit['factors']:
                part = values.get(link.target)
                if part is None:
                    return None
                result = multiply(result, power(part, link.exponent))
            return result

        def si_dependencies(unit_id: str) -> set[str]:
            unit_chain = chains.get(unit_id)
            if unit_chain is None:
                return set()
            if unit_chain[0] != unit_id:
                return {unit_chain[0]}
            if unit_id in seeds:
                return set()
            return {link.target for link in self.units[unit_id]['factors']}

        self.si_factors = self.required_values({u: si_dependencies(u) for u in self.units}, si_factor, 'SI factors')
        self.conversions = {
            unit_id: unit_chain if unit_chain is not None
            and self.unit_dimensions.get(unit_chain[0]) is not None
            and self.si_factors.get(unit_chain[0]) is not None else None
            for unit_id, unit_chain in chains.items()}

    # --- assembly ---------------------------------------------------------

    def assign_units(self) -> None:
        """Attach each unit to the kinds it measures.

        A unit that names no kind takes the kinds of its reference unit, then
        of its general unit. A kind with no unit of its own takes the units of
        its nearest general kind that has some.
        """
        unit_parents = {u: ([unit['reference']] if unit['reference'] else []) + unit['general']
                        for u, unit in self.units.items()}

        unit_rules: dict[str, list[tuple[str, ...]]] = {
                      u: [()] if unit['kinds'] else [(p,) for p in unit_parents[u]]
                      for u, unit in self.units.items()}

        def unit_kinds(identifier: str, index: int, values: Mapping[str, Any]) -> list[str]:
            if self.units[identifier]['kinds']:
                return self.units[identifier]['kinds']
            return values[unit_rules[identifier][index][0]]

        assignments = self.inferred_values(unit_rules, unit_kinds, 'quantity-kind assignment')
        self.assigned_kinds = {}
        for unit_id, unit in sorted(self.units.items()):
            kinds = [k for k in assignments[unit_id] or [] if k in self.kinds]
            self.assigned_kinds[unit_id] = kinds
            if not kinds:
                self.problem(unit_id, unit['name'], 'missing_relationship',
                             'unit: measures no quantity kind in the library')
            for kind_id in kinds:
                self.kinds[kind_id]['units'].append(unit_id)

        kind_rules: dict[str, list[tuple[str, ...]]] = {
                      k: [()] if kind['units'] else [(g,) for g in kind['general']]
                      for k, kind in self.kinds.items()}

        def inherited(identifier: str, index: int, values: Mapping[str, Any]) -> list[str]:
            kind = self.kinds[identifier]
            return kind['units'] or values[kind_rules[identifier][index][0]]

        borrowed = self.inferred_values(kind_rules, inherited, 'unit inheritance')
        for kind_id, units in borrowed.items():
            self.kinds[kind_id]['units'] = list(units or [])

    def report_unresolved(self) -> None:
        """Diagnose kinds and units the derived view leaves unresolved or inconsistent."""
        for kind_id, kind in self.kinds.items():
            dims = self.kind_dimensions[kind_id]
            if dims is None:
                names = [self.kinds[k]['name'] if k in self.kinds else k
                         for k in self.unresolved_dependencies(kind_id)]
                self.problem(kind_id, kind['name'], 'unresolved_dimensions',
                             'kind: dimensions depend on unresolved ' + ', '.join(names))
                continue
            for unit_id in kind['units']:
                unit_dims = self.unit_dimensions[unit_id]
                if unit_dims is not None and unit_dims != dims:
                    self.problem(unit_id, self.units[unit_id]['name'], 'dimension_mismatch',
                                 f'unit of {kind["name"]!r}: dimensions {_show(unit_dims)} differ from kind {_show(dims)}')
        for unit_id, unit in self.units.items():
            if self.unit_dimensions[unit_id] is None:
                self.problem(unit_id, unit['name'], 'unresolved_dimensions', 'unit: dimensions unresolved')
            elif self.conversions[unit_id] is None:
                self.problem(unit_id, unit['name'], 'unsupported_conversion', 'unit: unresolved conversion')

    def derive(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """The resolved kinds and units, computed once.

        A ConversionError leaves the whole derived view empty, never partially
        published, and becomes the only derivation diagnostic, naming the
        declaration that failed. A CorrectionError propagates.
        """
        if self._derived is not None:
            return self._derived
        try:
            self.load_model()
            self.apply_corrections()
            self.check_factor_names()
            self.assign_units()
            self.resolve_dimensions()
            self.resolve_conversions()
            self.report_unresolved()
        except CorrectionError:
            raise
        except ConversionError as error:
            declaration = self.declarations.get(error.declaration) if error.declaration is not None else None
            subject = (declaration.name if declaration is not None else None) or error.declaration or 'derived analysis'
            self._diagnostics = {Diagnostic(error.declaration, subject, 'derived_analysis', error.reason): None}
            self._derived = ({}, {})
            return self._derived

        def dimensions(dims: Optional[dict[str, Fraction]]) -> Optional[dict[str, Any]]:
            return None if dims is None else {
                key: value.numerator if value.denominator == 1 else str(value)
                for key, value in sorted(dims.items())}

        kinds = {identifier: {
            'dimensions': dimensions(self.kind_dimensions[identifier]),
            'factors': [{'kind': link.target, 'exponent': str(link.exponent)}
                        for link in self.kinds[identifier]['factors']],
            'unresolved_dependencies': self.unresolved_dependencies(identifier),
        } for identifier in sorted(self.kinds)}
        units = {}
        for identifier in sorted(self.units):
            factor = self.si_factors.get(identifier)
            conversion = self.conversions[identifier]
            units[identifier] = {
                'name': self.units[identifier]['name'],
                'symbol': self.units[identifier]['symbol'],
                'quantity_kinds': self.assigned_kinds[identifier],
                'dimensions': dimensions(self.unit_dimensions[identifier]),
                'si_factor': None if factor is None else factor.record(),
                'conversion': None if conversion is None else {
                    'reference_unit': conversion[0],
                    'scale': conversion[1].record(),
                    'offset': conversion[2].record(),
                },
            }
        self._derived = (kinds, units)
        return self._derived

    def catalog(self) -> dict[str, Any]:
        """Source declarations plus separately labelled derived information.

        IDs are scoped to the source SHA-256. Source slots retain their full
        feature URIs; local feature names are conveniences, not global identity.
        Unresolved declarations and unknown expression languages remain data.
        """
        kinds, units = self.derive()
        numbers = {}
        for identifier, declaration in sorted(self.declarations.items()):
            if declaration.cls in NUMBER_CLASSES:
                try:
                    numbers[identifier] = self.constant(identifier).record()
                except ConversionError as error:
                    self.problem(identifier, declaration.name or identifier, 'unsupported_number',
                                 f'number: {error.reason if error.declaration == identifier else error}')
        return {
            'schema_version': 2,
            'source': {'sha256': self.source_hash, 'format': 'OMG-QUDV-XMI'},
            'declarations': {identifier: declaration.record()
                             for identifier, declaration in sorted(self.declarations.items())},
            'resolved': {'kinds': kinds, 'units': units, 'numbers': numbers},
            'applied_corrections': self.applied_corrections,
            'diagnostics': {
                'problems': [d.record() for d in sorted(self._diagnostics, key=lambda d: (d.subject, d.reason))],
            },
        }


def _sum_dims(parts: Iterable[tuple[Optional[dict[str, Fraction]], Fraction]]) -> Optional[dict[str, Fraction]]:
    total: dict[str, Fraction] = defaultdict(Fraction)
    for dims, exponent in parts:
        if dims is None:
            return None
        for symbol, power in dims.items():
            total[symbol] += power * exponent
    return {symbol: power for symbol, power in total.items() if power != 0}


def _show(dims: Optional[dict[str, Fraction]]) -> str:
    if dims is None:
        return 'unresolved'
    return '{' + ', '.join(f'{k}: {v}' for k, v in dims.items()) + '}'


def main() -> int:
    parser = argparse.ArgumentParser(description='ISO-80000 XMI to source-preserving YAML catalog (schema 2).')
    parser.add_argument('xmi', nargs='?', default='ISO-80000.xmi')
    parser.add_argument('-o', '--output', help='write YAML here instead of stdout')
    parser.add_argument('--corrections', help='apply an attributed, source-hash-pinned correction YAML')
    parser.add_argument('--strict', action='store_true',
                        help='exit 1 on resolution gaps, even though they are preserved in the catalog')
    args = parser.parse_args()

    try:
        catalog = ISO80000Converter(args.xmi, corrections_file=args.corrections).catalog()
    except ConversionError as error:
        print(f'conversion failed: {error}', file=sys.stderr)
        return 2
    text = yaml.safe_dump(catalog, sort_keys=False, allow_unicode=True)
    if args.output:
        with open(args.output, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
    else:
        sys.stdout.buffer.write(text.encode('utf-8'))
    for change in (catalog['applied_corrections'] or {}).get('changes', []):
        print(f"  corrected: {change['target']}.{change['field']}: {change['reason']}", file=sys.stderr)
    problems = catalog['diagnostics']['problems']
    for problem in problems:
        print(f"  unresolved in derived view: {problem['subject']}: {problem['reason']}", file=sys.stderr)
    return 1 if args.strict and problems else 0


if __name__ == '__main__':
    sys.exit(main())
