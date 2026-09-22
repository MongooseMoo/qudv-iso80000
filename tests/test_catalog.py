"""Small, always-running XMI fixtures for the source-preserving catalog."""
import hashlib
import subprocess
import sys
from fractions import Fraction
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
    instance('isq_temperature', 'baseQuantityKind', 'ISQ base thermodynamic temperature',
             baseQuantityKind=['temperature'])
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
    assert catalog['schema_version'] == 2
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


def test_affine_and_nonlinear_units_have_no_multiplicative_factor(tmp_path):
    result = conv.ISO80000Converter(str(model(tmp_path))).catalog()
    units = result['resolved']['units']
    assert units['kelvin']['si_factor'] == {'rational': '1', 'pi_exponent': 0}
    assert units['kelvin']['symbol'] == 'K'
    assert units['celsius']['symbol'] is None
    for identifier in ['celsius', 'mcelsius', 'general']:
        assert units[identifier]['si_factor'] is None
    assert units['general']['conversion'] is None
    assert any(p['id'] == 'general' and p['category'] == 'unsupported_conversion'
               for p in result['diagnostics']['problems'])


def test_catalog_cli_and_strict_diagnostics(tmp_path):
    path = model(tmp_path)
    result = subprocess.run([
        sys.executable, str(Path(conv.__file__)), str(path), '--strict',
    ], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 1
    assert yaml.safe_load(result.stdout)['declarations']['celsius']['slots']['offset'] == [{'ref': 'offset'}]


def test_duplicate_ids_are_rejected(tmp_path):
    path = tmp_path / 'duplicate.xmi'
    path.write_text(f'<model xmlns:xmi="{conv.XMI_NS}"><a xmi:id="x"/><b xmi:id="x"/></model>')
    with pytest.raises(conv.ConversionError, match='duplicate XMI id'):
        conv.ISO80000Converter(str(path))


def test_decimal_literal_does_not_round_through_binary_float():
    assert conv.evaluate_constant('0.123456789012345678901').record() == {
        'rational': '123456789012345678901/1000000000000000000000', 'pi_exponent': 0,
    }


def test_exact_catalog_numbers_preserve_pi_powers():
    assert conv.evaluate_constant('(Pi/180)^2').record() == {
        'rational': '1/32400', 'pi_exponent': 2,
    }
    assert conv.evaluate_constant('ln(10)').record() == {'approximate': pytest.approx(2.302585092994046)}


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
                      if (feature := s.find('definingFeature')) is not None
                      and feature.get('href', '').endswith('.expression'))
    value = expression.find('value')
    assert value is not None
    value.set('value', '  opaque expression\n')
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
    body = node.find('specification/body')
    assert body is not None
    body.text = 'Real(sin(1))'
    tree.write(str(path), encoding='utf-8')
    result = conv.ISO80000Converter(str(path)).catalog()
    assert 'prefix' in result['declarations']
    assert result['resolved']['units'] == {}
    # The failure names the declaration whose content could not be interpreted.
    assert any(p['category'] == 'derived_analysis' and p['id'] == 'milli' and p['subject'] == 'milli'
               for p in result['diagnostics']['problems'])


def add_instance(path, identifier, cls, name, **slots):
    tree = etree.parse(str(path))
    node = etree.SubElement(tree.getroot(), 'packagedElement', {
        conv.XMI_ID: identifier, conv.XMI_TYPE: 'uml:InstanceSpecification', 'name': name})
    etree.SubElement(node, 'classifier', href='qudv#_' + cls)
    for feature, values in slots.items():
        slot = etree.SubElement(node, 'slot')
        etree.SubElement(slot, 'definingFeature', href='qudv#_' + cls + '.' + feature)
        for value in values:
            if isinstance(value, tuple):
                etree.SubElement(slot, 'value', {conv.XMI_TYPE: 'uml:LiteralString', 'value': value[0]})
            else:
                v = etree.SubElement(slot, 'value')
                etree.SubElement(v, 'instance', {conv.XMI_IDREF: value})
    tree.write(str(path), encoding='utf-8')


