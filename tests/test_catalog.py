"""Small, always-running XMI fixtures for the source-preserving catalog."""
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from lxml import etree

import iso80000_converter as conv


def model(tmp_path):
    root = etree.Element('model', nsmap={'xmi': conv.XMI_NS})

    def instance(identifier, cls, name, **slots):
        node = etree.SubElement(root, 'packagedElement', {
            conv.XMI_ID: identifier, conv.XMI_TYPE: 'uml:InstanceSpecification',
            'name': name,
        })
        etree.SubElement(node, 'classifier', href='qudv#_' + cls)
        for feature, values in slots.items():
            slot = etree.SubElement(node, 'slot')
            etree.SubElement(slot, 'definingFeature', href='qudv#_' + cls + '.' + feature)
            for value in values:
                if isinstance(value, tuple):
                    etree.SubElement(slot, 'value', {
                        conv.XMI_TYPE: 'uml:LiteralString', 'value': value[0],
                    })
                else:
                    v = etree.SubElement(slot, 'value', {conv.XMI_TYPE: 'uml:InstanceValue'})
                    etree.SubElement(v, 'instance', {conv.XMI_IDREF: value})
        return node

    for identifier, body in [('one', '1'), ('offset', '273.15'), ('milli', '1/1000')]:
        node = instance(identifier, 'Real', identifier)
        spec = etree.SubElement(node, 'specification')
        etree.SubElement(spec, 'body').text = 'Real(' + body + ')'
    instance('temperature', 'SimpleQuantityKind', 'thermodynamic temperature')
    instance('celsius_kind', 'SimpleQuantityKind', 'Celsius temperature', general=['temperature'])
    instance('unknown', 'SimpleQuantityKind', 'unresolved quantity')
    instance('kelvin', 'SimpleUnit', 'kelvin', quantityKind=['temperature'], symbol=[('K',)])
    instance('celsius', 'AffineConversionUnit', 'degree Celsius',
             quantityKind=['celsius_kind'], referenceUnit=['kelvin'], factor=['one'], offset=['offset'])
    instance('prefix', 'Prefix', 'milli', factor=['milli'], symbol=[('m',)])
    instance('mcelsius', 'PrefixedUnit', 'millidegree Celsius', referenceUnit=['celsius'], prefix=['prefix'])
    instance('general', 'GeneralConversionUnit', 'nonlinear temperature',
             quantityKind=['temperature'], referenceUnit=['kelvin'],
             expression=[('log(x)',)], expressionLanguageURI=[('urn:example:math',)])
    path = tmp_path / 'fixture.xmi'
    etree.ElementTree(root).write(str(path), encoding='utf-8')
    return path


