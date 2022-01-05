import collections
import sys
import torch
from torch.fx.graph import (
    Graph,
    Node,
)
from .quantization_types import Pattern
from .quantization_patterns import (
    QuantizeHandler,
    CustomModuleQuantizeHandler,
    StandaloneModuleQuantizeHandler,
)
from ..qconfig import (
    QConfigAny,
)
from .graph_module import (
    is_observed_standalone_module,
)

from typing import Any, Dict, List, Callable, Optional, Tuple, Set

MatchResult = Tuple[Node, List[Node], Optional[Pattern], QuantizeHandler,
                    QConfigAny]

# Module types in this list are allowed to be used by multiple 'call_module'
# nodes. Module types not in this list will not be matched if they are used by
# multiple 'call_module' nodes.
MODULE_TYPES_SAFE_TO_USE_BY_MULTIPLE_NODES = set([
    torch.nn.ReLU,
])

# TODO: maybe rename this to MatchInputNode
class MatchAllNode:
    """ A node pattern that matches all nodes
    """
    pass

def calculate_module_name_to_num_node_users(
    graph: Graph,
) -> Dict[str, int]:
    """
    Given a dictionary of named modules in a parent module and a graph,
    calculates a dictionary with the key being module name of each child module,
    and the value being the number of nodes in the graph that use that child
    module.

    This is useful for detecting whether a single module instance is used by
    more than one node. This information does not show up in
    `torch.fx.Node.users`, which is why calculate it here.
    """
    results: Dict[str, int] = collections.defaultdict(int)
    for node in graph.nodes:
        if node.op == 'call_module':
            results[node.target] += 1
    return results

# Note: The order of patterns is important! match function will take whatever is matched first, so we'll
# need to put the fusion patterns before single patterns. For example, add_relu should be registered come before relu.
# decorators are applied in the reverse order we see. Also when we match the nodes in the graph with these patterns,
# we'll start from the last node of the graph and traverse back.
def is_match(
    modules: Dict[str, torch.nn.Module],
    node: Node,
    pattern: Pattern,
    module_name_to_num_node_users: Dict[str, int],
    max_uses=sys.maxsize
) -> bool:
    """ Matches a node in fx against a pattern
    """
    if isinstance(pattern, tuple):
        self_match, *arg_matches = pattern
        if self_match is getattr:
            assert len(pattern) == 2, 'Expecting getattr pattern to have two elements'
            arg_matches = []
    else:
        self_match = pattern
        arg_matches = []

    if isinstance(self_match, type) and issubclass(self_match, MatchAllNode):
        return True

    if len(node.users) > max_uses:
        return False

    if (
        # The following two lines are True if the current module has
        # more than one node using it
        node.op == 'call_module' and
        module_name_to_num_node_users[node.target] > 1 and  # type: ignore[index]
        # Exclude module types which should be usable by multiple nodes
        not type(modules[node.target]) in MODULE_TYPES_SAFE_TO_USE_BY_MULTIPLE_NODES  # type: ignore[index]
    ):
        # If a module is used by more than one node, we cannot safely
        # fuse this module without further analysis of the uses. For now,
        # we disable fusion for such modules. A future optimization can
        # enable fusion while handling the multiple uses correctly.
        return False

    if isinstance(self_match, type) and issubclass(self_match, torch.nn.Module):
        if node.op != 'call_module':
            return False
        if not type(modules[node.target]) == self_match:  # type: ignore[index]
            return False
    elif callable(self_match):
        if node.op != 'call_function' or node.target is not self_match:
            return False
        elif node.target is getattr:
            if node.args[1] != pattern[1]:  # type: ignore[index]
                return False
    elif isinstance(self_match, str):
        if node.op != 'call_method' or node.target != self_match:
            return False
    elif node.target != self_match:
        return False

    if not arg_matches:
        return True

    if len(arg_matches) != len(node.args):
        return False

    return all(
        is_match(
            modules, node, arg_match, module_name_to_num_node_users, max_uses=1)  # type: ignore[arg-type]
        for node, arg_match in zip(node.args, arg_matches))

def find_matches(
        graph: Graph,
        modules: Dict[str, torch.nn.Module],
        patterns: Dict[Pattern, QuantizeHandler],
        qconfig_map: Dict[str, QConfigAny],
        standalone_module_names: List[str] = None,
        standalone_module_classes: List[Callable] = None,
        custom_module_classes: List[Any] = None) -> Dict[str, MatchResult]:
    """
    Matches the nodes in the input graph to quantization patterns, and
    outputs the information needed to quantize them in future steps.

    Inputs:
      - graph: an fx.Graph object
      - modules: a mapping of fully qualified module name to instance,
          for example, {'foo': ModuleFoo, ...}
      - patterns: a mapping from a tuple of nodes in reverse order to
          uninitialized QuantizeHandler subclass.

    Outputs a map of
      node_name ->
        (node, matched_values, matched_pattern, QuantizeHandler instance,
         qconfig)

    For example, {
      'relu_1': (relu_1, [relu_1], torch.nn.functional.relu,
                 <CopyNodeQuantizeHandler instance>, QConfig(...)),
      ...
    }
    """
    if custom_module_classes is None:
        custom_module_classes = []

    if standalone_module_classes is None:
        standalone_module_classes = []

    if standalone_module_names is None:
        standalone_module_names = []

    match_map: Dict[str, MatchResult] = {}
    all_matched : Set[str] = set()

    def record_match(pattern, node, matched):
        if isinstance(pattern, tuple):
            s, *args = pattern
            record_match(s, node, matched)
            if pattern[0] is not getattr:
                for subpattern, arg in zip(args, node.args):
                    record_match(subpattern, arg, matched)
        else:
            matched.append(node)

    module_name_to_num_node_users = \
        calculate_module_name_to_num_node_users(graph)
    cache_for_no_tensor_check: Dict[Node, bool] = dict()
    for node in reversed(graph.nodes):
        if node.name not in match_map and node.name not in all_matched:
            for pattern, value in patterns.items():
                if is_match(
                        modules, node, pattern, module_name_to_num_node_users):
                    matched: List[Any] = []
                    record_match(pattern, node, matched)
                    for n in matched:
                        match_map[n.name] = (
                            node, matched, pattern, value(node, modules),  # type: ignore[operator]
                            qconfig_map[n.name])
                        all_matched.add(n.name)
                    # break after finding the first match
                    break

    # add custom module instances to the match result
    assert modules is not None
    for node in graph.nodes:
        if node.op == 'call_module' and \
           type(modules[node.target]) in custom_module_classes:
            custom_module_qconfig = qconfig_map[node.name]
            match_map[node.name] = (
                node, [node], None, CustomModuleQuantizeHandler(node, modules),
                custom_module_qconfig)

    def is_standalone_module(node_target: str, modules: Dict[str, torch.nn.Module]):
        assert modules is not None
        return (
            node_target in standalone_module_names or  # type: ignore[operator]
            type(modules[node_target]) in standalone_module_classes  # type: ignore[operator]
        )

    # add standalone modules to the match
    for node in graph.nodes:
        if node.op == 'call_module' and \
           (is_standalone_module(node.target, modules) or
                is_observed_standalone_module(modules[node.target])):
            # add node to matched nodes
            custom_module_qconfig = qconfig_map[node.name]
            match_map[node.name] = (
                node, [node], None,
                StandaloneModuleQuantizeHandler(node, modules),
                custom_module_qconfig)

    return match_map