def number(path, identifier, body):
    add_instance(path, identifier, 'Real', identifier)
    tree = etree.parse(str(path))
    node = next(n for n in tree.getroot() if n.get(conv.XMI_ID) == identifier)
    etree.SubElement(etree.SubElement(node, 'specification'), 'body').text = f'Real({body})'
    tree.write(str(path), encoding='utf-8')


def test_affine_composition_and_temperature_differences(tmp_path):
    path = model(tmp_path)
    number(path, 'five_ninths', '5/9')
    number(path, 'fahrenheit_offset', '-160/9')
    add_instance(path, 'fahrenheit', 'AffineConversionUnit', 'Fahrenheit',
                 referenceUnit=['celsius'], factor=['five_ninths'], offset=['fahrenheit_offset'])
    result = conv.ISO80000Converter(str(path)).catalog()
    units = result['resolved']['units']
    transform = units['fahrenheit']['conversion']
    assert transform['reference_unit'] == 'kelvin'
    assert transform['scale'] == {'rational': '5/9', 'pi_exponent': 0}
    assert transform['offset'] == {'rational': '45967/180', 'pi_exponent': 0}
    # Absolute freezing point and boiling-to-freezing interval use different equations.
    scale = Fraction(transform['scale']['rational'])
    offset = Fraction(transform['offset']['rational'])
    assert scale * 32 + offset == Fraction('273.15')
    assert scale * (212 - 32) == 100
    assert units['mcelsius']['conversion']['scale']['rational'] == '1/1000'
    assert units['mcelsius']['conversion']['offset']['rational'] == '5463/20'
    assert units['general']['conversion'] is None
    assert units['fahrenheit']['si_factor'] is None
    assert not any(p['subject'] in {'degree Celsius', 'millidegree Celsius', 'Fahrenheit'}
                   for p in result['diagnostics']['problems'])


def test_entity_count_and_dependency_diagnostics(tmp_path):
    path = model(tmp_path)
    add_instance(path, 'count', 'SimpleQuantityKind', 'count', isNumberOfEntities=[('true',)])
    add_instance(path, 'turn', 'SimpleUnit', 'turn', quantityKind=['count'])
    add_instance(path, 'amount', 'SimpleQuantityKind', 'amount of substance', isNumberOfEntities=[('true',)])
    add_instance(path, 'isq_amount', 'baseQuantityKind', 'ISQ base amount', baseQuantityKind=['amount'])
    add_instance(path, 'unknown_factor', 'QuantityKindFactor', 'unknown^1',
                 quantityKind=['unknown'], exponent=['one'])
    add_instance(path, 'dependent', 'DerivedQuantityKind', 'dependent', factor=['unknown_factor'])
    add_instance(path, 'lost_unit', 'SimpleUnit', 'lost unit', quantityKind=['dependent'])
    # A base quantity flagged as an entity count states two dimensions; that is
    # refused until a correction declares which statement is wrong.
    refused = conv.ISO80000Converter(str(path)).catalog()
    assert refused['resolved']['kinds'] == {}
    assert [(p['id'], p['category']) for p in refused['diagnostics']['problems']] == [('amount', 'derived_analysis')]
    corrections = correction_file(tmp_path, path)
    document = yaml.safe_load(corrections.read_text())
    document['changes'] = [dict(target='amount', field='entity_count', expected=True, value=False,
                                reason='Fixture correction', citation='urn:test')]
    corrections.write_text(yaml.safe_dump(document))
    result = conv.ISO80000Converter(str(path), corrections_file=str(corrections)).catalog()
    kinds = result['resolved']['kinds']
    assert kinds['count']['dimensions'] == {}
    assert kinds['amount']['dimensions'] == {'N': 1}
    assert kinds['dependent']['unresolved_dependencies'] == ['unknown']
    assert kinds['dependent']['factors'] == [{'kind': 'unknown', 'exponent': '1'}]
    problems = result['diagnostics']['problems']
    assert any(p['subject'] == 'dependent' and 'unresolved quantity' in p['reason'] for p in problems)
    assert any(p['subject'] == 'lost unit' for p in problems)


