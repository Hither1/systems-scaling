"""
Copyright (c) Microsoft Corporation.
Licensed under the MIT License.
"""

import torch
import torch.nn.functional as F

from .vector_ops import *
from .specs import apply_mx_specs, get_backwards_mx_specs
from .specs import mx_assert_test
from .norm_utils import _norm_forward, _norm_backward_LN, _norm_backward


torch_layer_norm = F.layer_norm

import torch

class LayerNormFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, weight, bias, eps, mx_specs=None,
                name=None):
        ctx.eps = eps
        ctx.name = name

        if not mx_specs["dont_quantize_layernorm"]:
            x = vec_quantize(x, mx_specs=mx_specs)
            bf_weight = vec_quantize(weight, mx_specs=mx_specs)
            bf_bias = vec_quantize(bias, mx_specs=mx_specs) if bias is not None else bias
        else:
            bf_weight = weight
            bf_bias = bias

        output, _, x_norm, _, _, x_vare = \
                _norm_forward(
                        x, -1, bf_weight, bf_bias, eps,
                        mx_specs, quantize = (not mx_specs["dont_quantize_layernorm"]))

        # stash for backprop
        if mx_specs['quantize_backprop']:
            ctx.save_for_backward(x_norm, x_vare, bf_weight)
        else:
            ctx.save_for_backward(x_norm, x_vare, weight)

        ctx.mx_specs = get_backwards_mx_specs(mx_specs)

        return output

    @staticmethod
    def backward(ctx, grad_output):
        H = grad_output.shape[-1]
        sum_axes = list(range(grad_output.ndim - 1))
        # get stashed intermediate
        x_norm, x_vare, weight = ctx.saved_tensors


        if not ctx.mx_specs['dont_quantize_layernorm']:
            grad_output = vec_quantize(grad_output, mx_specs=ctx.mx_specs)
        # grad_bias
        grad_bias = vec_reduce_sum(grad_output, sum_axes,
                                mx_specs=ctx.mx_specs, quantize=(not ctx.mx_specs['dont_quantize_layernorm']))

        # grad_weight
        grad_weight = vec_mul(grad_output, x_norm, mx_specs=ctx.mx_specs, quantize=(not ctx.mx_specs['dont_quantize_layernorm']))

        grad_weight = vec_reduce_sum(grad_weight, sum_axes,
                                    mx_specs=ctx.mx_specs, quantize=(not ctx.mx_specs['dont_quantize_layernorm']))

        grad_input = _norm_backward_LN(
                grad_output, -1, weight, x_norm,
                x_vare, ctx.mx_specs, quantize=(not ctx.mx_specs['dont_quantize_layernorm']))

        return (grad_input, grad_weight, None, None, None, None) # grad_bias


class LayerNorm(torch.nn.LayerNorm):
    def __init__(self, hidden_size, eps=1e-12, mx_specs=None, name=None):
        """Construct a layernorm module in the TF style (epsilon inside the square root).
        """
        mx_assert_test(mx_specs)
        self.mx_none = (mx_specs is None)

        self.name = name
        self.mx_specs = apply_mx_specs(mx_specs)

        super().__init__(
                normalized_shape=hidden_size, eps=eps)

    def apply_mx_specs(self, mx_specs):
        self.mx_specs = mx_specs
        self.mx_none = (mx_specs is None)
        self.mx_specs = apply_mx_specs(mx_specs)

    def append_name(self, postfix):
        self.name += postfix

    def forward(self, x):
        if self.mx_none or self.mx_specs["dont_quantize_layernorm"]:
            return super().forward(x)

        return LayerNormFunction.apply(
                x, self.weight, self.bias, self.eps,
                self.mx_specs, self.name)


class RMSNormFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, weight, bias, eps, mx_specs=None, name=None):
        ctx.eps = eps
        ctx.name = name

        x = vec_quantize(x, mx_specs=mx_specs)

        # x2 (N, L, H)
        x2 = vec_mul(x, x, mx_specs=mx_specs)

        # x_ms (N, L, 1)
        x_ms = vec_reduce_mean(x2, -1, keepdim=True,
                               mx_specs=mx_specs)

        # x_vare (N, L, 1)
        x_mse = vec_add(x_ms, eps, mx_specs=mx_specs)
        # x_std (N, L, 1)
        x_rms = vec_sqrt(x_mse, mx_specs=mx_specs)

        # x_norm (N, L, H)
        x_rms_inv = vec_recip(x_rms, mx_specs=mx_specs)
        x_norm = vec_mul(x, x_rms_inv, mx_specs=mx_specs)
        # weight (H)
        bf_weight = vec_quantize(weight, mx_specs=mx_specs)
        # bias (H)
        bf_bias = vec_quantize(bias, mx_specs=mx_specs)
        # x_scale (N, L, H)
        x_scale = vec_mul(bf_weight, x_norm, mx_specs=mx_specs)
        # output
        output = vec_add(x_scale, bf_bias, mx_specs=mx_specs)

        # stash for backprop
        if mx_specs['quantize_backprop']:
            ctx.save_for_backward(x_norm, x_rms_inv, bf_weight)
        else:
            ctx.save_for_backward(x_norm, x_rms_inv, weight)

        ctx.mx_specs = get_backwards_mx_specs(mx_specs)
        return output

    @staticmethod
    def backward(ctx, grad_output):
        """ grad_in = grad_out*w/x_rms - sum(grad_out*w*x)*x / (x_rms^3) """
        H = grad_output.shape[-1]
        sum_axes = list(range(len(grad_output.shape) - 1))
        # get stashed intermediate
        x_norm, x_rms_inv, weight = ctx.saved_tensors

        grad_output = vec_quantize(grad_output, mx_specs=ctx.mx_specs)
        # grad_bias
        grad_bias = vec_reduce_sum(grad_output, sum_axes,
                                   mx_specs=ctx.mx_specs)

        # grad_weight
        grad_weight = vec_mul(grad_output, x_norm, mx_specs=ctx.mx_specs)

        grad_weight = vec_reduce_sum(grad_weight, sum_axes,
                                     mx_specs=ctx.mx_specs)

        # grad_input
        # dx_norm (N, L, H)
        dx_norm = vec_mul(grad_output, weight, mx_specs=ctx.mx_specs)
        # dx1 (N, L, H)
        dx1 = vec_mul(dx_norm, x_rms_inv, mx_specs=ctx.mx_specs)

        # dx_norm2 (N, L, 1)
        dx_norm2 = vec_mul(dx1, x_norm, mx_specs=ctx.mx_specs)
        dx_norm2 = vec_reduce_mean(dx_norm2, -1, keepdim=True,
                                   mx_specs=ctx.mx_specs)

        # dx_norm3 (N, L, H)
        dx_norm3 = vec_mul(x_norm, dx_norm2, mx_specs=ctx.mx_specs)

        grad_input = vec_sub(dx1, dx_norm3)

        return (grad_input, grad_weight, grad_bias, None, None, None)


class RMSNorm(torch.nn.LayerNorm):
    def __init__(self, hidden_size, eps=1e-12, mx_specs=None, name=None):
        """ There's no torch equivalent for RMSNorm
        """
        mx_assert_test(mx_specs)

        self.name = name
        self.mx_specs = apply_mx_specs(mx_specs)

        super().__init__(
                normalized_shape=hidden_size, eps=eps)

    def apply_mx_specs(self, mx_specs):
        self.mx_specs = mx_specs
        self.mx_none = (mx_specs is None)
        self.mx_specs = apply_mx_specs(mx_specs)

    def append_name(self, postfix):
        self.name += postfix

    def forward(self, x):
        return RMSNormFunction.apply(
                x, self.weight, self.bias, self.eps,
                self.mx_specs, self.name)
    

# below commented out code was a workaround to get LN without affine parameters
# to work with this package... you can uncomment and use at your own risk!

# def layer_norm(input, normalized_shape, weight, bias, eps=1e-12,
#                mx_specs=None, name=None):
#     mx_assert_test(mx_specs)
#     #if mx_specs is None or weight is None: # modified to handle None weight (if elementwise affine is not used)
#     #    return torch_layer_norm(input, normalized_shape, weight, bias, eps)
#         #return F._layer_norm(input, normalized_shape, weight, bias, eps) # directly call C impl  ??
#         #return torch.nn.functional.layer_norm(input, normalized_shape, weight, bias, eps)
#     with torch.no_grad():
#         input_dtype = input.dtype
#         device = input.device
            
#         # Cast to float32 for better numerical stability
#         x = input.to(torch.float32)
        
#         # Calculate dimensions to normalize over
#         dims = tuple(-(i+1) for i in range(len(normalized_shape)))
            
#         # Calculate mean
#         mean = x.mean(dims, keepdim=True)
            
#         # Calculate variance with Bessel's correction
#         # Get the number of elements in the normalization dimensions
#         n = torch.tensor(1, dtype=torch.float32, device=device)
#         for d in dims:
#             n = n * x.size(d if d >= 0 else x.ndim + d)
            
#         # Apply Bessel's correction (n/(n-1))
#         bessel_correction = n / (n - 1)
#         var = (x - mean).pow(2).mean(dims, keepdim=True) # * bessel_correction
            
#         # Normalize
#         x_norm = (x - mean) / torch.sqrt(var + eps)
            
#         # Apply affine transform if provided (weight should be None for this path)
#         if weight is not None:
#             x_norm = x_norm * weight.view(*(1,) * (input.ndim - weight.ndim), *weight.shape)
#         if bias is not None:
#             x_norm = x_norm + bias.view(*(1,) * (input.ndim - bias.ndim), *bias.shape)
                
#         # Return with original dtype
#         return x_norm.to(input_dtype)
    
#     mx_specs = apply_mx_specs(mx_specs)
#     if weight is not None:
#         assert(normalized_shape == weight.shape)
#     return LayerNormFunction.apply(input, weight, bias, eps, mx_specs, name)

def layer_norm(input, normalized_shape, weight, bias, eps=1e-12,
               mx_specs=None, name=None):
    mx_assert_test(mx_specs)
    if mx_specs is None:
        return torch_layer_norm(input, normalized_shape, weight, bias, eps)
    mx_specs = apply_mx_specs(mx_specs)
    assert(normalized_shape == weight.shape)
    return LayerNormFunction.apply(input, weight, bias, eps, mx_specs, name)
