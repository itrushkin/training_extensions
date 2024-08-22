# Copyright (C) 2024 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import pytest
import torch
from otx.core.data.entity.base import ImageInfo
from otx.core.data.entity.diffusion import (
    DiffusionBatchDataEntity,
    DiffusionBatchPredEntity,
)

SKIP_TRANSFORMERS_TEST = False
try:
    from diffusers import StableDiffusionPipeline
    from otx.algo.diffusion.huggingface_model import HuggingFaceModelForDiffusion
except ImportError:
    SKIP_TRANSFORMERS_TEST = True


@pytest.mark.skipif(
    SKIP_TRANSFORMERS_TEST,
    reason="Either 'transformers' or 'diffusers' is not installed",
)
class TestHuggingFaceModelForDiffusion:
    @pytest.fixture()
    def fxt_diffusion_model(self):
        return HuggingFaceModelForDiffusion(
            model_name_or_path="CompVis/stable-diffusion-v1-4",
        )

    @pytest.fixture()
    def fxt_diffusion_batch_data_entity(self) -> DiffusionBatchDataEntity:
        return DiffusionBatchDataEntity(
            batch_size=1,
            images=[torch.randn(3, 512, 512), torch.randn(3, 512, 512)],
            imgs_info=[
                ImageInfo(img_idx=1, img_shape=(512, 512), ori_shape=(512, 512)),
                ImageInfo(img_idx=2, img_shape=(512, 512), ori_shape=(512, 512)),
            ],
            captions=["noisy image 1", "noisy image 2"],
        )

    def test_init(self, fxt_diffusion_model):
        print(fxt_diffusion_model.__dict__)
        assert isinstance(fxt_diffusion_model.pipe, StableDiffusionPipeline)

    def test_customize_inputs(self, fxt_diffusion_model, fxt_det_batch_data_entity):
        outputs = fxt_diffusion_model._customize_inputs(fxt_det_batch_data_entity)
        assert "pixel_values" in outputs
        assert "input_ids" in outputs

    def test_customize_outputs(self, fxt_diffusion_model, fxt_det_batch_data_entity):
        outputs = (torch.zeros(1, 4, 512, 512), torch.zeros(1, 4, 512, 512))
        fxt_diffusion_model.training = True
        result = fxt_diffusion_model._customize_outputs(
            outputs,
            fxt_det_batch_data_entity,
        )
        assert isinstance(result, dict)
        assert "mse" in result

        fxt_diffusion_model.training = False
        result = fxt_diffusion_model._customize_outputs(
            outputs,
            fxt_det_batch_data_entity,
        )
        assert isinstance(result, DiffusionBatchPredEntity)
        assert len(result.images) == 2