def correction_file(tmp_path, path, **change):
    manifest = {'schema_version': 1, 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'changes': [dict(target='celsius', field='offset', expected='5463/20',
                                 value='273', reason='Fixture correction', citation='urn:test', **change)]}
    target = tmp_path / 'corrections.yml'
    target.write_text(yaml.safe_dump(manifest), encoding='utf-8')
    return target


def test_corrections_preserve_source_and_record_provenance(tmp_path):
    path = model(tmp_path)
    corrections = correction_file(tmp_path, path)
    converter = conv.ISO80000Converter(str(path), corrections_file=str(corrections))
    result = converter.catalog()
    assert result['resolved']['units']['mcelsius']['conversion']['offset']['rational'] == '273'
    assert result['resolved']['numbers']['offset']['rational'] == '5463/20'
    assert result['declarations']['celsius']['slots']['offset'] == [{'ref': 'offset'}]
    assert result['applied_corrections']['changes'][0]['citation'] == 'urn:test'
    assert result == converter.catalog()


def test_prefix_correction_rescales_the_unit_without_touching_the_source_prefix(tmp_path):
    path = model(tmp_path)
    corrections = correction_file(tmp_path, path)
    document = yaml.safe_load(corrections.read_text())
    document['changes'] = [dict(target='mcelsius', field='scale', expected='1/1000', value='1/100',
                                reason='Fixture correction', citation='urn:test')]
    corrections.write_text(yaml.safe_dump(document))
    result = conv.ISO80000Converter(str(path), corrections_file=str(corrections)).catalog()
    assert result['resolved']['units']['mcelsius']['conversion']['scale']['rational'] == '1/100'
    assert result['resolved']['numbers']['milli']['rational'] == '1/1000'
    assert result['declarations']['mcelsius']['slots']['prefix'] == [{'ref': 'prefix'}]


@pytest.mark.parametrize('field,value', [('source_sha256', 'bad'), ('expected', '99'), ('target', 'absent')])
def test_stale_corrections_fail_closed(tmp_path, field, value):
    path = model(tmp_path)
    corrections = correction_file(tmp_path, path)
    document = yaml.safe_load(corrections.read_text())
    (document if field == 'source_sha256' else document['changes'][0])[field] = value
    corrections.write_text(yaml.safe_dump(document))
    with pytest.raises(conv.ConversionError, match='correction'):
        conv.ISO80000Converter(str(path), corrections_file=str(corrections)).catalog()


def test_topological_resolution_is_order_independent_and_reports_cycles(tmp_path):
    path = model(tmp_path)
    add_instance(path, 'a', 'LinearConversionUnit', 'cycle a', referenceUnit=['b'], factor=['one'])
    add_instance(path, 'b', 'LinearConversionUnit', 'cycle b', referenceUnit=['a'], factor=['one'])
    # A reference to a declaration that is not a unit is missing from the unit graph.
    add_instance(path, 'missing', 'LinearConversionUnit', 'missing reference',
                 referenceUnit=['unknown'], factor=['one'])
    first = conv.ISO80000Converter(str(path)).catalog()
    assert first['resolved']['units']['a']['conversion'] is None
    assert any(p['category'] == 'dependency_cycle' for p in first['diagnostics']['problems'])
    assert any(p['category'] == 'missing_reference' for p in first['diagnostics']['problems'])
    tree = etree.parse(str(path))
    root = tree.getroot()
    root[:] = list(reversed(root[:]))
    tree.write(str(path), encoding='utf-8')
    second = conv.ISO80000Converter(str(path)).catalog()
    assert first['resolved'] == second['resolved']
    assert first['diagnostics'] == second['diagnostics']


