"""This module contains the Encoder class for the autoencoder."""

from __future__ import annotations

import torch
import torch.nn.functional as F  # noqa: N812
from torch import nn

from .mid import Mid
from .resnet_block import ResnetBlock


class Encoder(nn.Module):
    """Encoder module for the autoencoder."""

    class _Conv2d(nn.Conv2d):
        def __init__(
            self,
            in_channels: int,
            out_channels: int,
            kernel_size: int,
            stride: int,
            padding: tuple[int, int, int, int],
        ) -> None:
            super().__init__(in_channels, out_channels, kernel_size, stride)
            self.padding: tuple[int, int, int, int] = padding

        def _conv_forward(self, _input: torch.Tensor, weight: torch.Tensor, bias: torch.Tensor | None) -> torch.Tensor:
            return F.conv2d(
                F.pad(_input, self.padding),
                weight,
                bias,
                self.stride,
                (0, 0),
                self.dilation,
                self.groups,
            )

    def __init__(self) -> None:
        super().__init__()
        sz = [(128, 128), (128, 256), (256, 512), (512, 512)]
        self.conv_in = nn.Conv2d(3, 128, 3, padding=1)

        arr = nn.ModuleList()
        for i, s in enumerate(sz):
            d = nn.ModuleDict({"block": nn.ModuleList([ResnetBlock(s[0], s[1]), ResnetBlock(s[1], s[1])])})
            if i != len(sz) - 1:
                d["downsample"] = nn.ModuleDict(
                    {"conv": Encoder._Conv2d(s[1], s[1], 3, stride=2, padding=(0, 1, 0, 1))},
                )
            arr.append(d)
        self.down = arr

        self.mid = Mid(512)
        self.norm_out = nn.GroupNorm(32, 512)
        self.conv_out = nn.Conv2d(512, 8, 3, padding=1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass of the Encoder module.

        Args:
            x (torch.Tensor): Input tensor.

        Returns:
            torch.Tensor: Output tensor.
        """
        x = self.conv_in(x)

        for layer in self.down:
            for b in layer.block:
                x = b(x)
            if hasattr(layer, "downsample"):
                x = layer.downsample.conv(x)

        x = self.mid(x)
        return self.conv_out(F.silu(self.norm_out(x)))
