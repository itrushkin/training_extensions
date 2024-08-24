"""Module for implementing feed forward neural networks."""

import torch
from torch import nn


class GEGLU(nn.Module):
    """GEGLU module implementation."""

    def __init__(self, dim_in: int, dim_out: int):
        super().__init__()
        self.proj = nn.Linear(dim_in, dim_out * 2)
        self.dim_out = dim_out

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass of the GEGLU module.

        Args:
            x (torch.Tensor): Input tensor.

        Returns:
            torch.Tensor: Output tensor.
        """
        x, gate = self.proj(x).chunk(2, dim=-1)
        return x * gate.gelu()


class FeedForward(nn.Sequential):
    """FeedForward module implementation."""

    def __init__(self, dim: int, mult: int = 4):
        super().__init__(
            GEGLU(dim, dim * mult),
            nn.Linear(dim * mult, dim),
        )