def test_deep_conversion_chain_does_not_use_python_recursion(tmp_path):
    path = model(tmp_path)
    tree = etree.parse(str(path))
    parent = 'celsius'
    for i in range(1100):
        identifier = f'chain_{i}'
        node = etree.SubElement(tree.getroot(), 'packagedElement', {
            conv.XMI_ID: identifier, conv.XMI_TYPE: 'uml:InstanceSpecification', 'name': identifier})
        etree.SubElement(node, 'classifier', href='qudv#_LinearConversionUnit')
        for feature, target in [('factor', 'one'), ('referenceUnit', parent)]:
            slot = etree.SubElement(node, 'slot')
            etree.SubElement(slot, 'definingFeature', href='qudv#_LinearConversionUnit.' + feature)
            etree.SubElement(etree.SubElement(slot, 'value'), 'instance', {conv.XMI_IDREF: target})
        parent = identifier
    tree.write(str(path), encoding='utf-8')
    result = conv.ISO80000Converter(str(path)).catalog()
    assert result['resolved']['units'][parent]['conversion'] == result['resolved']['units']['celsius']['conversion']


def test_mixed_pi_offset_stays_exact_and_zero_scale_is_unresolved(tmp_path):
    path = model(tmp_path)
    number(path, 'pi', 'Pi')
    number(path, 'zero', '0')
    add_instance(path, 'shifted', 'AffineConversionUnit', 'shifted',
                 referenceUnit=['celsius'], factor=['one'], offset=['pi'])
    add_instance(path, 'singular', 'AffineConversionUnit', 'singular',
                 referenceUnit=['celsius'], factor=['zero'], offset=['one'])
    result = conv.ISO80000Converter(str(path)).catalog()
    assert result['resolved']['units']['shifted']['conversion']['offset'] == {'sum': [
        {'rational': '5463/20', 'pi_exponent': 0}, {'rational': '1', 'pi_exponent': 1}]}
    assert result['resolved']['units']['singular']['conversion'] is None
    assert any(p['subject'] == 'singular' and p['category'] == 'unsupported_conversion'
               for p in result['diagnostics']['problems'])


def test_catalog_strict_accepts_supported_affine_projection(tmp_path):
    path = model(tmp_path)
    tree = etree.parse(str(path))
    for node in list(tree.getroot()):
        if node.get(conv.XMI_ID) in {'unknown', 'general'}:
            tree.getroot().remove(node)
    tree.write(str(path), encoding='utf-8')
    corrections = correction_file(tmp_path, path)
    command = [sys.executable, str(Path(conv.__file__)), str(path), '--strict', '--corrections', str(corrections)]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    assert yaml.safe_load(result.stdout)['resolved']['units']['celsius']['conversion']['offset']['rational'] == '273'


