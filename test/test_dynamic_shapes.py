# -*- coding: utf-8 -*-
# Owner(s): ["oncall: jit"]

from torch._C import _disabled_torch_function_impl
import torch.fx
import torch.nn.functional as F
from torch.testing._internal.common_utils import run_tests, TestCase
import unittest
import torch
from torch.utils._pytree import tree_map
aten = torch.ops.aten

try:
    import sympy
    HAS_SYMPY = True
except ImportError:
    HAS_SYMPY = False
skipIfNoSympy = unittest.skipIf(not HAS_SYMPY, "no sympy")


meta_funcs = {}


def register_meta(op):
    def decorator(f):
        def add_func(op):
            meta_funcs[op] = f
        tree_map(add_func, op)
        return f
    return decorator


@register_meta([aten.add.Tensor, aten.sub.Tensor])
def binary_meta(a, b):
    return a.new_empty(a.sym_size())


@register_meta(aten.cat.default)
def cat_meta(tensors, dim=0):
    concat_length = 0
    shape = tensors[0].shape
    for tensor in tensors:
        for idx, (common_length, length) in enumerate(zip(shape, tensor.shape)):
            if idx == dim:
                concat_length = concat_length + length
            else:
                assert length == common_length
    new_shape = list(shape)
    new_shape[dim] = concat_length
    return tensors[0].new_empty(new_shape)


@register_meta([aten.narrow_copy.SymInt])
def narrow_copy_symint_meta(a, dim, start, length, **kwargs):
    shape = []
    for i, x in enumerate(a.sym_size()):
        if i == dim:
            shape.append(length)
        else:
            shape.append(x)
    return a.new_empty(tuple(shape))


@register_meta([aten.expand.SymInt])
def expand_symint_meta(a, size, implicit=False):
    return a.new_empty(size)


class PySymInt(object):
    def __init__(self, expr, shape_env):
        self.expr = expr
        self.shape_env = shape_env

    def wrap(self, num):
        return PySymInt(sympy.Integer(num), self.shape_env)

    def __str__(self):
        return f"PySymInt({self.expr})"

    def __int__(self):
        return self.shape_env.evaluate_expr(self.expr)

    def __bool__(self):
        return bool(self.shape_env.evaluate_expr(self.expr))


magic_methods = {
    'add': lambda a, b: a + b,
    'radd': lambda a, b: a + b,
    'sub': lambda a, b: a - b,
    'mul': lambda a, b: a * b,
    'div': lambda a, b: a / b,
    'mod': lambda a, b: a % b,
    'eq': lambda a, b: sympy.Eq(a, b),
    'gt': lambda a, b: sympy.Gt(a, b),
    'lt': lambda a, b: sympy.Lt(a, b),
}

for method, func in magic_methods.items():
    method_name = f'{method}'

    def create_magic_impl(func):
        def magic_impl(self, other):
            if isinstance(other, PySymInt):
                other = other.expr
            return PySymInt(func(self.expr, other), self.shape_env)
        return magic_impl

    # this should be wrapped transparently into torch._C.SymbolicIntNode
    setattr(PySymInt, method_name, create_magic_impl(func))


class ShapeEnv(object):
    def __init__(self):
        self.guards = []
        self.shape_env = {}

    def create_symint(self, name, val):
        sympy_expr = sympy.Symbol(name)
        py_sym_int = PySymInt(sympy_expr, self)
        cpp_sym_int = torch._C.SymbolicIntNode.new_symint(py_sym_int)
        self.shape_env[sympy_expr] = val
        return cpp_sym_int

    def evaluate_expr(self, expr):
        concrete_val = expr.subs(self.shape_env)
        self.guards.append((expr, concrete_val))
        return concrete_val


def create_contiguous(shape):
    strides = [1]
    for dim in reversed(shape[:-1]):
        strides.append(dim * strides[-1])
    return list(reversed(strides))


