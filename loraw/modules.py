import math
import torch
from torch import nn


class LoRAWModule(nn.Module):
    def __init__(
        self,
        lora_name,
        original_module: nn.Module,
        multiplier=1.0,
        lora_dim=16,
        alpha=1.0,
        dropout=None,
        module_dropout=None,
    ):
        super().__init__()
        self.lora_name = lora_name
        self.lora_dim = lora_dim
        self.multiplier = multiplier
        self.original_module = original_module
        self.original_forward = original_module.forward
        self.dropout = dropout
        self.module_dropout = module_dropout

        if type(alpha) == torch.Tensor:
            alpha = alpha.detach().float().numpy()
        alpha = self.lora_dim if alpha is None or alpha == 0 else alpha
        self.scale = alpha / self.lora_dim

    def forward(self, x):
        # module dropout (skip loraw module)
        if self.module_dropout is not None and self.training:
            if torch.rand(1) < self.module_dropout:
                return self.original_forward(x)

        # down to low-rank
        lx = self.lora_down(x)

        # regular dropout
        if self.dropout is not None and self.training:
            lx = torch.nn.functional.dropout(lx, p=self.dropout)

        # back up to full-rank
        lx = self.lora_up(lx)

        # add scaled residual to original
        return self.original_forward(x) + lx * self.scale * self.multiplier

    def inject(self, parent_module):
        parent_module._modules[self.lora_name.split("/")[-1]] = self

    def inject_forward(self):
        self.original_module.forward = self.forward
        del self.original_module


class LoRAWLinear(LoRAWModule):
    def __init__(
        self,
        lora_name,
        original_module: nn.Module,
        multiplier=1,
        lora_dim=16,
        alpha=1,
        dropout=None,
        module_dropout=None,
    ):
        super().__init__(
            lora_name,
            original_module,
            multiplier,
            lora_dim,
            alpha,
            dropout,
            module_dropout,
        )
        in_dim = original_module.in_features
        out_dim = original_module.out_features
        self.lora_down = torch.nn.Linear(in_dim, self.lora_dim, bias=False)
        self.lora_up = torch.nn.Linear(self.lora_dim, out_dim, bias=False)

        torch.nn.init.kaiming_uniform_(self.lora_down.weight, a=math.sqrt(5))
        torch.nn.init.zeros_(self.lora_up.weight)


class LoRAWConv1d(LoRAWModule):
    def __init__(
        self,
        lora_name,
        original_module: nn.Module,
        multiplier=1,
        lora_dim=16,
        alpha=1,
        dropout=None,
        module_dropout=None,
    ):
        super().__init__(
            lora_name,
            original_module,
            multiplier,
            lora_dim,
            alpha,
            dropout,
            module_dropout,
        )
        in_dim = original_module.in_channels
        out_dim = original_module.out_channels
        kernel_size = original_module.kernel_size
        stride = original_module.stride
        padding = original_module.padding
        self.lora_down = torch.nn.Conv1d(
            in_dim, self.lora_dim, kernel_size, stride, padding, bias=False
        )
        self.lora_up = torch.nn.Conv1d(self.lora_dim, out_dim, 1, 1, bias=False)

        torch.nn.init.kaiming_uniform_(self.lora_down.weight, a=math.sqrt(5))
        torch.nn.init.zeros_(self.lora_up.weight)
