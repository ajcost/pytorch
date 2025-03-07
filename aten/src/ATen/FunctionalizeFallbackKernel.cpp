#include <ATen/core/dispatch/Dispatcher.h>
#include <ATen/core/LegacyTypeDispatch.h>
#include <ATen/EmptyTensor.h>
#include <ATen/FunctionalTensorWrapper.h>
#include <torch/library.h>
#include <c10/util/irange.h>

#ifndef AT_PER_OPERATOR_HEADERS
#include <ATen/ATen.h>
#include <ATen/Functions.h>
#include <ATen/NativeFunctions.h>
#else
#include <ATen/ops/_to_copy.h>
#include <ATen/ops/to_native.h>
#include <ATen/ops/resize.h>
#include <ATen/ops/as_strided.h>
#include <ATen/ops/as_strided_copy.h>
#include <ATen/ops/empty_strided_native.h>
#endif

namespace {
  void functionalizeFallback(const c10::OperatorHandle& op, c10::DispatchKeySet dispatchKeySet, torch::jit::Stack* stack) {
    const auto& schema = op.schema();
    TORCH_INTERNAL_ASSERT(!schema.hasAnyAliasInfo(), "mutating and aliasing ops should all have codegen'd kernels");
    const auto num_arguments = schema.arguments().size();
    const auto arguments_begin = stack->size() - num_arguments;
    auto arguments = torch::jit::last(stack, num_arguments);

    auto any_functional_inputs = false;
    auto any_tensor_inputs = false;
    for (uint64_t idx = 0; idx < num_arguments; ++idx) {
      const auto& ivalue = arguments[idx];
      if (ivalue.isTensor()) {
        any_tensor_inputs = true;
        auto t = ivalue.toTensor();
        if (t.defined() && at::functionalization::impl::isFunctionalTensor(t)) {
          any_functional_inputs = true;
          at::functionalization::impl::sync(t);
          auto t_new = c10::IValue(at::functionalization::impl::from_functional_tensor(t));
          (*stack)[arguments_begin + idx] = t_new;
        }
      } else if (ivalue.isTensorList()) {
        any_tensor_inputs = true;
        auto tensors = ivalue.toTensorList();
        if (at::functionalization::impl::isFunctionalTensor(tensors)) {
          any_functional_inputs = true;
          at::functionalization::impl::sync(tensors);
          auto t_new = c10::IValue(at::functionalization::impl::from_functional_tensor(tensors));
          (*stack)[arguments_begin + idx] = t_new;
        }
      } else if (ivalue.isOptionalTensorList()) {
        any_tensor_inputs = true;
        auto opt_tensors = ivalue.toOptionalTensorList();
        if (at::functionalization::impl::isFunctionalTensor(opt_tensors)) {
          any_functional_inputs = true;
          at::functionalization::impl::sync(opt_tensors);
          auto t_new = c10::IValue(at::functionalization::impl::from_functional_tensor(opt_tensors));
          (*stack)[arguments_begin + idx] = t_new;
        }
      }
    }
    // we should wrap the output if any inputs were wrapped,
    // OR if we're hitting a factory function (with no tensor inputs)
    auto should_wrap_outputs = !any_tensor_inputs || any_functional_inputs;
    {
      at::AutoDispatchSkipFunctionalize guard;
      op.callBoxed(stack);
    }
    const auto num_returns = schema.returns().size();
    const auto returns_begin = stack->size() - num_returns;
    auto returns = torch::jit::last(stack, num_returns);

    for (const auto idx : c10::irange(num_returns)) {
      const auto& ivalue = returns[idx];
      if (ivalue.isTensor() && should_wrap_outputs) {
        auto t = ivalue.toTensor();
        if (!t.defined()) continue;
        auto t_new = c10::IValue(at::functionalization::impl::to_functional_tensor(t));
        (*stack)[returns_begin + idx] = t_new;
      } else if (ivalue.isTensorList() && should_wrap_outputs) {
        auto tensors = ivalue.toTensorList();
        auto t_new = c10::IValue(at::functionalization::impl::to_functional_tensor(tensors));
        (*stack)[returns_begin + idx] = t_new;
      } else if (ivalue.isOptionalTensorList() && should_wrap_outputs) {
        auto opt_tensors = ivalue.toOptionalTensorList();
        auto t_new = c10::IValue(at::functionalization::impl::to_functional_tensor(opt_tensors));
        (*stack)[returns_begin + idx] = t_new;
      }
    }
  }
}

// Vanilla implementation to compute contiguous strides given some sizes.
// Should probably refactor this into shared code (also used in TensorImpl.h)
std::vector<int64_t> compute_contiguous_strides(c10::IntArrayRef sizes) {
  auto n = sizes.size();
  std::vector<int64_t> strides(n);
  if (n == 0) return strides;

  strides[n - 1] = 1;
  for (int64_t i = n - 2; i >= 0; --i) {
    strides[i] = strides[i+1] * sizes[i];
  }
  return strides;
}