class FakeSymbolicTensor(torch.Tensor):
    @staticmethod
    def __new__(cls, sym_shape, sym_strides, dtype, layout, requires_grad, device):
        # sym_strides doesn't work yet
        # TODO: this is wrong in general
        offset = 0
        r = torch.Tensor._make_wrapper_subclass(
            cls, sym_shape,
            create_contiguous(sym_shape), offset,
            dtype=dtype, layout=layout, requires_grad=requires_grad,
            device=device,
        )

        r.sym_shape = sym_shape
        return r

    __torch_function__ = _disabled_torch_function_impl

    def new_empty(self, shape):
        return FakeSymbolicTensor(shape, None, self.dtype, self.layout, self.requires_grad, self.device)

    @classmethod
    def __torch_dispatch__(cls, func_overload, types, args=(), kwargs=None):
        if func_overload in meta_funcs:
            return meta_funcs[func_overload](*args, **kwargs)

        if func_overload == torch.ops.aten.sym_size.default:
            self = args[0]
            return self.sym_shape

        if func_overload == torch.ops.aten.new_empty.default:
            self = args[0]
            shape = args[1]
            return FakeSymbolicTensor(shape, self.stride(), self.dtype, self.layout, self.requires_grad, self.device)

        raise RuntimeError(f"operator {func_overload} not supported")


def create_symbolic_tensor(name, arg, shape_env):
    sym_shapes = tuple([shape_env.create_symint(f"{name}_{idx}", val) for idx, val in enumerate(arg.sym_size())])
    sym_strides = tuple([shape_env.create_symint(f"{name}_{idx}_stride", val) for idx, val in enumerate(arg.stride())])
    return FakeSymbolicTensor(sym_shapes, sym_strides, arg.dtype, arg.layout, arg.requires_grad, arg.device)


CPP_SYMINT_CLASS = type(torch._C.SymbolicIntNode.new_symint(1))