def test_catalog_preserves_definitions_and_unresolved_nodes(tmp_path):
    path = model(tmp_path)
    converter = conv.ISO80000Converter(str(path))
    catalog = converter.catalog()
    assert catalog['schema_version'] == 1
    assert catalog['source']['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    nodes = catalog['declarations']
    assert nodes['celsius']['class'] == 'AffineConversionUnit'
    assert nodes['celsius']['slots']['offset'][0] == {'ref': 'offset'}
    assert nodes['celsius_kind']['slots']['general'] == [{'ref': 'temperature'}]
    assert nodes['prefix']['slots']['factor'] == [{'ref': 'milli'}]
    assert nodes['general']['slots']['expression'][0]['value'] == 'log(x)'
    assert nodes['offset']['specification']['children'][0]['text'] == 'Real(273.15)'
    assert catalog['resolved']['numbers']['offset'] == {'rational': '5463/20', 'pi_exponent': 0}
    assert 'unknown' in nodes
    assert catalog['resolved']['kinds']['unknown']['dimensions'] is None
    assert catalog['resolved']['kinds']['temperature']['dimensions'] == {'Θ': 1}
    assert catalog['resolved']['units']['celsius']['si_factor'] is None
    assert catalog['diagnostics']['problems']
    assert yaml.safe_load(yaml.safe_dump(catalog, allow_unicode=True)) == catalog
    assert converter.catalog() == catalog


def test_affine_and_nonlinear_units_never_become_scalar_factors(tmp_path):
    converter = conv.ISO80000Converter(str(model(tmp_path)))
    scalars = converter.convert()
    assert 'celsius' in converter.units
    assert 'general' in converter.units
    for family in scalars:
        assert not {'DegreeCelsius', 'MillidegreeCelsius', 'NonlinearTemperature'} & family['units'].keys()
    assert any('non-multiplicative' in reason for _, reason in converter.problems)


def test_catalog_cli_and_strict_diagnostics(tmp_path):
    path = model(tmp_path)
    result = subprocess.run([
        sys.executable, str(Path(conv.__file__)), str(path), '--format', 'catalog', '--strict',
    ], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 1
    assert yaml.safe_load(result.stdout)['declarations']['celsius']['slots']['offset'] == [{'ref': 'offset'}]


def test_duplicate_ids_are_rejected(tmp_path):
    path = tmp_path / 'duplicate.xmi'
    path.write_text(f'<model xmlns:xmi="{conv.XMI_NS}"><a xmi:id="x"/><b xmi:id="x"/></model>')
    with pytest.raises(conv.ConversionError, match='duplicate XMI id'):
        conv.ISO80000Converter(str(path))


def test_decimal_literal_does_not_round_through_binary_float():
    assert conv.evaluate_constant('0.123456789012345678901').to_yaml() == '123456789012345678901/1000000000000000000000'


def test_exact_catalog_numbers_preserve_pi_powers():
    assert conv.evaluate_constant('(Pi/180)^2').exact_record() == {
        'rational': '1/32400', 'pi_exponent': 2,
    }
    assert conv.evaluate_constant('ln(10)').exact_record() == {'approximate': pytest.approx(2.302585092994046)}


def test_catalog_preserves_unknown_number_expression(tmp_path):
    path = model(tmp_path)
    tree = etree.parse(str(path))
    # An unused constant must remain available even when we cannot evaluate it.
    node = etree.SubElement(tree.getroot(), 'packagedElement', {
        conv.XMI_ID: 'opaque_number', conv.XMI_TYPE: 'uml:InstanceSpecification',
        'name': 'opaque number',
    })
    etree.SubElement(node, 'classifier', href='qudv#_Real')
    spec = etree.SubElement(node, 'specification')
    etree.SubElement(spec, 'body').text = 'Real(sin(1))'
    tree.write(str(path), encoding='utf-8')
    converter = conv.ISO80000Converter(str(path))
    result = converter.catalog()
    assert result['declarations']['opaque_number']['specification']['children'][0]['text'] == 'Real(sin(1))'
    assert 'opaque_number' not in result['resolved']['numbers']
    assert any(p['subject'] == 'opaque number' for p in result['diagnostics']['problems'])
    assert converter.catalog() == result


def test_source_slot_text_and_duplicate_features(tmp_path):
    path = model(tmp_path)
    tree = etree.parse(str(path))
    node = next(n for n in tree.getroot() if n.get(conv.XMI_ID) == 'general')
    expression = next(s for s in node.findall('slot')
                      if s.find('definingFeature').get('href').endswith('.expression'))
    expression.find('value').set('value', '  opaque expression\n')
    tree.write(str(path), encoding='utf-8')
    result = conv.ISO80000Converter(str(path)).catalog()
    assert result['declarations']['general']['slots']['expression'][0]['value'] == '  opaque expression\n'
    duplicate = etree.SubElement(node, 'slot')
    etree.SubElement(duplicate, 'definingFeature', href='another#Type.expression')
    tree.write(str(path), encoding='utf-8')
    with pytest.raises(conv.ConversionError, match='duplicate slot name'):
        conv.ISO80000Converter(str(path)).catalog()


def test_uninterpretable_used_constant_does_not_block_source_export(tmp_path):
    path = model(tmp_path)
    tree = etree.parse(str(path))
    node = next(n for n in tree.getroot() if n.get(conv.XMI_ID) == 'milli')
    node.find('specification/body').text = 'Real(sin(1))'
    tree.write(str(path), encoding='utf-8')
    result = conv.ISO80000Converter(str(path)).catalog()
    assert 'prefix' in result['declarations']
    assert result['resolved']['units'] == {}
    assert any(p['subject'] == 'derived analysis' for p in result['diagnostics']['problems'])