// resize_() is special because:
// - when we resize to a larger size, it acts as a mutation
// - when we resize to a smaller size, it acts as a view
// See Note [resize_ in Functionalization] for more dtails
const at::Tensor & resize__functionalization(c10::DispatchKeySet dispatchKeySet, const at::Tensor & self, at::IntArrayRef size, c10::optional<at::MemoryFormat> memory_format) {
  // First unwrap the tensor arguments
  at::Tensor self_;
  if (at::functionalization::impl::isFunctionalTensor(self)) {
    at::functionalization::impl::sync(self);
    self_ = at::functionalization::impl::from_functional_tensor(self);
  } else {
    self_ = self;
  }
  // Case 1: arguments are not functional tensors, so we no-op and redispatch.
  if (!at::functionalization::impl::isFunctionalTensor(self)) {
     at::AutoDispatchSkipFunctionalize guard;
     at::Tensor tmp_output = self_.resize_(size, memory_format);
     return self;
  }

  // Case 2: actually functionalize resize_()
  at::Tensor tmp_output;
  {
    at::AutoDispatchSkipFunctionalize guard;
    tmp_output = at::resize_functional(self_, size, memory_format);
  }

  auto itemsize = self.dtype().itemsize();
  auto storage_offset = self.storage_offset();
  auto new_size_bytes = at::detail::computeStorageNbytesContiguous(size, itemsize, storage_offset);
  auto needs_resize_storage = new_size_bytes > self.storage().nbytes();

  if (needs_resize_storage) {
    // If resize_() actually increases the size of the storage, then we need to tell FunctionalTensorWrapper about it.
    // See Note[resize_() in functionalization pass]
    auto func_impl = at::functionalization::impl::unsafeGetFunctionalWrapper(self);
    func_impl->maybe_replace_storage(tmp_output);
    // See the note - we're guaranteed at this point that "self" is *not* a view (and has no outstanding views)
    // So we don't need to treat the output of resize as view tensor.
    return self;
  }

  // Otherwise, we know that we're resizing to a smaller size.
  // resize_() is effectively a view operator.
  // The output of resizing is equivalent to taking a slice of a larger tensor.
  // We have to emulate this "slicing" with an as_strided call.
  auto reapply_views = at::functionalization::impl::getFunctionalizationReapplyViewsTLS();
  at::functionalization::ViewMeta view_meta = at::functionalization::ViewMeta(
    [reapply_views = reapply_views, size = size.vec()](const at::Tensor & base, int64_t mutated_view_idx) -> at::Tensor {
      if (reapply_views) {
        return base.as_strided(size, compute_contiguous_strides(size));
      } else {
        return at::as_strided_copy(base, size, compute_contiguous_strides(size));
      }
    },
    [size = size.vec()](const at::Tensor & base, const at::Tensor & mutated_view, int64_t mutated_view_idx) -> at::Tensor {
      return base.as_strided_scatter(mutated_view, size, compute_contiguous_strides(size));
    }
  );
  at::functionalization::impl::mutate_view_meta(self, view_meta);
  return self;
}


at::Tensor lift_functionalize(const at::Tensor & self) {
  TORCH_INTERNAL_ASSERT(!at::functionalization::impl::isFunctionalTensor(self));
  return at::functionalization::impl::to_functional_tensor(self);
}

bool device_opted_into_functionalization(c10::Device self_device, c10::optional<c10::Device> tgt_device) {
    // If the target device is empty, then the output tensor should be on the same device as the input
    auto real_tgt_device = tgt_device.has_value() ? tgt_device.value() : self_device;
    return real_tgt_device.type() == c10::DeviceType::XLA || real_tgt_device.type() == c10::DeviceType::Lazy;
}

// note I only need this because the to.dtype/to.dtype_layout overload calls this, so we skip the op above.
// We should probably get rid of this though.
at::Tensor _to_copy_functionalize(
        const at::Tensor & self,
        c10::optional<at::ScalarType> dtype,
        c10::optional<at::Layout> layout,
        c10::optional<at::Device> device,
        c10::optional<bool> pin_memory,
        bool non_blocking,
        c10::optional<at::MemoryFormat> memory_format) {
  at::Tensor self_;
  if (at::functionalization::impl::isFunctionalTensor(self)) {
    // sync any pending updates
    at::functionalization::impl::sync(self);
    // pass the unwrapped tensor to the backend
    self_ = at::functionalization::impl::from_functional_tensor(self);
  } else {
    self_ = self;
  }

  at::AutoDispatchSkipFunctionalize guard;
  auto out = at::_to_copy(self_, dtype, layout, device, pin_memory, non_blocking, memory_format);

  // Special case: if the Functionalize key is not in TLS, we assume that we're running
  // on a lazy backend (LTC).
  // In that case, if we're copying to a non-functionalize-enabled device,
  // then the functionalization pass should "end". We need to sync any updates on the input
  // tensor, but we shouldn't wrap the output.
  if (!c10::impl::tls_local_dispatch_key_set().included_.has(c10::DispatchKey::Functionalize)) {
    if (!device_opted_into_functionalization(self.device(), device)) {
      return out;
    }
  }
  return at::functionalization::impl::to_functional_tensor(out);
}

TORCH_LIBRARY_IMPL(_, Functionalize, m) {
  m.fallback(torch::CppFunction::makeFromBoxedFunction<&functionalizeFallback>());
}

TORCH_LIBRARY_IMPL(aten, Functionalize, m) {
  m.impl("resize_", TORCH_FN(resize__functionalization));
  m.impl("lift", TORCH_FN(lift_functionalize));
  m.impl("_to_copy", TORCH_FN(_to_copy_functionalize));
}