class TestPySymInt(TestCase):

    @skipIfNoSympy
    def test_roundtrip(self):
        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5, 4, 3), shape_env)
        self.assertTrue(not isinstance(x.sym_size(0), PySymInt))
        self.assertTrue(isinstance(x.sym_size(0), CPP_SYMINT_CLASS))

        self.assertEqual(int(x.sym_size(0)), 5)
        self.assertEqual(int(x.sym_size(1)), 4)
        self.assertEqual(int(x.sym_size(2)), 3)

        self.assertEqual(int(x.sym_size()[0]), 5)
        self.assertEqual(int(x.sym_size()[1]), 4)
        self.assertTrue(isinstance(x.sym_size()[1], CPP_SYMINT_CLASS))
        self.assertEqual(int(x.sym_size()[2]), 3)

        self.assertEqual(int(x.sym_size(0)), 5)
        self.assertEqual(int(x.sym_size(1)), 4)
        self.assertEqual(int(x.sym_size(2)), 3)
        self.assertTrue(isinstance(x.sym_size(2), CPP_SYMINT_CLASS))

    @skipIfNoSympy
    def test_binary(self):
        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5, 4, 3), shape_env)
        y = create_symbolic_tensor("y", torch.randn(5, 4, 3), shape_env)

        z = x + y
        self.assertEqual(int(z.sym_size(0)), 5)
        self.assertEqual(int(z.sym_size(1)), 4)
        self.assertEqual(int(z.sym_size(2)), 3)

        # broadcasting
        y = create_symbolic_tensor("y", torch.randn(1, 4, 1), shape_env)
        z = x + y
        self.assertEqual(int(z.sym_size(0)), 5)
        self.assertEqual(int(z.sym_size(1)), 4)
        self.assertEqual(int(z.sym_size(2)), 3)

    @skipIfNoSympy
    def test_symint_args(self):
        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5, 4, 3), shape_env)
        y = create_symbolic_tensor("y", torch.randn(5, 4, 1), shape_env)
        LAST_DIM = 2
        z = x.narrow_copy(LAST_DIM, 0, y.sym_size(LAST_DIM))
        self.assertEqual(int(z.sym_size(2)), int(y.sym_size(2)))

        # arithmetic expr with two symints
        z = x.narrow_copy(LAST_DIM, 0, x.sym_size(LAST_DIM) - y.sym_size(LAST_DIM))
        self.assertEqual(int(z.sym_size(2)), 2)

        # arithmetic expr with a symint and python int
        z = x.narrow_copy(LAST_DIM, 0, x.sym_size(LAST_DIM) - 1)
        self.assertEqual(int(z.sym_size(2)), 2)

    @skipIfNoSympy
    def test_symint_vargs(self):
        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5, 4, 3), shape_env)
        y = create_symbolic_tensor("y", torch.randn(1, 4, 1), shape_env)

        # varargs
        z = y.expand(x.sym_size(0), y.sym_size(1), x.sym_size(2))
        self.assertEqual(int(z.sym_size(0)), 5)
        self.assertEqual(int(z.sym_size(1)), 4)
        self.assertEqual(int(z.sym_size(2)), 3)

        # shape list
        z = y.expand((x.sym_size(0), y.sym_size(1), x.sym_size(2)))
        self.assertEqual(int(z.sym_size(0)), 5)
        self.assertEqual(int(z.sym_size(1)), 4)
        self.assertEqual(int(z.sym_size(2)), 3)

        # mixed python symints and ints
        z = y.expand(x.sym_size(0), y.sym_size(1), 3)
        self.assertEqual(int(z.sym_size(0)), 5)
        self.assertEqual(int(z.sym_size(1)), 4)
        self.assertEqual(int(z.sym_size(2)), 3)

        # mixed python symints and ints in a list
        z = y.expand((x.sym_size(0), y.sym_size(1), 3))
        self.assertEqual(int(z.sym_size(0)), 5)
        self.assertEqual(int(z.sym_size(1)), 4)
        self.assertEqual(int(z.sym_size(2)), 3)

        # mixed python symints and ints
        z = y.expand(5, y.sym_size(1), x.sym_size(2))
        self.assertEqual(int(z.sym_size(0)), 5)
        self.assertEqual(int(z.sym_size(1)), 4)
        self.assertEqual(int(z.sym_size(2)), 3)

        # mixed python ints and symints in a list
        z = y.expand((5, y.sym_size(1), x.sym_size(2)))
        self.assertEqual(int(z.sym_size(0)), 5)
        self.assertEqual(int(z.sym_size(1)), 4)
        self.assertEqual(int(z.sym_size(2)), 3)

    @skipIfNoSympy
    def test_size_expressions(self):
        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5), shape_env)
        expand_x = x.expand(x.sym_size(0), x.sym_size(0))
        if expand_x.sym_size(0) > 3:
            result = expand_x + expand_x
        else:
            result = expand_x + expand_x

        gt_op = shape_env.guards[0][0]
        self.assertTrue(isinstance(gt_op, sympy.core.relational.StrictGreaterThan))
        self.assertTrue(str(x.sym_size(0)), str(gt_op.args[0]))
        self.assertTrue(str(expand_x.sym_size(1)), str(x.sym_size(0)))
        self.assertTrue(str(expand_x.sym_size(1)), str(result.sym_size(0)))

    @skipIfNoSympy
    def test_aten_ops(self):

        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5), shape_env)
        torch.ops.aten.narrow_copy.SymInt(x, 0, 0, x.sym_size(0))

        shape_env = ShapeEnv()
        x = create_symbolic_tensor("x", torch.randn(5, 4, 3), shape_env)
        torch.ops.aten.expand.SymInt(x, [x.sym_size(0), x.sym_size(1), x.sym_size(2)])

    def test_fx_trace_intlist(self):
        class CustomModule(torch.nn.Module):
            def forward(self, x):
                bs, c, h, w = x.shape
                return F.pad(x, (0, w % 2, 0, h % 2, 0, 0))

        m = CustomModule()
        x = torch.rand(1, 3, 4, 4)
        # should not TypeError: pad(): argument 'pad' (position 2) must be
        # tuple of ints, not tuple
        torch.fx.symbolic_trace(m)


if __name__ == '__main__':
    run_tests()
