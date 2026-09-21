"""Resolution contracts independent of XMI parsing and physical naming."""

from dependency_resolution import infer_alternatives, resolve_dependencies, unresolved_cycles


def test_required_edges_do_not_use_fallbacks_to_break_cycles():
    graph = {'a': ['b'], 'b': ['a'], 'downstream': ['a'],
             'missing': ['absent'], 'source': [], 'valid': ['source']}

    def evaluate(node, values):
        if node == 'source':
            return 7
        return values.get(graph[node][0])

    values = resolve_dependencies(graph, evaluate)
    assert values == {'a': None, 'b': None, 'downstream': None, 'missing': None,
                      'source': 7, 'valid': 7}
    assert unresolved_cycles(graph, values) == [['a', 'b']]


def test_overlapping_cycles_are_one_component_and_exclude_dependents():
    graph = {'a': ['b'], 'b': ['a', 'c'], 'c': ['b'], 'd': ['c'],
             'self': ['self'], 'missing': ['absent']}
    assert unresolved_cycles(graph, {}) == [['a', 'b', 'c'], ['self']]
    reversed_graph = {node: list(reversed(parents)) for node, parents in reversed(list(graph.items()))}
    assert unresolved_cycles(reversed_graph, {}) == unresolved_cycles(graph, {})


def test_late_preferred_rule_updates_transitive_dependents():
    rules = {'a': [('preferred',), ('fallback',)], 'b': [('a',)],
             'fallback': [()], 'preferred': [('delayed',), ('a',)], 'delayed': [()]}

    def evaluate(node, index, values):
        if node == 'fallback':
            return {'T': 1}
        if node == 'delayed':
            return {'L': 1}
        return values[rules[node][index][0]]

    expected = infer_alternatives(rules, evaluate)
    assert expected['a'] == expected['b'] == expected['preferred'] == {'L': 1}
    assert infer_alternatives(dict(reversed(list(rules.items()))), evaluate) == expected


def test_a_fact_cannot_justify_itself_through_a_derived_result():
    rules = {'a': [('b',), ()], 'b': [('a',)]}

    def evaluate(node, index, values):
        return 1 if not rules[node][index] else 2 * values[rules[node][index][0]]

    # Selecting a's preferred rule would create an unbounded a=2*b, b=2*a
    # feedback loop. Its grounded fallback remains the proof for both facts.
    assert infer_alternatives(rules, evaluate) == {'a': 1, 'b': 2}


def test_equal_value_proof_replacement_can_unblock_an_indirect_preference():
    rules = {'a': [('c',), ()], 'b': [('z',), ('a',)],
             'c': [('b',)], 'root': [()], 'z': [('root',)]}

    def evaluate(node, index, values):
        if not rules[node][index]:
            return 1
        value = values[rules[node][index][0]]
        return value * 10 if node == 'a' else value

    # Initially b and c depend on a's fallback. b later acquires an independent
    # proof of the same value, allowing a's preferred rule without feedback.
    assert infer_alternatives(rules, evaluate) == {'a': 10, 'b': 1, 'c': 1, 'root': 1, 'z': 1}


def test_unanchored_inference_cycle_and_missing_rule_stay_unresolved():
    rules = {'a': [('b',)], 'b': [('a',)], 'missing': [('absent',)],
             'undefined': [], 'dimension_one': [()]}
    values = infer_alternatives(rules, lambda node, index, known: {})
    assert values == {'a': None, 'b': None, 'missing': None, 'undefined': None, 'dimension_one': {}}


def test_deep_inference_does_not_use_recursion():
    rules: dict[str, list[tuple[str, ...]]] = {'n0': [()]}
    rules.update({f'n{i}': [(f'n{i - 1}',)] for i in range(1, 2500)})

    def evaluate(node, index, values):
        return 0 if node == 'n0' else values[rules[node][index][0]] + 1

    assert infer_alternatives(rules, evaluate)['n2499'] == 2499
