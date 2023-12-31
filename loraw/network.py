import torch
from torch import nn
from torch import optim
from enum import Enum

from .modules import LoRAWLinear, LoRAWConv1d
from .util import *
from .attributes import *


class TargetableModules(Enum):
    Linear = LoRAWLinear
    Conv1d = LoRAWConv1d


def scan_model(model, target_blocks, whitelist=None, blacklist=None):
    # Find all targetable modules that are in targeted blocks
    target_blocks = set(target_blocks)
    # If a whitelist is specified, modules must have at least one whitelisted ancestor
    whitelist = set(whitelist) if whitelist is not None else None
    # If a blacklist is specified, modules must have no blacklisted ancestors
    blacklist = set(blacklist) if blacklist is not None else None
    module_map = {}
    for ancestor_name, ancestor_module in model.named_modules():
        ancestor_set = set(ancestor_name.split("."))
        if (
            ancestor_module.__class__.__name__ in target_blocks
            and (whitelist is None or not ancestor_set.isdisjoint(whitelist))
            and (blacklist is None or ancestor_set.isdisjoint(blacklist))
        ):
            for decendant_name, decendant_module in ancestor_module.named_modules():
                if decendant_module.__class__.__name__ in TargetableModules.__members__:
                    # Get parent if child is not a direct decendant
                    for name in decendant_name.split(".")[:-1]:
                        ancestor_module = ancestor_module._modules[name]
                    # Since '.' is not allowed, replace with '/' (makes it look like a path)
                    id = f"{ancestor_name}.{decendant_name}".replace(".", "/")
                    module_map[id] = {
                        "module": decendant_module,
                        "parent": ancestor_module,
                    }
    print(f"Found {len(module_map)} candidates for LoRAW replacement")
    return module_map


class LoRAWNetwork(nn.Module):
    def __init__(
        self,
        target_map,
        multiplier=1.0,
        lora_dim=16,
        alpha=1.0,
        dropout=None,
        module_dropout=None,
    ):
        super().__init__()
        self.active = False
        self.multiplier = multiplier
        self.lora_dim = lora_dim
        self.alpha = alpha
        self.dropout = dropout
        self.module_dropout = module_dropout
        self.lora_modules = nn.ModuleDict()
        # Scan model and create loras for respective modules
        for name, info in target_map.items():
            module = info["module"]
            self.lora_modules[name] = TargetableModules[
                module.__class__.__name__
            ].value(
                name,
                module,
                multiplier=multiplier,
                lora_dim=lora_dim,
                alpha=alpha,
                dropout=dropout,
                module_dropout=module_dropout,
            )

    def activate(self, target_map):
        for name, module in self.lora_modules.items():
            module.inject(target_map[name]["parent"])
        self.active = True
        print(f"Injected {len(self.lora_modules)} LoRAW modules into model")

    def activate_forward(self):
        for _, module in self.lora_modules.items():
            module.inject_forward()
        self.active = True
        print(f"Forwarded {len(self.lora_modules)} LoRAW modules into model")

    def set_multiplier(self, multiplier):
        self.multiplier = multiplier
        for _, module in self.lora_modules.items():
            module.multiplier = self.multiplier


class LoRAWWrapper:
    def __init__(
        self,
        target_model,
        model_type=None,
        target_blocks=["Attention"],
        component_whitelist=None,
        multiplier=1.0,
        lora_dim=16,
        alpha=1.0,
        dropout=None,
        module_dropout=None,
    ):
        self.target_model = target_model
        self.model_type = model_type
        self.target_blocks = target_blocks
        self.component_whitelist = component_whitelist

        self.is_active = False
        self.is_trainable = False

        # Gather candidates for replacement
        self.target_map = scan_model(
            target_model, target_blocks, whitelist=component_whitelist
        )

        # Construct LoRAW network
        self.net = LoRAWNetwork(
            self.target_map,
            multiplier=multiplier,
            lora_dim=lora_dim,
            alpha=alpha,
            dropout=dropout,
            module_dropout=module_dropout,
        )

        # Get a list of bottom-level lora modules, excluding the originals
        self.residual_modules = nn.ModuleDict()
        for name, module in self.net.lora_modules.items():
            self.residual_modules[f"{name}/lora_down"] = module.lora_down
            self.residual_modules[f"{name}/lora_up"] = module.lora_up

    def activate(self):
        assert not self.is_active, "LoRAW is already active"
        self.net.activate(self.target_map)
        self.is_active = True

    def configure_optimizers(self):
        return optim.Adam([*self.residual_modules.parameters()], lr=self.lr)

    def prepare_for_training(self, training_wrapper, lr=None):
        assert self.is_active, "LoRAW must be activated before training preparation"

        # Freeze target model
        for param in self.target_model.parameters():
            param.requires_grad = False

        # Unfreeze lora modules
        for param in self.residual_modules.parameters():
            param.requires_grad = True

        # Move lora to training device
        self.net.to(device=training_wrapper.device)

        # Replace optimizer to use lora parameters
        if lr is None:
            self.lr = training_wrapper.lr
        else:
            self.lr = lr
        training_wrapper.configure_optimizers = self.configure_optimizers

        # Trim ema model if present
        if self.model_type is not None and self.model_type in EMA_MODEL:
            trim_ema(getattr(training_wrapper, EMA_MODEL[self.model_type]))

        self.is_trainable = True

    def save_weights(self, path, dtype=torch.float16):
        torch.save(self.residual_modules.state_dict(), path)

    def load_weights(self, path):
        weights = torch.load(path, map_location="cpu")
        info = self.residual_modules.load_state_dict(weights, False)
        return info

    def merge_weights(self, path, multiplier=1.0):
        weights = torch.load(path, map_location="cpu")
        for name, weight in weights.items():
            param = self.residual_modules.state_dict()[name]
            param.copy_(param + weight * multiplier)

    def extract_diff(self, tuned_model):
        lora_weights = calculate_svds(
            self.net.lora_modules,
            tuned_model,
            self.net.lora_modules.keys(),
            rank=self.net.lora_dim,
        )
        for name, (down_weight, up_weight) in lora_weights.items():
            self.residual_modules[f"{name}/lora_down"].weight.copy_(down_weight)
            self.residual_modules[f"{name}/lora_up"].weight.copy_(up_weight)


def create_loraw_from_config(config, model):
    loraw_config = config["loraw"]

    model_type = config["model_type"]

    target_blocks = loraw_config.get("target_blocks", None)
    assert target_blocks is not None, "Must specify target blocks in config"

    component_whitelist = loraw_config.get("component_whitelist", None)
    assert component_whitelist is not None, "Must specify component whitelist in config"

    multiplier = loraw_config.get("multiplier", None)
    assert multiplier is not None, "Must specify multiplier in config"

    rank = loraw_config.get("rank", None)
    assert rank is not None, "Must specify rank in config"

    alpha = loraw_config.get("alpha", None)
    assert alpha is not None, "Must specify alpha in config"

    dropout = loraw_config.get("dropout", None)
    if dropout == 0: dropout = None

    module_dropout = loraw_config.get("module_dropout", None)
    if module_dropout == 0: module_dropout = None

    loraw = LoRAWWrapper(
        model,
        model_type=model_type,
        target_blocks=target_blocks,
        component_whitelist=component_whitelist,
        multiplier=multiplier,
        lora_dim=rank,
        alpha=alpha,
        dropout=dropout,
        module_dropout=module_dropout,
    )

    return loraw
