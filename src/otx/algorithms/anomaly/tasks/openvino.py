"""OpenVINO Anomaly Task."""

# Copyright (C) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.

import io
import json
import os
import random
import tempfile
from typing import Any, Dict, List, Optional, Tuple, Union
from zipfile import ZipFile

import nncf
import numpy as np
import openvino.runtime as ov
from addict import Dict as ADDict
from anomalib.data.utils.transform import get_transforms
from nncf.common.quantization.structs import QuantizationPreset
from omegaconf import OmegaConf
from openvino.model_api.models import AnomalyDetection, AnomalyResult

from otx.algorithms.anomaly.adapters.anomalib.config import get_anomalib_config
from otx.algorithms.anomaly.configs.base.configuration import BaseAnomalyConfig
from otx.algorithms.common.utils import embed_ir_model_data
from otx.algorithms.common.utils.ir import check_if_quantized
from otx.algorithms.common.utils.utils import read_py_config
from otx.api.configuration.configurable_parameters import ConfigurableParameters
from otx.api.entities.datasets import DatasetEntity
from otx.api.entities.inference_parameters import (
    InferenceParameters,
    default_progress_callback,
)
from otx.api.entities.model import (
    ModelEntity,
    ModelFormat,
    ModelOptimizationType,
    ModelPrecision,
    OptimizationMethod,
)
from otx.api.entities.model_template import TaskType
from otx.api.entities.optimization_parameters import OptimizationParameters
from otx.api.entities.result_media import ResultMediaEntity
from otx.api.entities.resultset import ResultSetEntity
from otx.api.entities.scored_label import ScoredLabel
from otx.api.entities.task_environment import TaskEnvironment
from otx.api.serialization.label_mapper import LabelSchemaMapper, label_schema_to_bytes
from otx.api.usecases.evaluation.metrics_helper import MetricsHelper
from otx.api.usecases.evaluation.performance_provider_interface import (
    IPerformanceProvider,
)
from otx.api.usecases.exportable_code import demo
from otx.api.usecases.tasks.interfaces.deployment_interface import IDeploymentTask
from otx.api.usecases.tasks.interfaces.evaluate_interface import IEvaluationTask
from otx.api.usecases.tasks.interfaces.inference_interface import IInferenceTask
from otx.api.usecases.tasks.interfaces.optimization_interface import (
    IOptimizationTask,
    OptimizationType,
)
from otx.api.utils.anomaly_utils import create_detection_annotation_from_anomaly_heatmap
from otx.api.utils.segmentation_utils import create_annotation_from_segmentation_map
from otx.utils.logger import get_logger

logger = get_logger()


class OTXNNCFAnomalyDataloader:
    """Dataloader for loading OTX dataset for NNCF optimization.

    Args:
        dataset (DatasetEntity): OTX dataset entity
        model: (AnomalyDetection) The modelAPI model used for fetching the transforms.
        shuffle (bool, optional): Shuffle dataset. Defaults to True.
    """

    def __init__(
        self,
        dataset: DatasetEntity,
        model: AnomalyDetection,
        shuffle: bool = True,
    ):
        self.dataset = dataset
        self.model = model
        self.shuffler = None
        if shuffle:
            self.shuffler = list(range(len(dataset)))
            random.shuffle(self.shuffler)

    def __getitem__(self, index: int):
        """Get dataset item.

        Args:
            index (int): Index of the dataset sample.

        Returns:
            Dataset item.
        """
        if self.shuffler is not None:
            index = self.shuffler[index]

        image = self.dataset[index].numpy
        annotation = self.dataset[index].annotation_scene

        resized_image = self.model.resize(image, (self.model.w, self.model.h))
        resized_image = self.model.input_transform(resized_image)
        resized_image = self.model._change_layout(resized_image)

        return (index, annotation), resized_image

    def __len__(self) -> int:
        """Get size of the dataset.

        Returns:
            int: Size of the dataset.
        """
        return len(self.dataset)


