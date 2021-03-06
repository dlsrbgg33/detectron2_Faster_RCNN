# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import torch

from detectron2.utils.registry import Registry

EXTRA_NECK_REGISTRY = Registry("EXTRA_NECK")  # noqa F401 isort:skip
EXTRA_NECK_REGISTRY.__doc__ = """
Registry for meta-architectures, i.e. the whole model.

The registered object will be called with `obj(cfg)`
and expected to return a `nn.Module` object.
"""


def build_model(cfg):
    """
    Build the whole model architecture, defined by ``cfg.MODEL.META_ARCHITECTURE``.
    Note that it does not load any weights from ``cfg``.
    """
    meta_arch = cfg.MODEL.EXTRA_NECK
    model = EXTRA_NECK_REGISTRY.get(meta_arch)(cfg)
    model.to(torch.device(cfg.MODEL.DEVICE))
    return model
