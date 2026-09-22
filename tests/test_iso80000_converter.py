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


@pytest.mark.parametrize('body, expected', [
    ('24', 24),
    ('-2', -2),
    ('10^3', 1000),
    ('(2^10)^2', 1048576),
])
def test_integer_constants_are_exact(body, expected):
    assert conv.evaluate_constant(body) == conv.Exact(Fraction(expected))


def test_pi_constants_stay_symbolic():
    factor = conv.multiply(conv.evaluate_constant('Pi/180'), conv.Exact(Fraction(1, 60)))
    assert factor == conv.Exact(Fraction(1, 10800), 1)
    assert factor.record() == {'rational': '1/10800', 'pi_exponent': 1}
    assert factor.as_float() == pytest.approx(math.pi / 10800)


def test_logarithm_constant_is_approximate():
    value = conv.evaluate_constant('ln(10)')
    assert isinstance(value, conv.Approximate)
    assert value.value == pytest.approx(math.log(10))


@pytest.mark.parametrize('body', ['__import__("os")', '1/0', '1 +', '2^Pi'])
def test_unsupported_constant_is_refused(body):
    with pytest.raises(conv.ConversionError):
        conv.evaluate_constant(body)


def test_symbol_text_flattens_html_fragments():
    markup = '<html><head><style>p {margin:0px;}</style></head><body>\n μ\n</body></html>' \
             '<html><body><p>\nA.m<sup>2</sup>\n</p></body></html>'
    assert conv.symbol_text(markup) == 'μA.m^2'
    assert conv.symbol_text(' kg ') == 'kg'


needs_xmi = pytest.mark.skipif(not XMI.exists(), reason=f'{XMI} not present')


def by_name(catalog, section):
    """Resolved entries keyed by unique name: a unit's corrected name, a kind's source name."""
    names = {}
    for identifier, entry in catalog['resolved'][section].items():
        name = entry['name'] if section == 'units' else catalog['declarations'][identifier]['name']
        assert name not in names, name
        names[name] = entry
    return names


@pytest.fixture(scope='module')
def corrected():
    converter = conv.ISO80000Converter(str(XMI), corrections_file=str(ROOT / 'iso80000-corrections.yml'))
    return converter, converter.catalog()


@needs_xmi
@pytest.mark.parametrize('unit, expected', [
    ('kilogram', {'rational': '1', 'pi_exponent': 0}),
    ('gram', {'rational': '1/1000', 'pi_exponent': 0}),
    ('tonne', {'rational': '1000', 'pi_exponent': 0}),
    ('hour', {'rational': '3600', 'pi_exponent': 0}),
    ('day', {'rational': '86400', 'pi_exponent': 0}),
    ('kilonewton', {'rational': '1000', 'pi_exponent': 0}),
    ('volt', {'rational': '1', 'pi_exponent': 0}),
    ('watt', {'rational': '1', 'pi_exponent': 0}),
    ('henry', {'rational': '1', 'pi_exponent': 0}),
    ('second angle', {'rational': '1/648000', 'pi_exponent': 1}),
    ('byte', {'rational': '8', 'pi_exponent': 0}),
    ('litre', {'rational': '1/1000', 'pi_exponent': 0}),
])
def test_known_si_factors(corrected, unit, expected):
    _, catalog = corrected
    assert by_name(catalog, 'units')[unit]['si_factor'] == expected


@needs_xmi
@pytest.mark.parametrize('kind, dimensions', [
    ('voltage', {'M': 1, 'L': 2, 'T': -3, 'I': -1}),
    ('electric field strength', {'M': 1, 'L': 1, 'T': -3, 'I': -1}),
    ('magnetic flux', {'M': 1, 'L': 2, 'T': -2, 'I': -1}),
    ('thermodynamic temperature', {'Θ': 1}),
    ('amount of substance', {'N': 1}),
    ('number of turns in a winding', {}),
    ('initial phase of electric current', {}),
])
def test_known_dimensions(corrected, kind, dimensions):
    _, catalog = corrected
    assert by_name(catalog, 'kinds')[kind]['dimensions'] == dimensions


@needs_xmi
def test_library_contradictions_are_refused_unless_declared():
    converter = conv.ISO80000Converter(str(XMI))
    catalog = converter.catalog()
    assert catalog['resolved']['kinds'] == {} and catalog['resolved']['units'] == {}
    [problem] = catalog['diagnostics']['problems']
    assert problem['category'] == 'derived_analysis'
    assert problem['subject'] == 'electric charge^-1'
    assert 'declare a correction' in problem['reason']


@needs_xmi
def test_real_catalog_retains_affine_source_error(corrected):
    converter, catalog = corrected
    assert yaml.safe_load(yaml.safe_dump(catalog, allow_unicode=True)) == catalog
    nodes = catalog['declarations']
    celsius = next(n for n in nodes.values() if n['class'] == 'AffineConversionUnit')
    offset_id = celsius['slots']['offset'][0]['ref']
    # This is the source's erroneous value, deliberately not a silent repair.
    assert converter.constant(offset_id) == conv.Exact(Fraction(6829, 25))
    assert catalog['resolved']['numbers'][offset_id]['rational'] == '6829/25'
    for node in nodes.values():
        for values in node['slots'].values():
            for value in values:
                if 'ref' in value:
                    assert value['ref'] in nodes


@needs_xmi
def test_reviewed_source_corrections_resolve_all_units(corrected):
    _, catalog = corrected
    kinds = catalog['resolved']['kinds']
    units = catalog['resolved']['units']
    assert len(kinds) == 325
    assert len(units) == 2795
    named_kinds = {catalog['declarations'][k]['name']: k for k in kinds}
    named_units = by_name(catalog, 'units')
    assert kinds[named_kinds['kinematic viscosity']]['dimensions'] == {'L': 2, 'T': -1}
    for unit, kind in [('square metre per second', 'kinematic viscosity'),
                       ('pascal second cubic metre per kilogram', 'kinematic viscosity'),
                       ('weber per metre', 'magnetic vector potential'),
                       ('kelvin to the power minus one', 'linear expansion coefficient'),
                       ('pascal to the power minus one', 'compressibility')]:
        assert named_kinds[kind] in named_units[unit]['quantity_kinds'], unit
    # Prefixed units follow their corrected reference unit's name and symbol.
    kilo = named_units['kilopascal second cubic metre per kilogram']
    assert kilo['symbol'] == 'kPa.s.m^3/kg'
    assert named_units['micropascal second cubic metre per kilogram']['symbol'] == 'μPa.s.m^3/kg'
    assert all(u['conversion'] is not None for u in units.values())
    assert all(u['quantity_kinds'] for u in units.values())
    assert all(u['symbol'] is None or u['symbol'] for u in units.values())
    assert {p['category'] for p in catalog['diagnostics']['problems']} == {'unresolved_dimensions'}
    assert len(catalog['diagnostics']['problems']) == 7
    assert 'generalized coordinate' in {p['subject'] for p in catalog['diagnostics']['problems']}
    celsius_id = next(i for i, n in catalog['declarations'].items() if n['class'] == 'AffineConversionUnit')
    assert units[celsius_id]['conversion']['offset']['rational'] == '5463/20'
    assert units[celsius_id]['si_factor'] is None
    original_offset = catalog['declarations'][celsius_id]['slots']['offset'][0]['ref']
    assert catalog['resolved']['numbers'][original_offset]['rational'] == '6829/25'
