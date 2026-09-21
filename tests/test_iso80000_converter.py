"""Tests for the ISO-80000 XMI converter.

The library-backed tests need the 92 MB OMG model library, which is not checked
in. Put it at the repository root as ISO-80000.xmi, or point ISO80000_XMI at it:
    http://www.omg.org/spec/SysML/20150709/ISO-80000.xmi
"""

import math
import os
import sys
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso80000_converter as conv  # noqa: E402

XMI = Path(os.environ.get('ISO80000_XMI', ROOT / 'ISO-80000.xmi'))


def factor_value(rendered):
    """Numeric value of a rendered factor: a number, 'n/d', 'pi', 'n*pi', 'pi/d' or 'n*pi/d'."""
    if not isinstance(rendered, str):
        return rendered
    head, _, denominator = rendered.partition('/')
    value = Fraction(1, int(denominator)) if denominator else Fraction(1)
    for term in head.split('*'):
        value = value * math.pi if term == 'pi' else value * int(term)
    return value


@pytest.mark.parametrize('body, expected', [
    ('24', 24),
    ('-2', -2),
    ('10^3', 1000),
    ('(2^10)^2', 1048576),
])
def test_integer_constants_are_exact(body, expected):
    factor = conv.evaluate_constant(body)
    assert factor.inexact is None and factor.pi_exp == 0
    assert factor.rational == expected


def test_pi_constants_stay_symbolic():
    factor = conv.evaluate_constant('Pi/180') * conv.Factor(Fraction(1, 60))
    assert factor.to_yaml() == 'pi/10800'
    assert factor_value(factor.to_yaml()) == pytest.approx(math.pi / 10800)


def test_logarithm_constant_is_float():
    assert conv.evaluate_constant('ln(10)').to_yaml() == pytest.approx(math.log(10))


def test_unsupported_constant_is_refused():
    with pytest.raises(conv.ConversionError):
        conv.evaluate_constant('__import__("os")')


@pytest.mark.parametrize('factor, rendered', [
    (conv.Factor(Fraction(1000)), 1000),
    (conv.Factor(Fraction(1, 1000)), '1/1000'),
    (conv.Factor(Fraction(1, 200), pi_exp=1), 'pi/200'),
    (conv.Factor(Fraction(2), pi_exp=1), '2*pi'),
])
def test_factor_rendering(factor, rendered):
    assert factor.to_yaml() == rendered
    assert factor_value(rendered) == pytest.approx(factor.as_float())


def test_symbol_text_flattens_html_fragments():
    markup = '<html><head><style>p {margin:0px;}</style></head><body>\n μ\n</body></html>' \
             '<html><body><p>\nA.m<sup>2</sup>\n</p></body></html>'
    assert conv.symbol_text(markup) == 'μA.m^2'
    assert conv.symbol_text(' kg ') == 'kg'


def test_pascal_case_drops_punctuation():
    assert conv.to_pascal_case('watt per metre kelvin') == 'WattPerMetreKelvin'
    assert conv.to_pascal_case('linked flux (loop m)') == 'LinkedFluxLoopM'


needs_xmi = pytest.mark.skipif(not XMI.exists(), reason=f'{XMI} not present')


@pytest.fixture(scope='module')
def converted():
    converter = conv.ISO80000Converter(str(XMI))
    scalars = converter.convert()
    text = conv.render(scalars, converter.problems, converter.corrections)
    return converter, {s['name']: s for s in scalars}, text


@needs_xmi
def test_every_family_is_complete(converted):
    _, families, _ = converted
    # Celsius is affine; it must not be fabricated by inheriting kelvin units.
    assert len(families) == 316
    assert 'CelsiusTemperature' not in families
    for family in families.values():
        assert family['units'], family['name']
        assert factor_value(family['units'][family['canonical']]['factor']) == 1, family['name']


@needs_xmi
@pytest.mark.parametrize('family, unit, expected', [
    ('Mass', 'Kilogram', 1),
    ('Mass', 'Gram', Fraction(1, 1000)),
    ('Mass', 'Tonne', 1000),
    ('Time', 'Hour', 3600),
    ('Time', 'Day', 86400),
    ('Force', 'Kilonewton', 1000),
    ('ElectricPotential', 'Volt', 1),
    ('Power', 'Watt', 1),
    ('Inductance', 'Henry', 1),
    ('PlaneAngle', 'SecondAngle', math.pi / 648000),
    ('StorageCapacity', 'Byte', 8),
])
def test_known_factors(converted, family, unit, expected):
    _, families, _ = converted
    assert factor_value(families[family]['units'][unit]['factor']) == pytest.approx(expected)


@needs_xmi
@pytest.mark.parametrize('family, dimensions', [
    ('ElectricPotential', {'M': 1, 'L': 2, 'T': -3, 'I': -1}),
    ('ElectricPower', {'M': 1, 'L': 2, 'T': -3}),
    ('MagneticFlux', {'M': 1, 'L': 2, 'T': -2, 'I': -1}),
    ('PlaneAngle', {}),
    ('PhaseDifference', {}),
])
def test_known_dimensions(converted, family, dimensions):
    _, families, _ = converted
    assert families[family]['dimensions'] == dimensions


@needs_xmi
def test_library_contradictions_are_reported_not_hidden(converted):
    converter, families, _ = converted
    corrected = {subject for subject, _ in converter.corrections}
    assert 'electric charge^-1' in corrected
    unresolved = {subject for subject, reason in converter.problems if reason.startswith('kind:')}
    assert 'generalized coordinate' in unresolved
    assert 'GeneralizedCoordinate' not in families


@needs_xmi
def test_rendered_yaml_round_trips(converted):
    _, families, text = converted
    loaded = yaml.safe_load(text)
    assert {s['name'] for s in loaded['scalars']} == set(families)


@needs_xmi
def test_real_catalog_retains_affine_source_error_and_all_kinds(converted):
    converter, _, _ = converted
    catalog = converter.catalog()
    assert len(catalog['resolved']['kinds']) == 325
    assert len(catalog['resolved']['units']) == 2795
    nodes = catalog['declarations']
    celsius = next(n for n in nodes.values() if n['class'] == 'AffineConversionUnit')
    offset_id = celsius['slots']['offset'][0]['ref']
    # This is the source's erroneous value, deliberately not a silent repair.
    assert converter.constant(converter.by_id[offset_id]).to_yaml() == '6829/25'
    assert catalog['resolved']['numbers'][offset_id]['rational'] == '6829/25'
    for node in nodes.values():
        for values in node['slots'].values():
            for value in values:
                if 'ref' in value:
                    assert value['ref'] in nodes