def test_correction_batch_is_atomic_and_cli_keeps_existing_output(tmp_path):
    path = model(tmp_path)
    corrections = correction_file(tmp_path, path)
    document = yaml.safe_load(corrections.read_text())
    document['changes'].append({**document['changes'][0], 'target': 'absent'})
    corrections.write_text(yaml.safe_dump(document))
    converter = conv.ISO80000Converter(str(path), corrections_file=str(corrections))
    converter.load_model()
    with pytest.raises(conv.ConversionError):
        converter.apply_corrections()
    assert converter.units['celsius']['offset'] == conv.Exact(Fraction(5463, 20))
    assert converter.applied_corrections is None
    output = tmp_path / 'existing.yml'
    output.write_text('keep this')
    result = subprocess.run([sys.executable, str(Path(conv.__file__)), str(path),
                             '--corrections', str(corrections), '-o', str(output)],
                            capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 2
    assert 'correction' in result.stderr
    assert output.read_text() == 'keep this'


def test_preferred_evidence_replaces_a_provisional_fallback(tmp_path):
    path = model(tmp_path)
    add_instance(path, 'length', 'SimpleQuantityKind', 'length')
    add_instance(path, 'isq_length', 'baseQuantityKind', 'ISQ base length', baseQuantityKind=['length'])
    add_instance(path, 'metre', 'SimpleUnit', 'metre', quantityKind=['length'])
    # The lower-priority general links form a cycle. The preferred definition
    # is independently grounded in length and must win for both kinds.
    add_instance(path, 'preferred', 'SimpleQuantityKind', 'preferred', general=['length', 'target'])
    add_instance(path, 'target', 'SimpleQuantityKind', 'target', general=['preferred', 'temperature'])
    converter = conv.ISO80000Converter(str(path))
    result = converter.catalog()
    for identifier in ['preferred', 'target']:
        assert result['resolved']['kinds'][identifier]['dimensions'] == {'L': 1}
        assert converter.kinds[identifier]['units'] == ['metre']
    assert not any(p['category'] == 'dependency_cycle' and p['id'] in {'preferred', 'target'}
                   for p in result['diagnostics']['problems'])
    tree = etree.parse(str(path))
    tree.getroot()[:] = list(reversed(tree.getroot()[:]))
    tree.write(str(path), encoding='utf-8')
    reordered = conv.ISO80000Converter(str(path)).catalog()
    assert result['resolved'] == reordered['resolved']
    assert result['diagnostics'] == reordered['diagnostics']


def test_dangling_reference_is_an_error(tmp_path):
    path = model(tmp_path)
    add_instance(path, 'dangling', 'LinearConversionUnit', 'dangling', referenceUnit=['absent'], factor=['one'])
    with pytest.raises(conv.ConversionError, match="refers to 'absent'") as error:
        conv.ISO80000Converter(str(path))
    assert error.value.declaration == 'dangling'


def test_factor_name_disagreement_is_refused_unless_declared(tmp_path):
    path = model(tmp_path)
    add_instance(path, 'length', 'SimpleQuantityKind', 'length')
    add_instance(path, 'isq_length', 'baseQuantityKind', 'ISQ base length', baseQuantityKind=['length'])
    add_instance(path, 'per_length', 'QuantityKindFactor', 'length^-1', quantityKind=['length'], exponent=['one'])
    add_instance(path, 'repetency', 'DerivedQuantityKind', 'repetency', factor=['per_length'])
    refused = conv.ISO80000Converter(str(path)).catalog()
    assert [(p['id'], p['category']) for p in refused['diagnostics']['problems']] == [
        ('per_length', 'derived_analysis')]
    corrections = correction_file(tmp_path, path)
    document = yaml.safe_load(corrections.read_text())
    document['changes'] = [dict(target='repetency', field='factors', expected=[['length', '1']],
                                value=[['length', '-1']], reason='Fixture correction', citation='urn:test')]
    corrections.write_text(yaml.safe_dump(document))
    result = conv.ISO80000Converter(str(path), corrections_file=str(corrections)).catalog()
    assert result['resolved']['kinds']['repetency']['dimensions'] == {'L': -1}
    assert result['declarations']['per_length']['slots']['exponent'] == [{'ref': 'one'}]


def test_prefixed_units_follow_a_corrected_reference_name(tmp_path):
    path = model(tmp_path)
    corrections = correction_file(tmp_path, path)
    document = yaml.safe_load(corrections.read_text())
    document['changes'] = [dict(target='celsius', field='name', expected='degree Celsius', value='degree celsius',
                                reason='Fixture correction', citation='urn:test')]
    corrections.write_text(yaml.safe_dump(document))
    result = conv.ISO80000Converter(str(path), corrections_file=str(corrections)).catalog()
    assert result['resolved']['units']['mcelsius']['name'] == 'millidegree celsius'
    assert result['declarations']['mcelsius']['name'] == 'millidegree Celsius'
    # A prefixed name that is not prefix + reference is an undeclared disagreement.
    tree = etree.parse(str(path))
    node = next(n for n in tree.getroot() if n.get(conv.XMI_ID) == 'mcelsius')
    node.set('name', 'thousandth Celsius')
    tree.write(str(path), encoding='utf-8')
    document['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    corrections.write_text(yaml.safe_dump(document))
    with pytest.raises(conv.CorrectionError, match='declare its correction'):
        conv.ISO80000Converter(str(path), corrections_file=str(corrections)).catalog()