class OpenVINOTask(IInferenceTask, IEvaluationTask, IOptimizationTask, IDeploymentTask):
    """OpenVINO inference task.

    Args:
        task_environment (TaskEnvironment): task environment of the trained anomaly model
    """

    def __init__(self, task_environment: TaskEnvironment) -> None:
        logger.info("Initializing the OpenVINO task.")
        self.task_environment = task_environment
        self.task_type = self.task_environment.model_template.task_type
        self.config = self.get_config()
        self.inference_model = self.get_openvino_model()

        labels = self.task_environment.get_labels()
        self.normal_label = [label for label in labels if not label.is_anomalous][0]
        self.anomalous_label = [label for label in labels if label.is_anomalous][0]

        template_file_path = task_environment.model_template.model_template_path
        self._base_dir = os.path.abspath(os.path.dirname(template_file_path))

    def get_config(self) -> ADDict:
        """Get Anomalib Config from task environment.

        Returns:
            ADDict: Anomalib config
        """
        task_name = self.task_environment.model_template.name
        otx_config: ConfigurableParameters = self.task_environment.get_hyper_parameters()
        config = get_anomalib_config(task_name=task_name, otx_config=otx_config)
        return ADDict(OmegaConf.to_container(config))

    def infer(self, dataset: DatasetEntity, inference_parameters: InferenceParameters) -> DatasetEntity:
        """Perform Inference.

        Args:
            dataset (DatasetEntity): Inference dataset
            inference_parameters (InferenceParameters): Inference parameters.

        Returns:
            DatasetEntity: Output dataset storing inference predictions.
        """
        if self.task_environment.model is None:
            raise Exception("task_environment.model is None. Cannot access threshold to calculate labels.")

        logger.info("Start OpenVINO inference.")
        update_progress_callback = default_progress_callback
        if inference_parameters is not None:
            update_progress_callback = inference_parameters.update_progress  # type: ignore

        for idx, dataset_item in enumerate(dataset):
            image_result: AnomalyResult = self.inference_model(dataset_item.numpy)

            # TODO: inferencer should return predicted label and mask
            pred_label = image_result.pred_label
            pred_mask = image_result.pred_mask
            probability = image_result.pred_score if pred_label == "Anomaly" else 1 - image_result.pred_score
            if self.task_type == TaskType.ANOMALY_CLASSIFICATION:
                label = self.anomalous_label if image_result.pred_score >= 0.5 else self.normal_label
            elif self.task_type == TaskType.ANOMALY_SEGMENTATION:
                annotations = create_annotation_from_segmentation_map(
                    pred_mask,
                    image_result.anomaly_map.squeeze() / 255.0,
                    {0: self.normal_label, 1: self.anomalous_label},
                )
                dataset_item.append_annotations(annotations)
                label = self.normal_label if len(annotations) == 0 else self.anomalous_label
            elif self.task_type == TaskType.ANOMALY_DETECTION:
                annotations = create_detection_annotation_from_anomaly_heatmap(
                    pred_mask,
                    image_result.anomaly_map.squeeze() / 255.0,
                    {0: self.normal_label, 1: self.anomalous_label},
                )
                dataset_item.append_annotations(annotations)
                label = self.normal_label if len(annotations) == 0 else self.anomalous_label
            else:
                raise ValueError(f"Unknown task type: {self.task_type}")

            dataset_item.append_labels([ScoredLabel(label=label, probability=float(probability))])
            heatmap_media = ResultMediaEntity(
                name="Anomaly Map",
                type="anomaly_map",
                label=label,
                annotation_scene=dataset_item.annotation_scene,
                numpy=image_result.anomaly_map,
            )
            dataset_item.append_metadata_item(heatmap_media)
            update_progress_callback(int((idx + 1) / len(dataset) * 100))

        return dataset

    def get_metadata(self) -> Dict:
        """Get Meta Data."""
        metadata = {}
        if self.task_environment.model is not None:
            try:
                metadata = json.loads(self.task_environment.model.get_data("metadata").decode())
                self._populate_metadata(metadata)
                logger.info("Metadata loaded from model v1.4.")
            except (KeyError, json.decoder.JSONDecodeError):
                # model is from version 1.2.x
                metadata = self._populate_metadata_legacy(self.task_environment.model)
                logger.info("Metadata loaded from model v1.2.x.")
        else:
            raise ValueError("Cannot access meta-data. self.task_environment.model is empty.")

        return metadata

    def _populate_metadata_legacy(self, model: ModelEntity) -> Dict[str, Any]:
        """Populates metadata for models for version 1.2.x."""
        image_threshold = np.frombuffer(model.get_data("image_threshold"), dtype=np.float32)
        pixel_threshold = np.frombuffer(model.get_data("pixel_threshold"), dtype=np.float32)
        min_value = np.frombuffer(model.get_data("min"), dtype=np.float32)
        max_value = np.frombuffer(model.get_data("max"), dtype=np.float32)
        transform = get_transforms(
            config=self.config.dataset.transform_config.train,
            image_size=tuple(self.config.dataset.image_size),
            to_tensor=True,
        )
        metadata = {
            # TODO: Replace with transform.to_dict() when OTX supports albumentations 1.3.0
            "transform": {"transform": transform._to_dict()},
            "image_threshold": image_threshold,
            "pixel_threshold": pixel_threshold,
            "min": min_value,
            "max": max_value,
            "task": str(self.task_type).lower().split("_")[-1],
        }
        return metadata

    def _populate_metadata(self, metadata: Dict[str, Any]):
        """Populates metadata for models from version 1.4 onwards."""
        metadata["image_threshold"] = np.array(metadata["image_threshold"], dtype=np.float32).item()
        metadata["pixel_threshold"] = np.array(metadata["pixel_threshold"], dtype=np.float32).item()
        metadata["min"] = np.array(metadata["min"], dtype=np.float32).item()
        metadata["max"] = np.array(metadata["max"], dtype=np.float32).item()

    def evaluate(self, output_resultset: ResultSetEntity, evaluation_metric: Optional[str] = None):
        """Evaluate the performance of the model.

        Args:
            output_resultset (ResultSetEntity): Result set storing ground truth and predicted dataset.
            evaluation_metric (Optional[str], optional): Evaluation metric. Defaults to None.
        """
        metric: IPerformanceProvider
        if self.task_type == TaskType.ANOMALY_CLASSIFICATION:
            metric = MetricsHelper.compute_f_measure(output_resultset)
        elif self.task_type == TaskType.ANOMALY_DETECTION:
            metric = MetricsHelper.compute_anomaly_detection_scores(output_resultset)
        elif self.task_type == TaskType.ANOMALY_SEGMENTATION:
            metric = MetricsHelper.compute_anomaly_segmentation_scores(output_resultset)
        else:
            raise ValueError(f"Unknown task type: {self.task_type}")
        output_resultset.performance = metric.get_performance()

    def _get_optimization_algorithms_config(self) -> ADDict:
        """Returns list of optimization algorithms configurations."""
        hparams: BaseAnomalyConfig = self.task_environment.get_hyper_parameters()

        optimization_config_path = os.path.join(self._base_dir, "ptq_optimization_config.py")
        ptq_config = ADDict()
        if os.path.exists(optimization_config_path):
            ptq_config = read_py_config(optimization_config_path)
        ptq_config.update(
            subset_size=hparams.pot_parameters.stat_subset_size,
            preset=QuantizationPreset(hparams.pot_parameters.preset.name.lower()),
        )

        return ptq_config

    def optimize(
        self,
        optimization_type: OptimizationType,
        dataset: DatasetEntity,
        output_model: ModelEntity,
        optimization_parameters: Optional[OptimizationParameters],
    ):
        """Optimize the model.

        Args:
            optimization_type (OptimizationType): Type of optimization [POT or NNCF]
            dataset (DatasetEntity): Input Dataset.
            output_model (ModelEntity): Output model.
            optimization_parameters (Optional[OptimizationParameters]): Optimization parameters.

        Raises:
            ValueError: When the optimization type is not POT, which is the only support type at the moment.
        """
        if optimization_type is not OptimizationType.POT:
            raise ValueError("PTQ is the only supported optimization type for OpenVINO models")

        # Training subset does not contain example of anomalous images.
        # Anomalous examples from all dataset used to get statistics for quantization.
        dataset = DatasetEntity(
            items=[item for item in dataset if item.get_shapes_labels()[0].is_anomalous], purpose=dataset.purpose
        )

        logger.info("Starting PTQ optimization.")
        data_loader = OTXNNCFAnomalyDataloader(dataset=dataset, model=self.inference_model)
        quantization_dataset = nncf.Dataset(data_loader, lambda data: data[1])

        with tempfile.TemporaryDirectory() as tempdir:
            xml_path = os.path.join(tempdir, "model.xml")
            bin_path = os.path.join(tempdir, "model.bin")

            if self.task_environment.model is not None:
                self.__save_weights(xml_path, self.task_environment.model.get_data("openvino.xml"))
                self.__save_weights(bin_path, self.task_environment.model.get_data("openvino.bin"))
            else:
                raise ValueError("Cannot save the weights. self.task_environment.model is None.")

            ov_model = ov.Core().read_model(xml_path)
            if check_if_quantized(ov_model):
                raise RuntimeError("Model is already optimized by PTQ")

        if optimization_parameters is not None:
            optimization_parameters.update_progress(10, None)

        quantization_config = self._get_optimization_algorithms_config()
        quantization_config.subset_size = min(quantization_config.subset_size, len(data_loader))

        compressed_model = nncf.quantize(ov_model, quantization_dataset, **quantization_config)

        if optimization_parameters is not None:
            optimization_parameters.update_progress(90, None)

        with tempfile.TemporaryDirectory() as tempdir:
            xml_path = os.path.join(tempdir, "model.xml")
            ov.save_model(compressed_model, xml_path)
            self.__load_weights(path=xml_path, output_model=output_model, key="openvino.xml")
            self.__load_weights(path=os.path.join(tempdir, "model.bin"), output_model=output_model, key="openvino.bin")

        output_model.set_data("label_schema.json", label_schema_to_bytes(self.task_environment.label_schema))
        output_model.model_format = ModelFormat.OPENVINO
        output_model.optimization_type = ModelOptimizationType.POT
        output_model.optimization_methods = [OptimizationMethod.QUANTIZATION]
        output_model.precision = [ModelPrecision.INT8]
        metadata = self.get_metadata()
        output_model.set_data("metadata", json.dumps(metadata).encode())

        self.task_environment.model = output_model
        self.inference_model = self.get_openvino_model()

        if optimization_parameters is not None:
            optimization_parameters.update_progress(100, None)
        logger.info("PTQ optimization completed")

    def get_openvino_model(self) -> AnomalyDetection:
        """Create the OpenVINO inferencer object.

        Returns:
            AnomalyDetection model
        """
        if self.task_environment.model is None:
            raise Exception("task_environment.model is None. Cannot load weights.")
        try:
            model = AnomalyDetection.create_model(
                model=self.task_environment.model.get_data("openvino.xml"),
                weights_path=self.task_environment.model.get_data("openvino.bin"),
            )
        except RuntimeError as exception:
            logger.exception(exception)
            logger.info("Possibly a legacy model is being loaded.")
            self._create_from_legacy()
            model = AnomalyDetection.create_model(
                model=self.task_environment.model.get_data("openvino.xml"),
                weights_path=self.task_environment.model.get_data("openvino.bin"),
            )

        return model

    def _create_from_legacy(self) -> None:
        """Generates an OpenVINO model in new format from the legacy model.

        TODO: This needs to be removed once all projects in Geti have been migrated to the newer version.

        Args:
            model_file (str): The XML model file.
        """
        extra_model_data = self._metadata_in_ir_format()

        for key, value in extra_model_data.items():
            if isinstance(value, np.ndarray):
                extra_model_data[key] = value.tolist()

        with tempfile.TemporaryDirectory() as temp_dir:
            xml_data = self.task_environment.model.get_data("openvino.xml")
            bin_data = self.task_environment.model.get_data("openvino.bin")
            with open(f"{temp_dir}/openvino.xml", "wb") as file:
                file.write(xml_data)
            with open(f"{temp_dir}/openvino.bin", "wb") as file:
                file.write(bin_data)
            embed_ir_model_data(f"{temp_dir}/openvino.xml", extra_model_data)
            with open(f"{temp_dir}/openvino.xml", "rb") as file:
                self.task_environment.model.set_data("openvino.xml", file.read())
            with open(f"{temp_dir}/openvino.bin", "rb") as file:
                self.task_environment.model.set_data("openvino.bin", file.read())

    def _metadata_in_ir_format(self) -> Dict[Tuple[str, str], Union[str, int, float, List[Union[int, float]]]]:
        """Return metadata in format of tuple keys that are used in IR with modelAPI."""
        metadata = self.get_metadata()
        extra_model_data: Dict[Tuple[str, str], Any] = {}
        for key, value in metadata.items():
            if key in ("transform", "min", "max"):
                continue
            extra_model_data[("model_info", key)] = value
        # Add transforms
        if "transform" in metadata:
            for transform_dict in metadata["transform"]["transform"]["transforms"]:
                transform = transform_dict.pop("__class_fullname__")
                if transform == "Normalize":
                    extra_model_data[("model_info", "mean_values")] = self._serialize_list(
                        [x * 255.0 for x in transform_dict["mean"]]
                    )
                    extra_model_data[("model_info", "scale_values")] = self._serialize_list(
                        [x * 255.0 for x in transform_dict["std"]]
                    )
                elif transform == "Resize":
                    extra_model_data[("model_info", "orig_height")] = transform_dict["height"]
                    extra_model_data[("model_info", "orig_width")] = transform_dict["width"]
                else:
                    logger.warn(f"Transform {transform} is not supported currently")
        # Since we only need the diff of max and min, we fuse the min and max into one op
        if "min" in metadata and "max" in metadata:
            extra_model_data[("model_info", "normalization_scale")] = metadata["max"] - metadata["min"]

        extra_model_data[("model_info", "reverse_input_channels")] = False
        extra_model_data[("model_info", "model_type")] = "AnomalyDetection"
        extra_model_data[("model_info", "labels")] = "Normal Anomaly"
        return extra_model_data

    def _serialize_list(self, arr: Union[Tuple, List]) -> str:
        """Converts a list to space separated string."""
        return " ".join(map(str, arr))

    @staticmethod
    def __save_weights(path: str, data: bytes) -> None:
        """Write data to file.

        Args:
            path (str): Path of output file
            data (bytes): Data to write
        """
        with open(path, "wb") as file:
            file.write(data)

    @staticmethod
    def __load_weights(path: str, output_model: ModelEntity, key: str) -> None:
        """Load weights into output model.

        Args:
            path (str): Path to weights
            output_model (ModelEntity): Model to which the weights are assigned
            key (str): Key of the output model into which the weights are assigned
        """
        with open(path, "rb") as file:
            output_model.set_data(key, file.read())

    def _get_openvino_configuration(self) -> Dict[str, Any]:
        """Return configuration required by the exported model."""
        if self.task_environment.model is None:
            raise Exception("task_environment.model is None. Cannot get configuration.")

        configuration: Dict[str, Any] = {
            "labels": LabelSchemaMapper.forward(self.task_environment.label_schema),
        }
        # Add new IR keys to parameters
        for key, value in self._metadata_in_ir_format().items():
            # since the same key is used to store label info in OTX SDK format
            if key[1] == "labels":
                assert isinstance(value, str)
                configuration["modelapi_labels"] = [name for name in value.split(" ")]
            elif key[1] in ("mean_values", "scale_values"):
                assert isinstance(value, str)
                configuration[key[1]] = [float(x) for x in value.split(" ")]
            else:
                configuration[key[1]] = value

        return configuration

    def deploy(self, output_model: ModelEntity) -> None:
        """Exports the weights from ``output_model`` along with exportable code.

        Args:
            output_model (ModelEntity): Model with ``openvino.xml`` and ``.bin`` keys

        Raises:
            Exception: If ``task_environment.model`` is None
        """
        logger.info("Deploying Model")

        if self.task_environment.model is None:
            raise Exception("task_environment.model is None. Cannot load weights.")

        work_dir = os.path.dirname(demo.__file__)
        parameters: Dict[str, Any] = {}

        task_type = str(self.task_type).lower()

        parameters["type_of_model"] = "AnomalyDetection"
        parameters["converter_type"] = task_type.upper()
        parameters["model_parameters"] = self._get_openvino_configuration()
        zip_buffer = io.BytesIO()
        with ZipFile(zip_buffer, "w") as arch:
            # model files
            arch.writestr(os.path.join("model", "model.xml"), self.task_environment.model.get_data("openvino.xml"))
            arch.writestr(os.path.join("model", "model.bin"), self.task_environment.model.get_data("openvino.bin"))
            arch.writestr(os.path.join("model", "config.json"), json.dumps(parameters, ensure_ascii=False, indent=4))
            # other python files
            arch.write(os.path.join(work_dir, "requirements.txt"), os.path.join("python", "requirements.txt"))
            arch.write(os.path.join(work_dir, "LICENSE"), os.path.join("python", "LICENSE"))
            arch.write(os.path.join(work_dir, "demo.py"), os.path.join("python", "demo.py"))
            arch.write(os.path.join(work_dir, "README.md"), os.path.join(".", "README.md"))
        output_model.exportable_code = zip_buffer.getvalue()
        logger.info("Deployment completed.")
