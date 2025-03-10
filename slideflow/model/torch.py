'''PyTorch backend for the slideflow.model submodule.'''

import inspect
import json
import os
import types
from collections import defaultdict
from os.path import join
from typing import (TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple,
                    Union)

import numpy as np
import pretrainedmodels
import slideflow as sf
import slideflow.util.neptune_utils
import torchvision
from slideflow import errors
from slideflow.model import base as _base
from slideflow.model import torch_utils
from slideflow.model.base import log_manifest, no_scope
from slideflow.util import NormFit, Path
from slideflow.util import colors as col
from slideflow.util import log
from tqdm import tqdm

import torch
from torch import Tensor
from torch.utils.tensorboard import SummaryWriter

if TYPE_CHECKING:
    import pandas as pd
    from slideflow.norm import StainNormalizer


class LinearBlock(torch.nn.Module):
    '''Block module that includes a linear layer -> ReLU -> BatchNorm'''

    def __init__(
        self,
        in_ftrs: int,
        out_ftrs: int,
        dropout: Optional[float] = None
    ) -> None:
        super().__init__()
        self.in_ftrs = in_ftrs
        self.out_ftrs = out_ftrs
        self.linear = torch.nn.Linear(in_ftrs, out_ftrs)
        self.relu = torch.nn.ReLU(inplace=True)
        self.bn = torch.nn.BatchNorm1d(out_ftrs)
        if dropout:
            self.dropout = torch.nn.Dropout(dropout)
        else:
            self.dropout = None  # type: ignore

    def forward(self, x: Tensor) -> Tensor:
        x = self.linear(x)
        x = self.relu(x)
        x = self.bn(x)
        if self.dropout is not None:
            x = self.dropout(x)
        return x


class ModelWrapper(torch.nn.Module):
    '''Wrapper for PyTorch modules to support multiple outcomes, clinical
    (patient-level) inputs, and additional hidden layers.'''

    def __init__(
        self,
        model: Any,
        n_classes: List[int],
        num_slide_features: int = 0,
        hidden_layers: Optional[List[int]] = None,
        drop_images: bool = False,
        dropout: Optional[float] = None,
        include_top: bool = True
    ) -> None:
        super().__init__()
        self.model = model
        self.n_classes = len(n_classes)
        self.drop_images = drop_images
        self.num_slide_features = num_slide_features
        self.num_hidden_layers = 0 if not hidden_layers else len(hidden_layers)
        self.has_aux = False
        log.debug(f'Model class name: {model.__class__.__name__}')
        if not drop_images:
            # Check for auxillary classifier
            if model.__class__.__name__ in ('Inception3',):
                log.debug("Auxillary classifier detected")
                self.has_aux = True

            # Get the last linear layer prior to the logits layer
            if model.__class__.__name__ in ('Xception', 'NASNetALarge'):
                num_ftrs = self.model.last_linear.in_features
                self.model.last_linear = torch.nn.Identity()
            elif model.__class__.__name__ in ('SqueezeNet'):
                num_ftrs = 1000
            elif hasattr(self.model, 'classifier'):
                children = list(self.model.classifier.named_children())
                if len(children):
                    # VGG, AlexNet
                    if include_top:
                        log.debug("Including existing fully-connected "
                                  "top classifier layers")
                        last_linear_name, last_linear = children[-1]
                        num_ftrs = last_linear.in_features
                        setattr(
                            self.model.classifier,
                            last_linear_name,
                            torch.nn.Identity()
                        )
                    elif model.__class__.__name__ in ('AlexNet',
                                                      'MobileNetV2',
                                                      'MNASNet'):
                        log.debug("Removing fully-connected classifier layers")
                        _, first_classifier = children[1]
                        num_ftrs = first_classifier.in_features
                        self.model.classifier = torch.nn.Identity()
                    elif model.__class__.__name__ in ('VGG', 'MobileNetV3'):
                        log.debug("Removing fully-connected classifier layers")
                        _, first_classifier = children[0]
                        num_ftrs = first_classifier.in_features
                        self.model.classifier = torch.nn.Identity()
                else:
                    num_ftrs = self.model.classifier.in_features
                    self.model.classifier = torch.nn.Identity()
            elif hasattr(self.model, 'fc'):
                num_ftrs = self.model.fc.in_features
                self.model.fc = torch.nn.Identity()
            elif hasattr(self.model, 'out_features'):
                num_ftrs = self.model.out_features
            else:
                raise errors.ModelError("Unable to find last linear layer for "
                                        f"model {model.__class__.__name__}")
        else:
            num_ftrs = 0

        # Add slide-level features
        num_ftrs += num_slide_features

        # Add hidden layers
        if hidden_layers:
            hl_ftrs = [num_ftrs] + hidden_layers
            for i in range(len(hidden_layers)):
                setattr(self, f'h{i}', LinearBlock(hl_ftrs[i],
                                                   hl_ftrs[i+1],
                                                   dropout=dropout))
            num_ftrs = hidden_layers[-1]

        # Add the outcome/logits layers for each outcome, if multiple outcomes
        for i, n in enumerate(n_classes):
            setattr(self, f'fc{i}', torch.nn.Linear(num_ftrs, n))

    def __getattr__(self, name: str) -> Any:
        try:
            return super().__getattr__(name)
        except AttributeError as e:
            if name == 'model':
                raise e
            return getattr(self.model, name)

    def forward(
        self,
        img: Tensor,
        slide_features: Optional[Tensor] = None
    ):
        if slide_features is None and self.num_slide_features:
            raise ValueError("Expected 2 inputs, got 1")

        # Last linear of core convolutional model
        if not self.drop_images:
            x = self.model(img)

        # Discard auxillary classifier
        if self.has_aux:
            x, _ = x

        # Merging image data with any slide-level input data
        if self.num_slide_features and not self.drop_images:
            assert slide_features is not None
            x = torch.cat([x, slide_features], dim=1)
        elif self.num_slide_features:
            x = slide_features

        # Hidden layers
        if self.num_hidden_layers:
            x = self.h0(x)
        if self.num_hidden_layers > 1:
            for h in range(1, self.num_hidden_layers):
                x = getattr(self, f'h{h}')(x)

        # Return a list of outputs if we have multiple outcomes
        if self.n_classes > 1:
            out = [getattr(self, f'fc{i}')(x) for i in range(self.n_classes)]

        # Otherwise, return the single output
        else:
            out = self.fc0(x)

        return out  # , x


class ModelParams(_base._ModelParams):
    """Build a set of hyperparameters."""

    def __init__(self, loss: str = 'CrossEntropy', **kwargs) -> None:
        self.OptDict = {
            'Adadelta': torch.optim.Adadelta,
            'Adagrad': torch.optim.Adagrad,
            'Adam': torch.optim.Adam,
            'AdamW': torch.optim.AdamW,
            'SparseAdam': torch.optim.SparseAdam,
            'Adamax': torch.optim.Adamax,
            'ASGD': torch.optim.ASGD,
            'LBFGS': torch.optim.LBFGS,
            'RMSprop': torch.optim.RMSprop,
            'Rprop': torch.optim.Rprop,
            'SGD': torch.optim.SGD
        }
        self.ModelDict = {
            'resnet18': torchvision.models.resnet18,
            'resnet50': torchvision.models.resnet50,
            'alexnet': torchvision.models.alexnet,
            'squeezenet': torchvision.models.squeezenet.squeezenet1_1,
            'densenet': torchvision.models.densenet161,
            'inception': torchvision.models.inception_v3,
            'googlenet': torchvision.models.googlenet,
            'shufflenet': torchvision.models.shufflenet_v2_x1_0,
            'resnext50_32x4d': torchvision.models.resnext50_32x4d,
            'vgg16': torchvision.models.vgg16,  # needs support added
            'mobilenet_v2': torchvision.models.mobilenet_v2,
            'mobilenet_v3_small': torchvision.models.mobilenet_v3_small,
            'mobilenet_v3_large': torchvision.models.mobilenet_v3_large,
            'wide_resnet50_2': torchvision.models.wide_resnet50_2,
            'mnasnet': torchvision.models.mnasnet1_0,
            'xception': pretrainedmodels.xception,
            'nasnet_large': pretrainedmodels.nasnetalarge
        }
        self.LinearLossDict = {
            'L1': torch.nn.L1Loss,
            'MSE': torch.nn.MSELoss,
            'NLL': torch.nn.NLLLoss,  # negative log likelihood
            'HingeEmbedding': torch.nn.HingeEmbeddingLoss,
            'SmoothL1': torch.nn.SmoothL1Loss,
            'CosineEmbedding': torch.nn.CosineEmbeddingLoss,
        }
        self.AllLossDict = {
            'CrossEntropy': torch.nn.CrossEntropyLoss,
            'CTC': torch.nn.CTCLoss,
            'PoissonNLL': torch.nn.PoissonNLLLoss,
            'GaussianNLL': torch.nn.GaussianNLLLoss,
            'KLDiv': torch.nn.KLDivLoss,
            'BCE': torch.nn.BCELoss,
            'BCEWithLogits': torch.nn.BCEWithLogitsLoss,
            'MarginRanking': torch.nn.MarginRankingLoss,
            'MultiLabelMargin': torch.nn.MultiLabelMarginLoss,
            'Huber': torch.nn.HuberLoss,
            'SoftMargin': torch.nn.SoftMarginLoss,
            'MultiLabelSoftMargin': torch.nn.MultiLabelSoftMarginLoss,
            'MultiMargin': torch.nn.MultiMarginLoss,
            'TripletMargin': torch.nn.TripletMarginLoss,
            'TripletMarginWithDistance': torch.nn.TripletMarginWithDistanceLoss,
            'L1': torch.nn.L1Loss,
            'MSE': torch.nn.MSELoss,
            'NLL': torch.nn.NLLLoss,  # negative log likelihood
            'HingeEmbedding': torch.nn.HingeEmbeddingLoss,
            'SmoothL1': torch.nn.SmoothL1Loss,
            'CosineEmbedding': torch.nn.CosineEmbeddingLoss,
        }
        super().__init__(loss=loss, **kwargs)
        assert self.model in self.ModelDict.keys()
        assert self.optimizer in self.OptDict.keys()
        assert self.loss in self.AllLossDict
        if isinstance(self.augment, str) and 'b' in self.augment:
            log.warn('Gaussian blur not yet optimized in PyTorch backend; '
                     'image pre-processing may be slow.')

    def get_opt(self, params_to_update: Iterable) -> torch.optim.Optimizer:
        return self.OptDict[self.optimizer](
            params_to_update,
            lr=self.learning_rate
        )

    def get_loss(self) -> torch.nn.modules.loss._Loss:
        return self.AllLossDict[self.loss]()

    def build_model(
        self,
        labels: Optional[Dict] = None,
        num_classes: Optional[Union[int, Dict[Any, int]]] = None,
        num_slide_features: int = 0,
        pretrain: Optional[str] = None,
        checkpoint: Optional[str] = None
    ) -> torch.nn.Module:
        assert num_classes is not None or labels is not None
        if num_classes is None:
            assert labels is not None
            num_classes = self._detect_classes_from_labels(labels)
        if not isinstance(num_classes, dict):
            num_classes = {'out-0': num_classes}

        # Build base model
        if self.model in ('xception', 'nasnet_large'):
            _model = self.ModelDict[self.model](
                num_classes=1000,
                pretrained=pretrain
            )
        else:
            model_fn = self.ModelDict[self.model]
            # Only pass kwargs accepted by model function
            model_fn_sig = inspect.signature(model_fn)
            model_kw = [
                param.name
                for param in model_fn_sig.parameters.values()
                if param.kind == param.POSITIONAL_OR_KEYWORD
            ]
            if 'image_size' in model_kw:
                _model = model_fn(pretrained=pretrain, image_size=self.tile_px)
            else:
                _model = model_fn(pretrained=pretrain)

        # Add final layers to models
        hidden_layers = [
            self.hidden_layer_width
            for _ in range(self.hidden_layers)
        ]
        model = ModelWrapper(
            _model,
            list(num_classes.values()),
            num_slide_features,
            hidden_layers,
            self.drop_images,
            dropout=self.dropout,
            include_top=self.include_top
        )
        if checkpoint is not None:
            model.load_state_dict(torch.load(checkpoint))
        return model

    def model_type(self) -> str:
        if self.loss == 'NLL':
            return 'cph'
        elif self.loss in self.LinearLossDict:
            return 'linear'
        else:
            return 'categorical'


class Trainer:
    """Base trainer class containing functionality for model building, input
    processing, training, and evaluation.

    This base class requires categorical outcome(s). Additional outcome types
    are supported by :class:`slideflow.model.LinearTrainer` and
    :class:`slideflow.model.CPHTrainer`.

    Slide-level (e.g. clinical) features can be used as additional model input
    by providing slide labels in the slide annotations dictionary, under
    the key 'input'.
    """

    _model_type = 'categorical'

    def __init__(
        self,
        hp: ModelParams,
        outdir: str,
        labels: Dict[str, Any],
        patients: Dict[str, str],
        slide_input: Optional[Dict[str, Any]] = None,
        name: str = 'Trainer',
        manifest: Optional[Dict[str, int]] = None,
        feature_sizes: Optional[List[int]] = None,
        feature_names: Optional[List[str]] = None,
        outcome_names: Optional[List[str]] = None,
        mixed_precision: bool = True,
        config: Dict[str, Any] = None,
        use_neptune: bool = False,
        neptune_api: Optional[str] = None,
        neptune_workspace: Optional[str] = None
    ):
        """Sets base configuration, preparing model inputs and outputs.

        Args:
            hp (:class:`slideflow.model.ModelParams`): ModelParams object.
            outdir (str): Destination for event logs and checkpoints.
            labels (dict): Dict mapping slide names to outcome labels (int or
                float format).
            patients (dict): Dict mapping slide names to patient ID, as some
                patients may have multiple slides. If not provided, assumes
                1:1 mapping between slide names and patients.
            slide_input (dict): Dict mapping slide names to additional
                slide-level input, concatenated after post-conv.
            name (str, optional): Optional name describing the model, used for
                model saving. Defaults to None.
            manifest (dict, optional): Manifest dictionary mapping TFRecords to
                number of tiles. Defaults to None.
            model_type (str, optional): Type of model outcome, 'categorical' or
                'linear'. Defaults to 'categorical'.
            feature_sizes (list, optional): List of sizes of input features.
                Required if providing additional input features as model input.
            feature_names (list, optional): List of names for input features.
            Used when permuting feature importance.
            outcome_names (list, optional): Name of each outcome. Defaults to
                "Outcome {X}" for each outcome.
            mixed_precision (bool, optional): Use FP16 mixed precision (rather
                than FP32). Defaults to True.
            config (dict, optional): Training configuration dictionary, used
                for logging. Defaults to None.
            use_neptune (bool, optional): Use Neptune API logging.
                Defaults to False
            neptune_api (str, optional): Neptune API token, used for logging.
                Defaults to None.
            neptune_workspace (str, optional): Neptune workspace.
                Defaults to None.
        """
        self.hp = hp
        self.outdir = outdir
        self.labels = labels
        self.patients = patients
        self.name = name
        self.manifest = manifest
        self.model = None  # type: Optional[torch.nn.Module]
        self.inference_model = None  # type: Optional[torch.nn.Module]
        self.mixed_precision = mixed_precision
        self.device = torch.device('cuda:0')
        self.mid_train_val_dts: Optional[Iterable]
        self.loss_fn: torch.nn.modules.loss._Loss
        self.use_tensorboard: bool
        self.writer: SummaryWriter
        self._reset_training_params()

        # Slide-level input args
        if slide_input:
            self.slide_input = {
                k: [float(vi) for vi in v]
                for k, v in slide_input.items()
            }
        else:
            self.slide_input = None  # type: ignore
        self.feature_names = feature_names
        self.feature_sizes = feature_sizes
        self.num_slide_features = 0 if not feature_sizes else sum(feature_sizes)

        self.normalizer = self.hp.get_normalizer()
        if self.normalizer:
            log.info(f'Using realtime {self.hp.normalizer} normalization')
        outcome_labels = np.array(list(labels.values()))
        if len(outcome_labels.shape) == 1:
            outcome_labels = np.expand_dims(outcome_labels, axis=1)
        if not outcome_names:
            self.outcome_names = [
                f'Outcome {i}'
                for i in range(outcome_labels.shape[1])
            ]
        else:
            self.outcome_names = outcome_names
        if not len(self.outcome_names) == outcome_labels.shape[1]:
            n_names = len(self.outcome_names)
            n_out = outcome_labels.shape[1]
            raise errors.ModelError(f"Number of outcome names ({n_names}) does"
                                    f" not match number of outcomes ({n_out})")
        if not os.path.exists(outdir):
            os.makedirs(outdir)

        # Log parameters
        if config is None:
            config = {
                'slideflow_version': sf.__version__,
                'hp': self.hp.get_dict(),
                'backend': sf.backend()
            }
        sf.util.write_json(config, join(self.outdir, 'params.json'))

        # Neptune logging
        self.config = config
        self.use_neptune = use_neptune
        self.neptune_run = None
        if self.use_neptune:
            if neptune_api is None or neptune_workspace is None:
                raise ValueError("If using Neptune, must supply neptune_api"
                                 " and neptune_workspace.")
            self.neptune_logger = sf.util.neptune_utils.NeptuneLog(
                neptune_api,
                neptune_workspace
            )

    @property
    def num_outcomes(self) -> int:
        if self.hp.model_type() == 'categorical':
            assert self.outcome_names is not None
            return len(self.outcome_names)
        else:
            return 1

    @property
    def multi_outcome(self) -> bool:
        return (self.num_outcomes > 1)

    def _reset_training_params(self) -> None:
        self.global_step = 0
        self.epoch = 0  # type: int
        self.step = 0  # type: int
        self.log_frequency = 0  # type: int
        self.early_stop = False  # type: bool
        self.moving_average = []  # type: List
        self.dataloaders = {}  # type: Dict[str, Any]
        self.validation_batch_size = None  # type: Optional[int]
        self.validate_on_batch = 0
        self.validation_steps = 0
        self.ema_observations = 0  # type: int
        self.ema_smoothing = 0
        self.last_ema = -1  # type: float
        self.ema_one_check_prior = -1  # type: float
        self.ema_two_checks_prior = -1  # type: float
        self.epoch_records = 0  # type: int
        self.running_loss = 0.0
        self.phase = None  # type: Optional[str]
        self.running_corrects = {}  # type: Union[Tensor, Dict[str, Tensor]]

    def _accuracy_as_numpy(
        self,
        acc: Union[Tensor, float, List[Tensor], List[float]]
    ) -> Union[float, List[float]]:
        if isinstance(acc, list):
            return [t.item() if isinstance(t, Tensor) else t for t in acc]
        else:
            return (acc.item() if isinstance(acc, Tensor) else acc)

    def _build_model(
        self,
        checkpoint: Optional[str] = None,
        pretrain: Optional[str] = None
    ) -> None:
        if checkpoint:
            log.info(f"Loading checkpoint at {col.green(checkpoint)}")
            self.load(checkpoint)
        else:
            self.model = self.hp.build_model(
                labels=self.labels,
                pretrain=pretrain,
                num_slide_features=self.num_slide_features
            )
        # Create an inference model before any multi-GPU parallelization
        # is applied to the self.model parameter
        self.inference_model = self.model

    def _calculate_accuracy(
        self,
        running_corrects: Union[Tensor, Dict[Any, Tensor]],
        num_records: int = 1
    ) -> Tuple[Union[Tensor, List[Tensor]], str]:
        '''Reports accuracy of each outcome.'''
        assert self.hp.model_type() == 'categorical'
        if self.num_outcomes > 1:
            if not isinstance(running_corrects, dict):
                raise ValueError("Expected running_corrects to be a dict:"
                                 " num_outcomes is > 1")
            acc_desc = ''
            acc_list = [running_corrects[r] / num_records
                        for r in running_corrects]
            for o in range(len(running_corrects)):
                _acc = running_corrects[f'out-{o}'] / num_records
                acc_desc += f"out-{o} acc: {_acc:.4f} "
            return acc_list, acc_desc
        else:
            assert not isinstance(running_corrects, dict)
            _acc = running_corrects / num_records
            return _acc, f'acc: {_acc:.4f}'

    def _calculate_loss(
        self,
        outputs: Union[Tensor, List[Tensor]],
        labels: Union[Tensor, Dict[Any, Tensor]],
        loss_fn: torch.nn.modules.loss._Loss
    ) -> Tensor:
        '''Calculates loss in a manner compatible with multiple outcomes.'''
        if self.num_outcomes > 1:
            if not isinstance(labels, dict):
                raise ValueError("Expected labels to be a dict: num_outcomes"
                                 " is > 1")
            loss = sum([
                loss_fn(out, labels[f'out-{o}'])
                for o, out in enumerate(outputs)
            ])
        else:
            loss = loss_fn(outputs, labels)
        return loss  # type: ignore

    def _check_early_stopping(
        self,
        val_acc: Optional[Union[float, List[float]]] = None,
        val_loss: Optional[float] = None
    ) -> str:
        if val_acc is None and val_loss is None:
            if (self.hp.early_stop
               and self.hp.early_stop_method == 'manual'
               and self.hp.manual_early_stop_epoch <= self.epoch  # type: ignore
               and self.hp.manual_early_stop_batch <= self.step):  # type: ignore
                log.info(f'Manual early stop triggered: epoch {self.epoch}, '
                         f'batch {self.step}')
                if self.epoch not in self.hp.epochs:
                    self.hp.epochs += [self.epoch]
                self.early_stop = True
        else:
            if self.hp.early_stop_method == 'accuracy':
                if self.num_outcomes > 1:
                    raise errors.ModelError(
                        "Early stopping method 'accuracy' not supported with"
                        " multiple outcomes; use 'loss'.")
                early_stop_val = val_acc
            else:
                early_stop_val = val_loss
            assert early_stop_val is not None
            assert isinstance(early_stop_val, float)

            self.moving_average += [early_stop_val]
            if len(self.moving_average) >= self.ema_observations:
                # Only keep track of the last [ema_observations]
                self.moving_average.pop(0)
                if self.last_ema == -1:
                    # Simple moving average
                    self.last_ema = (sum(self.moving_average)
                                     / len(self.moving_average))  # type: ignore
                    log_msg = f' (SMA: {self.last_ema:.3f})'
                else:
                    alpha = (self.ema_smoothing / (1 + self.ema_observations))
                    self.last_ema = (early_stop_val * alpha
                                     + (self.last_ema * (1 - alpha)))
                    log_msg = f' (EMA: {self.last_ema:.3f})'
                    if self.neptune_run and self.last_ema != -1:
                        neptune_dest = "metrics/val/batch/exp_moving_avg"
                        self.neptune_run[neptune_dest].log(self.last_ema)

                if (self.hp.early_stop
                   and self.ema_two_checks_prior != -1
                   and self.epoch > self.hp.early_stop_patience):

                    if ((self.hp.early_stop_method == 'accuracy'
                         and self.last_ema <= self.ema_two_checks_prior)
                       or (self.hp.early_stop_method == 'loss'
                           and self.last_ema >= self.ema_two_checks_prior)):

                        log.info(f'Early stop triggered: epoch {self.epoch}, '
                                 f'step {self.step}')
                        self._log_early_stop_to_neptune()
                        if self.epoch not in self.hp.epochs:
                            self.hp.epochs += [self.epoch]
                        self.early_stop = True
                        return log_msg

                self.ema_two_checks_prior = self.ema_one_check_prior
                self.ema_one_check_prior = self.last_ema
        return ''

    def _empty_corrects(self) -> Union[int, Dict[str, int]]:
        if self.multi_outcome:
            return {
                f'out-{o}': 0
                for o in range(self.num_outcomes)
            }
        else:
            return 0

    def _epoch_metrics(
        self,
        acc: Union[float, List[float]],
        loss: float,
        label: str
    ) -> Dict[str, Dict[str, Union[float, List[float]]]]:
        epoch_metrics = {'loss': loss}  # type: Dict
        if self.hp.model_type() == 'categorical':
            epoch_metrics.update({'accuracy': acc})
        return {f'{label}_metrics': epoch_metrics}

    def _val_metrics(self, **kwargs) -> Dict[str, Dict[str, float]]:
        """Evaluate model and calculate metrics.

        Returns:
            Dict[str, Dict[str, float]]: Dict with validation metrics.
            Returns metrics in the form:
            {
                'val_metrics': {
                    'loss': ...,
                    'accuracy': ...,
                },
                'tile_auc': ...,
                'slide_auc': ...,
                ...
            }
        """
        if hasattr(self, 'optimizer'):
            self.optimizer.zero_grad()
        assert self.model is not None
        self.model.eval()
        results_log = os.path.join(self.outdir, 'results_log.csv')
        epoch_results = {}

        # Preparations for calculating accuracy/loss in metrics_from_dataset()
        def update_corrects(pred, labels, running_corrects):
            if self.hp.model_type() == 'categorical':
                labels = self._labels_to_device(labels, self.device)
                return self._update_corrects(pred, labels, running_corrects)
            else:
                return 0

        def update_loss(pred, labels, running_loss, size):
            labels = self._labels_to_device(labels, self.device)
            loss = self._calculate_loss(pred, labels, self.loss_fn)
            return running_loss + (loss.item() * size)

        _running_corrects = self._empty_corrects()
        pred_args = types.SimpleNamespace(
            multi_outcome=(self.num_outcomes > 1),
            update_corrects=update_corrects,
            update_loss=update_loss,
            running_corrects=_running_corrects,
            num_slide_features=self.num_slide_features,
            slide_input=self.slide_input,
            uq=bool(self.hp.uq)
        )
        # Calculate patient/slide/tile metrics (AUC, R-squared, C-index, etc)
        metrics, acc, loss = sf.stats.metrics_from_dataset(
            self.inference_model,
            model_type=self.hp.model_type(),
            patients=self.patients,
            dataset=self.dataloaders['val'],
            data_dir=self.outdir,
            outcome_names=self.outcome_names,
            neptune_run=self.neptune_run,
            pred_args=pred_args,
            **kwargs
        )
        loss_and_acc = {'loss': loss}
        if self.hp.model_type() == 'categorical':
            loss_and_acc.update({'accuracy': acc})
            self._log_epoch(
                'val',
                self.epoch,
                loss,
                self._calculate_accuracy(acc)[1]  # type: ignore
            )
        epoch_metrics = {'val_metrics': loss_and_acc}

        for metric in metrics:
            if metrics[metric]['tile'] is None:
                continue
            epoch_results[f'tile_{metric}'] = metrics[metric]['tile']
            epoch_results[f'slide_{metric}'] = metrics[metric]['slide']
            epoch_results[f'patient_{metric}'] = metrics[metric]['patient']
        epoch_metrics.update(epoch_results)
        sf.util.update_results_log(
            results_log,
            'trained_model',
            {f'epoch{self.epoch}': epoch_metrics}
        )
        self._log_eval_to_neptune(loss, acc, metrics, epoch_metrics)
        return epoch_metrics

    def _fit_normalizer(self, norm_fit: Optional[NormFit]) -> None:
        """Fit the Trainer normalizer using the specified fit, if applicable.

        Args:
            norm_fit (Optional[Dict[str, np.ndarray]]): Normalizer fit.
        """
        if norm_fit is not None and not self.normalizer:
            raise ValueError("norm_fit supplied, but model params do not"
                             "specify a normalizer.")
        if self.normalizer and norm_fit is not None:
            self.normalizer.fit(**norm_fit)  # type: ignore
        elif (self.normalizer
              and 'norm_fit' in self.config
              and self.config['norm_fit'] is not None):
            log.debug("Detecting normalizer fit from model config")
            self.normalizer.fit(**self.config['norm_fit'])

    def _labels_to_device(
        self,
        labels: Union[Dict[Any, Tensor], Tensor],
        device: torch.device
    ) -> Union[Dict[Any, Tensor], Tensor]:
        '''Moves a set of outcome labels to the given device.'''
        if self.num_outcomes > 1:
            if not isinstance(labels, dict):
                raise ValueError("Expected labels to be a dict: num_outcomes"
                                 " is > 1")
            labels = {
                k: la.to(device, non_blocking=True) for k, la in labels.items()
            }
        elif isinstance(labels, dict):
            labels = torch.stack(list(labels.values()), dim=1)
            return labels.to(device, non_blocking=True)
        else:
            labels = labels.to(device, non_blocking=True)
        return labels

    def _log_epoch(
        self,
        phase: str,
        epoch: int,
        loss: float,
        accuracy_desc: str,
    ) -> None:
        """Logs epoch description."""
        log.info(f'{col.bold(col.blue(phase))} Epoch {epoch} | loss:'
                 f' {loss:.4f} {accuracy_desc}')

    def _log_manifest(
        self,
        train_dts: Optional["sf.Dataset"],
        val_dts: Optional["sf.Dataset"],
        labels: Optional[Union[str, Dict]] = 'auto'
    ) -> None:
        """Log the tfrecord and label manifest to slide_manifest.csv

        Args:
            train_dts (sf.Dataset): Training dataset. May be None.
            val_dts (sf.Dataset): Validation dataset. May be None.
            labels (dict, optional): Labels dictionary. May be None.
                Defaults to 'auto' (read from self.labels).
        """
        if labels == 'auto':
            _labels = self.labels
        elif labels is None:
            _labels = None
        else:
            assert isinstance(labels, dict)
            _labels = labels
        log_manifest(
            (train_dts.tfrecords() if train_dts else None),
            (val_dts.tfrecords() if val_dts else None),
            labels=_labels,
            filename=join(self.outdir, 'slide_manifest.csv')
        )

    def _log_to_tensorboard(
        self,
        loss: float,
        acc: Union[float, List[float]],
        label: str
    ) -> None:
        self.writer.add_scalar(f'Loss/{label}', loss, self.global_step)
        if self.hp.model_type() == 'categorical':
            if self.num_outcomes > 1:
                assert isinstance(acc, list)
                for o, _acc in enumerate(acc):
                    self.writer.add_scalar(
                        f'Accuracy-{o}/{label}', _acc, self.global_step
                    )
            else:
                self.writer.add_scalar(
                    f'Accuracy/{label}', acc, self.global_step
                )

    def _log_to_neptune(
        self,
        loss: float,
        acc: Union[Tensor, List[Tensor]],
        label: str,
        phase: str
    ) -> None:
        """Logs epoch loss/accuracy to Neptune."""
        assert phase in ('batch', 'epoch')
        step = self.epoch if phase == 'epoch' else self.global_step
        if self.neptune_run:
            self.neptune_run[f"metrics/{label}/{phase}/loss"].log(loss,
                                                                  step=step)
            run_kw = {
                'run': self.neptune_run,
                'step': step
            }
            acc = self._accuracy_as_numpy(acc)
            if isinstance(acc, list):
                for a, _acc in enumerate(acc):
                    sf.util.neptune_utils.list_log(
                        f'metrics/{label}/{phase}/accuracy-{a}', _acc, **run_kw
                    )
            else:
                sf.util.neptune_utils.list_log(
                    f'metrics/{label}/{phase}/accuracy', acc, **run_kw
                )

    def _log_early_stop_to_neptune(self) -> None:
        # Log early stop to neptune
        if self.neptune_run:
            self.neptune_run["early_stop/early_stop_epoch"] = self.epoch
            self.neptune_run["early_stop/early_stop_batch"] = self.step
            self.neptune_run["early_stop/method"] = self.hp.early_stop_method
            self.neptune_run["sys/tags"].add("early_stopped")

    def _log_eval_to_neptune(
        self,
        loss: float,
        acc: float,
        metrics: Dict[str, Any],
        epoch_results: Dict[str, Any]
    ) -> None:
        if self.use_neptune:
            assert self.neptune_run is not None
            self.neptune_run['results'] = epoch_results

            # Validation epoch metrics
            self.neptune_run['metrics/val/epoch/loss'].log(loss,
                                                           step=self.epoch)
            sf.util.neptune_utils.list_log(
                self.neptune_run,
                'metrics/val/epoch/accuracy',
                acc,
                step=self.epoch
            )
            for metric in metrics:
                if metrics[metric]['tile'] is None:
                    continue
                for outcome in metrics[metric]['tile']:
                    # If only one outcome,
                    #   log to metrics/val/epoch/[metric].
                    # If more than one outcome,
                    #   log to metrics/val/epoch/[metric]/[outcome_name]
                    def metric_label(s):
                        if len(metrics[metric]['tile']) == 1:
                            return f'metrics/val/epoch/{s}_{metric}'
                        else:
                            return f'metrics/val/epoch/{s}_{metric}/{outcome}'

                    tile_metric = metrics[metric]['tile'][outcome]
                    slide_metric = metrics[metric]['slide'][outcome]
                    patient_metric = metrics[metric]['patient'][outcome]

                    # If only one value for a metric, log to .../[metric]
                    # If more than one value for a metric
                    #   (e.g. AUC for each category),
                    # log to .../[metric]/[i]
                    sf.util.neptune_utils.list_log(
                        self.neptune_run,
                        metric_label('tile'),
                        tile_metric,
                        step=self.epoch
                    )
                    sf.util.neptune_utils.list_log(
                        self.neptune_run,
                        metric_label('slide'),
                        slide_metric,
                        step=self.epoch
                    )
                    sf.util.neptune_utils.list_log(
                        self.neptune_run,
                        metric_label('patient'),
                        patient_metric,
                        step=self.epoch
                    )

    def _mid_training_validation(self) -> None:
        """Perform mid-epoch validation, if appropriate."""

        if not self.validate_on_batch:
            return
        elif not (
            'val' in self.dataloaders
            and self.step > 0
            and self.step % self.validate_on_batch == 0
        ):
            return

        if self.model is None or self.inference_model is None:
            raise errors.ModelError("Model not yet initialized.")
        self.model.eval()
        running_val_loss = 0
        num_val = 0
        running_val_correct = self._empty_corrects()

        for _ in range(self.validation_steps):
            (val_img,
             val_label,
             slides) = next(self.mid_train_val_dts)  # type: ignore
            val_img = val_img.to(self.device)

            with torch.no_grad():
                _mp = self.mixed_precision
                _ns = no_scope()
                with torch.cuda.amp.autocast() if _mp else _ns:  # type: ignore
                    if self.num_slide_features:
                        _slide_in = [self.slide_input[s] for s in slides]
                        inp = (val_img, Tensor(_slide_in).to(self.device))
                    else:
                        inp = (val_img,)  # type: ignore
                    val_outputs = self.inference_model(*inp)
                    val_label = self._labels_to_device(val_label, self.device)
                    val_batch_loss = self._calculate_loss(
                        val_outputs, val_label, self.loss_fn
                    )

            running_val_loss += val_batch_loss.item() * val_img.size(0)
            if self.hp.model_type() == 'categorical':
                running_val_correct = self._update_corrects(
                    val_outputs, val_label, running_val_correct  # type: ignore
                )
            num_val += val_img.size(0)
        val_loss = running_val_loss / num_val
        if self.hp.model_type() == 'categorical':
            val_acc, val_acc_desc = self._calculate_accuracy(
                running_val_correct, num_val  # type: ignore
            )
        else:
            val_acc, val_acc_desc = 0, ''  # type: ignore
        log_msg = f'Batch {self.step}: val loss: {val_loss:.4f} {val_acc_desc}'

        # Log validation metrics to neptune & check early stopping
        self._log_to_neptune(val_loss, val_acc, 'val', phase='batch')
        log_msg += self._check_early_stopping(
            self._accuracy_as_numpy(val_acc),
            val_loss
        )
        log.info(log_msg)

        # Log to tensorboard
        if self.use_tensorboard:
            if self.num_outcomes > 1:
                assert isinstance(running_val_correct, dict)
                _val_acc = [
                    running_val_correct[f'out-{o}'] / num_val
                    for o in range(len(val_outputs))
                ]
            else:
                assert not isinstance(running_val_correct, dict)
                _val_acc = running_val_correct / num_val  # type: ignore
            self._log_to_tensorboard(
                val_loss,
                self._accuracy_as_numpy(_val_acc),
                'test'
            )  # type: ignore

        # Put model back in training mode
        self.model.train()

    def _prepare_optimizers_and_loss(self) -> None:
        if self.model is None:
            raise ValueError("Model has not yet been initialized.")
        self.optimizer = self.hp.get_opt(self.model.parameters())
        if self.hp.learning_rate_decay:
            self.scheduler = torch.optim.lr_scheduler.ExponentialLR(
                self.optimizer,
                gamma=self.hp.learning_rate_decay
            )
            log.debug("Using exponentially decaying learning rate")
        else:
            self.scheduler = None  # type: ignore
        self.loss_fn = self.hp.get_loss()
        if self.mixed_precision:
            self.scaler = torch.cuda.amp.GradScaler()

    def _prepare_neptune_run(self, dataset: "sf.Dataset", label: str) -> None:
        if self.use_neptune:
            tags = [label]
            if 'k-fold' in self.config['validation_strategy']:
                tags += [f'k-fold{self.config["k_fold_i"]}']
            self.neptune_run = self.neptune_logger.start_run(
                self.name,
                self.config['project'],
                dataset,
                tags=tags
            )
            assert self.neptune_run is not None
            self.neptune_logger.log_config(self.config, label)
            self.neptune_run['data/slide_manifest'].upload(
                os.path.join(self.outdir, 'slide_manifest.csv')
            )
            try:
                config_path = join(self.outdir, 'params.json')
                config = sf.util.load_json(config_path)
                config['neptune_id'] = self.neptune_run['sys/id'].fetch()
            except Exception:
                log.info("Unable to log params (params.json) with Neptune.")

    def _print_model_summary(self, train_dts) -> None:
        """Prints model summary and logs to neptune."""
        if self.model is None:
            raise ValueError("Model has not yet been initialized.")
        empty_inp = [torch.empty(
            [self.hp.batch_size, 3, train_dts.tile_px, train_dts.tile_px]
        )]
        if self.num_slide_features:
            empty_inp += [
                torch.empty([self.hp.batch_size, self.num_slide_features])
            ]
        if log.getEffectiveLevel() <= 20:
            model_summary = torch_utils.print_module_summary(
                self.model, empty_inp
            )
            if self.neptune_run:
                self.neptune_run['summary'] = model_summary

    def _save_model(self) -> None:
        assert self.model is not None
        name = self.name if self.name else 'trained_model'
        save_path = os.path.join(self.outdir, f'{name}_epoch{self.epoch}')
        torch.save(self.model.state_dict(), save_path)
        log.info(f"Model saved to {col.green(save_path)}")

    def _setup_dataloaders(
        self,
        train_dts: Optional["sf.Dataset"],
        val_dts: Optional["sf.Dataset"],
        mid_train_val: bool = False,
        incl_labels: bool = True
    ) -> None:
        interleave_args = types.SimpleNamespace(
            rank=0,
            num_replicas=1,
            labels=(self.labels if incl_labels else None),
            chunk_size=8,
            normalizer=self.normalizer,
            pin_memory=True,
            num_workers=4,
            onehot=False,
            incl_slidenames=True,
            device=self.device
        )

        if train_dts is not None:
            self.dataloaders = {
                'train': iter(train_dts.torch(
                    infinite=True,
                    batch_size=self.hp.batch_size,
                    augment=self.hp.augment,
                    drop_last=True,
                    **vars(interleave_args)
                ))
            }
        else:
            self.dataloaders = {}
        if val_dts is not None:
            if not self.validation_batch_size:
                validation_batch_size = self.hp.batch_size
            self.dataloaders['val'] = val_dts.torch(
                infinite=False,
                batch_size=validation_batch_size,
                augment=False,
                **vars(interleave_args)
            )
            # Mid-training validation dataset
            self.mid_train_val_dts = torch_utils.cycle(self.dataloaders['val'])
            if not self.validate_on_batch:
                val_log_msg = ''
            else:
                val_log_msg = f'every {str(self.validate_on_batch)} steps and '
            log.debug(f'Validation during training: {val_log_msg}at epoch end')
            if self.validation_steps:
                num_samples = self.validation_steps * self.hp.batch_size
                log.debug(
                    f'Using {self.validation_steps} batches ({num_samples} '
                    'samples) each validation check'
                )
            else:
                log.debug('Using entire validation set each validation check')
        else:
            self.mid_train_val_dts = None  # type: ignore
            log.debug('Validation during training: None')

    def _training_step(self, pb: tqdm) -> None:
        assert self.model is not None
        images, labels, slides = next(self.dataloaders['train'])
        images = images.to(self.device, non_blocking=True)
        labels = self._labels_to_device(labels, self.device)
        self.optimizer.zero_grad()
        with torch.set_grad_enabled(True):
            _mp = self.mixed_precision
            _ns = no_scope()
            with torch.cuda.amp.autocast() if _mp else _ns:  # type: ignore
                # Slide-level features
                if self.num_slide_features:
                    _slide_in = [self.slide_input[s] for s in slides]
                    inp = (images, Tensor(_slide_in).to(self.device))
                else:
                    inp = (images,)  # type: ignore
                outputs = self.model(*inp)
                loss = self._calculate_loss(outputs, labels, self.loss_fn)

            # Update weights
            if self.mixed_precision:
                self.scaler.scale(loss).backward()
                self.scaler.step(self.optimizer)
                self.scaler.update()
            else:
                loss.backward()
                self.optimizer.step()

            # Update learning rate if using a scheduler
            _lr_decay_steps = self.hp.learning_rate_decay_steps
            if self.scheduler and (self.global_step+1) % _lr_decay_steps == 0:
                log.debug("Stepping learning rate decay")
                self.scheduler.step()

        # Record accuracy and loss
        self.epoch_records += images.size(0)
        if self.hp.model_type() == 'categorical':
            self.running_corrects = self._update_corrects(
                outputs, labels, self.running_corrects
            )
            train_acc, acc_desc = self._calculate_accuracy(
                self.running_corrects, self.epoch_records
            )
        else:
            train_acc, acc_desc = 0, ''  # type: ignore
        self.running_loss += loss.item() * images.size(0)
        _loss = self.running_loss / self.epoch_records
        pb.set_description(f'{col.bold(col.blue(self.phase))} '
                           f'loss: {_loss:.4f} {acc_desc}')
        pb.update(images.size(0))

        # Log to tensorboard
        if self.use_tensorboard and self.global_step % self.log_frequency == 0:
            if self.num_outcomes > 1:
                _train_acc = [
                    (self.running_corrects[f'out-{o}']  # type: ignore
                     / self.epoch_records)
                    for o in range(len(outputs))
                ]
            else:
                _train_acc = (self.running_corrects  # type: ignore
                              / self.epoch_records)
            self._log_to_tensorboard(
                loss.item(),
                self._accuracy_as_numpy(_train_acc),
                'train'
            )
        # Log to neptune & check early stopping
        self._log_to_neptune(loss.item(), train_acc, 'train', phase='batch')
        self._check_early_stopping(None, None)

    def _update_corrects(
        self,
        outputs: Union[Tensor, Dict[Any, Tensor]],
        labels: Union[Tensor, Dict[str, Tensor]],
        running_corrects: Union[Tensor, Dict[str, Tensor]],
    ) -> Union[Tensor, Dict[str, Tensor]]:
        '''Updates running accuracy in a manner compatible with >1 outcomes.'''
        assert self.hp.model_type() == 'categorical'
        if self.num_outcomes > 1:
            for o, out in enumerate(outputs):
                _, preds = torch.max(out, 1)
                running_corrects[f'out-{o}'] += torch.sum(  # type: ignore
                    preds == labels[f'out-{o}'].data  # type: ignore
                )
        else:
            _, preds = torch.max(outputs, 1)  # type: ignore
            running_corrects += torch.sum(preds == labels.data)  # type: ignore
        return running_corrects

    def _validate_early_stop(self) -> None:
        """Validates early stopping parameters."""

        if (self.hp.early_stop and self.hp.early_stop_method == 'accuracy' and
           self.hp.model_type() == 'categorical' and self.num_outcomes > 1):
            raise errors.ModelError("Cannot combine 'accuracy' early stopping "
                                    "with multiple categorical outcomes.")
        if (self.hp.early_stop_method == 'manual'
            and (self.hp.manual_early_stop_epoch is None
                 or self.hp.manual_early_stop_batch is None)):
            raise errors.ModelError(
                "Early stopping method 'manual' requires that both "
                "manual_early_stop_epoch and manual_early_stop_batch are set "
                "in model params."
            )

    def load(self, model: str) -> None:
        """Loads a state dict at the given model location. Requires that the
        Trainer's hyperparameters (Trainer.hp)
        match the hyperparameters of the model to be loaded."""

        if self.labels is not None:
            self.model = self.hp.build_model(
                labels=self.labels,
                num_slide_features=self.num_slide_features
            )
        else:
            self.model = self.hp.build_model(
                num_classes=len(self.outcome_names),
                num_slide_features=self.num_slide_features
            )
        self.model.load_state_dict(torch.load(model))
        self.inference_model = self.model

    def predict(
        self,
        dataset: "sf.Dataset",
        batch_size: Optional[int] = None,
        norm_fit: Optional[NormFit] = None,
        format: str = 'csv'
    ) -> "pd.DataFrame":
        """Perform inference on a model, saving predictions.

        Args:
            dataset (:class:`slideflow.dataset.Dataset`): Dataset containing
                TFRecords to evaluate.
            batch_size (int, optional): Evaluation batch size. Defaults to the
                same as training (per self.hp)
            format (str, optional): Format in which to save predictions.
                Either 'csv' or 'feather'. Defaults to 'csv'.
            norm_fit (Dict[str, np.ndarray]): Normalizer fit, mapping fit
                parameters (e.g. target_means, target_stds) to values
                (np.ndarray). If not provided, will fit normalizer using
                model params (if applicable). Defaults to None.

        Returns:
            pandas.DataFrame of tile-level predictions.
        """

        # Fit normalizer
        self._fit_normalizer(norm_fit)

        # Load and initialize model
        if not self.model:
            raise errors.ModelNotLoadedError
        device = torch.device('cuda:0')
        self.model.to(device)
        self.model.eval()
        self._log_manifest(None, dataset, labels=None)

        if not batch_size:
            batch_size = self.hp.batch_size
        self._setup_dataloaders(None, dataset, incl_labels=False)
        # Generate predictions
        log.info('Generating predictions...')
        pred_args = types.SimpleNamespace(
            uq=bool(self.hp.uq),
            multi_outcome=(self.num_outcomes > 1),
            num_slide_features=self.num_slide_features,
            slide_input=self.slide_input
        )
        df = sf.stats.predict_from_dataset(
            model=self.model,
            dataset=self.dataloaders['val'],
            model_type=self._model_type,
            pred_args=pred_args
        )
        if format.lower() == 'csv':
            save_path = os.path.join(self.outdir, "tile_predictions.csv")
            df.to_csv(save_path)
        elif format.lower() == 'feather':
            import pyarrow.feather as feather
            save_path = os.path.join(self.outdir, 'tile_predictions.feather')
            feather.write_feather(df, save_path)
        log.debug(f"Predictions saved to {col.green(save_path)}")
        return df

    def evaluate(
        self,
        dataset: "sf.Dataset",
        batch_size: Optional[int] = None,
        histogram: bool = False,
        save_predictions: bool = False,
        reduce_method: str = 'average',
        norm_fit: Optional[NormFit] = None,
        uq: Union[bool, str] = 'auto'
    ):
        """Evaluate model, saving metrics and predictions.

        Args:
            dataset (:class:`slideflow.dataset.Dataset`): Dataset to evaluate.
            batch_size (int, optional): Evaluation batch size. Defaults to the
                same as training (per self.hp)
            histogram (bool, optional): Save histogram of tile predictions.
                Poorly optimized, uses seaborn, may drastically increase
                evaluation time. Defaults to False.
            save_predictions (bool, optional): Save tile, slide, and
                patient-level predictions to CSV. Defaults to False.
            reduce_method (str, optional): Reduction method for calculating
                slide-level and patient-level predictions for categorical outcomes.
                Either 'average' or 'proportion'. If 'average', will reduce with
                average of each logit across tiles. If 'proportion', will convert
                tile predictions into onehot encoding then reduce by averaging
                these onehot values. Defaults to 'average'.
            norm_fit (Dict[str, np.ndarray]): Normalizer fit, mapping fit
                parameters (e.g. target_means, target_stds) to values
                (np.ndarray). If not provided, will fit normalizer using
                model params (if applicable). Defaults to None.
            uq (bool or str, optional): Enable UQ estimation (for
                applicable models). Defaults to 'auto'.

        Returns:
            Dictionary of evaluation metrics.
        """
        if uq != 'auto':
            if not isinstance(uq, bool):
                raise ValueError(f"Unrecognized value {uq} for uq")
            self.hp.uq = uq
        if batch_size:
            self.validation_batch_size = batch_size
        if not self.model:
            raise errors.ModelNotLoadedError
        self._fit_normalizer(norm_fit)
        self.model.to(self.device)
        self.model.eval()
        self.loss_fn = self.hp.get_loss()
        self._log_manifest(None, dataset)
        self._prepare_neptune_run(dataset, 'eval')
        self._setup_dataloaders(None, val_dts=dataset)

        # Generate performance metrics
        log.info('Performing evaluation...')
        metrics = self._val_metrics(
            histogram=histogram,
            label='eval',
            reduce_method=reduce_method
        )
        results = {'eval': {
            k: v for k, v in metrics.items() if k != 'val_metrics'
        }}
        results['eval'].update(metrics['val_metrics'])  # type: ignore
        results_str = json.dumps(results['eval'], indent=2, sort_keys=True)
        log.info(f"Evaluation metrics: {results_str}")
        results_log = os.path.join(self.outdir, 'results_log.csv')
        sf.util.update_results_log(results_log, 'eval_model', results)

        if self.neptune_run:
            self.neptune_run['eval/results'] = results['eval']
            self.neptune_run.stop()
        return results

    def train(
        self,
        train_dts: "sf.Dataset",
        val_dts: "sf.Dataset",
        log_frequency: int = 20,
        validate_on_batch: int = 0,
        validation_batch_size: Optional[int] = None,
        validation_steps: int = 50,
        starting_epoch: int = 0,
        ema_observations: int = 20,
        ema_smoothing: int = 2,
        use_tensorboard: bool = True,
        steps_per_epoch_override: int = 0,
        save_predictions: bool = False,
        save_model: bool = True,
        resume_training: Optional[str] = None,
        pretrain: Optional[str] = 'imagenet',
        checkpoint: Optional[str] = None,
        multi_gpu: bool = False,
        norm_fit: Optional[NormFit] = None,
        reduce_method: str = 'average',
        seed: int = 0
    ) -> Dict[str, Any]:
        """Builds and trains a model from hyperparameters.

        Args:
            train_dts (:class:`slideflow.dataset.Dataset`): Training dataset.
            val_dts (:class:`slideflow.dataset.Dataset`): Validation dataset.
            log_frequency (int, optional): How frequent to update Tensorboard
                logs, in batches. Defaults to 100.
            validate_on_batch (int, optional): Validation will be performed
                every N batches. Defaults to 0.
            validation_batch_size (int, optional): Validation batch size.
                Defaults to same as training (per self.hp).
            validation_steps (int, optional): Number of batches to use for each
                instance of validation. Defaults to 200.
            starting_epoch (int, optional): Starts training at this epoch.
                Defaults to 0.
            ema_observations (int, optional): Number of observations over which
                to perform exponential moving average smoothing.
                Defaults to 20.
            ema_smoothing (int, optional): Exponential average smoothing value.
                Defaults to 2.
            use_tensoboard (bool, optional): Enable tensorboard callbacks.
                Defaults to False.
            steps_per_epoch_override (int, optional): Manually set the number
                of steps per epoch. Defaults to None.
            save_predictions (bool, optional): Save tile, slide, and
                patient-level predictions at each evaluation.
                Defaults to False.
            save_model (bool, optional): Save models when evaluating at
                specified epochs. Defaults to False.
            resume_training (str, optional): Not applicable to PyTorch backend.
                Included as argument for compatibility with Tensorflow backend.
                Will raise NotImplementedError if supplied.
            pretrain (str, optional): Either 'imagenet' or path to Tensorflow
                model from which to load weights. Defaults to 'imagenet'.
            checkpoint (str, optional): Path to cp.ckpt from which to load
                weights. Defaults to None.
            norm_fit (Dict[str, np.ndarray]): Normalizer fit, mapping fit
                parameters (e.g. target_means, target_stds) to values
                (np.ndarray). If not provided, will fit normalizer using
                model params (if applicable). Defaults to None.
            reduce_method (str, optional): Reduction method for calculating
                slide-level and patient-level predictions for categorical outcomes.
                Either 'average' or 'proportion'. If 'average', will reduce with
                average of each logit across tiles. If 'proportion', will convert
                tile predictions into onehot encoding then reduce by averaging
                these onehot values. Defaults to 'average'.

        Returns:
            Dict:   Nested dict containing metrics for each evaluated epoch.
        """
        if resume_training is not None:
            raise NotImplementedError(
                "PyTorch backend does not support `resume_training`; "
                "please use `checkpoint`"
            )
        results = {'epochs': defaultdict(dict)}  # type: Dict[str, Any]
        starting_epoch = max(starting_epoch, 1)
        self._reset_training_params()
        self.validation_batch_size = validation_batch_size
        self.validate_on_batch = validate_on_batch
        self.validation_steps = validation_steps
        self.ema_observations = ema_observations
        self.ema_smoothing = ema_smoothing
        self.use_tensorboard = use_tensorboard
        self.log_frequency = log_frequency

        # Validate early stopping parameters
        self._validate_early_stop()

        # Enable TF32 (should be enabled by default)
        # Allow PyTorch to internally use tf32 for matmul and convolutions
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True  # type: ignore

        # Fit normalizer to dataset, if applicable
        self._fit_normalizer(norm_fit)
        if self.normalizer and self.hp.normalizer_source == 'dataset':
            self.normalizer.fit(train_dts)

        # Training preparation
        if steps_per_epoch_override:
            self.steps_per_epoch = steps_per_epoch_override
            log.info(f"Setting steps per epoch = {steps_per_epoch_override}")
        else:
            self.steps_per_epoch = train_dts.num_tiles // self.hp.batch_size
            log.info(f"Steps per epoch = {self.steps_per_epoch}")
        epoch_size = (self.steps_per_epoch * self.hp.batch_size)
        if use_tensorboard:
            self.writer = SummaryWriter(self.outdir, flush_secs=60)
        self._log_manifest(train_dts, val_dts)

        # Prepare neptune run
        self._prepare_neptune_run(train_dts, 'train')

        # Build model
        self._build_model(checkpoint, pretrain)
        assert self.model is not None

        # Print model summary
        self._print_model_summary(train_dts)

        # Multi-GPU
        if multi_gpu:
            self.model = torch.nn.DataParallel(self.model)
        self.model = self.model.to(self.device)

        # Setup dataloaders
        self._setup_dataloaders(train_dts, val_dts, mid_train_val=True)

        # Model parameters and optimizer
        self._prepare_optimizers_and_loss()

        # === Epoch loop ======================================================
        for self.epoch in range(starting_epoch, max(self.hp.epochs)+1):
            np.random.seed(seed+self.epoch)
            log.info(col.bold(f'Epoch {self.epoch}/{max(self.hp.epochs)}'))

            # Training loop ---------------------------------------------------
            self.epoch_records = 0
            self.running_loss = 0.0
            self.step = 1
            self.running_corrects = self._empty_corrects()  # type: ignore
            self.model.train()
            pb = tqdm(total=epoch_size, unit='img', leave=False)
            while self.step < self.steps_per_epoch:
                self._training_step(pb)
                if self.early_stop:
                    break
                self._mid_training_validation()
                self.step += 1
                self.global_step += 1
            pb.close()

            # Update and log epoch metrics ------------------------------------
            loss = self.running_loss / self.epoch_records
            epoch_metrics = {'train_metrics': {'loss': loss}}
            if self.hp.model_type() == 'categorical':
                acc, acc_desc = self._calculate_accuracy(
                    self.running_corrects, self.epoch_records
                )
                epoch_metrics['train_metrics'].update({
                    'accuracy': self._accuracy_as_numpy(acc)  # type: ignore
                })
            else:
                acc, acc_desc = 0, ''  # type: ignore
            results['epochs'][f'epoch{self.epoch}'].update(epoch_metrics)
            self._log_epoch('train', self.epoch, loss, acc_desc)
            self._log_to_neptune(loss, acc, 'train', 'epoch')
            if save_model and self.epoch in self.hp.epochs:
                self._save_model()

            # Full evaluation -------------------------------------------------
            # Perform full evaluation if the epoch is one of the
            # predetermined epochs at which to save/eval a model
            if 'val' in self.dataloaders and self.epoch in self.hp.epochs:
                epoch_res = self._val_metrics(
                    save_predictions=save_predictions,
                    reduce_method=reduce_method
                )
                results['epochs'][f'epoch{self.epoch}'].update(epoch_res)

            # Early stopping --------------------------------------------------
            if self.early_stop:
                break

        # === [end epoch loop] ================================================

        if self.neptune_run:
            self.neptune_run['sys/tags'].add('training_complete')
            self.neptune_run.stop()
        return results


class LinearTrainer(Trainer):

    """Extends the base :class:`slideflow.model.Trainer` class to add support
    for linear outcomes. Requires that all outcomes be linear, with appropriate
    linear loss function. Uses R-squared as the evaluation metric, rather
    than AUROC.

    In this case, for the PyTorch backend, the linear outcomes support is
    already baked into the base Trainer class, so no additional modifications
    are required. This class is written to inherit the Trainer class without
    modification to maintain consistency with the Tensorflow backend.
    """

    _model_type = 'linear'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class CPHTrainer(Trainer):

    """Cox proportional hazards (CPH) models are not yet implemented, but are
    planned for a future update."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError


class Features:
    """Interface for obtaining logits and features from intermediate layer
    activations from Slideflow models.

    Use by calling on either a batch of images (returning outputs for a single
    batch), or by calling on a :class:`slideflow.WSI` object, which will
    generate an array of spatially-mapped activations matching the slide.

    Examples
        *Calling on batch of images:*

        .. code-block:: python

            interface = Features('/model/path', layers='postconv')
            for image_batch in train_data:
                # Return shape: (batch_size, num_features)
                batch_features = interface(image_batch)

        *Calling on a slide:*

        .. code-block:: python

            slide = sf.slide.WSI(...)
            interface = Features('/model/path', layers='postconv')
            # Return shape:
            # (slide.grid.shape[0], slide.grid.shape[1], num_features)
            activations_grid = interface(slide)

    Note:
        When this interface is called on a batch of images, no image processing
        or stain normalization will be performed, as it is assumed that
        normalization will occur during data loader image processing. When the
        interface is called on a `slideflow.WSI`, the normalization strategy
        will be read from the model configuration file, and normalization will
        be performed on image tiles extracted from the WSI. If this interface
        was created from an existing model and there is no model configuration
        file to read, a slideflow.norm.StainNormalizer object may be passed
        during initialization via the argument `wsi_normalizer`.

    """

    def __init__(
        self,
        path: Optional[Path],
        layers: Optional[Union[str, List[str]]] = 'postconv',
        include_logits: bool = False,
        mixed_precision: bool = True,
        device: Optional[torch.device] = None
    ):
        """Creates an activations interface from a saved slideflow model which
        outputs feature activations at the designated layers.

        Intermediate layers are returned in the order of layers.
        Logits are returned last.

        Args:
            path (str): Path to saved Slideflow model.
            layers (list(str), optional): Layers from which to generate
                activations.  The post-convolution activation layer is accessed
                via 'postconv'. Defaults to 'postconv'.
            include_logits (bool, optional): Include logits in output. Will be
                returned last. Defaults to False.
            mixed_precision (bool, optional): Use mixed precision.
                Defaults to True.
            device (:class:`torch.device`, optional): Device for model.
                Defaults to torch.device('cuda')
        """

        if layers and isinstance(layers, str):
            layers = [layers]
        self.path = path
        self.num_logits = 0
        self.num_features = 0
        self.num_uncertainty = 0
        self.mixed_precision = mixed_precision
        self.img_format = None
        # Hook for storing layer activations during model inference
        self.activation = {}  # type: Dict[Any, Tensor]
        self.layers = layers
        self.include_logits = include_logits
        self.device = device if device is not None else torch.device('cuda')

        if path is not None:
            config = sf.util.get_model_config(path)
            if 'img_format' in config:
                self.img_format = config['img_format']
            self.hp = ModelParams()  # type: Optional[ModelParams]
            self.hp.load_dict(config['hp'])
            self.wsi_normalizer = self.hp.get_normalizer()
            if 'norm_fit' in config and config['norm_fit'] is not None:
                self.wsi_normalizer.fit(**config['norm_fit'])  # type: ignore
            self.tile_px = self.hp.tile_px
            self._model = self.hp.build_model(
                num_classes=len(config['outcome_labels'])
            )
            self._model.load_state_dict(torch.load(path))
            self._model.to(self.device)
            if self._model.__class__.__name__ == 'ModelWrapper':
                self.model_type = self._model.model.__class__.__name__
            else:
                self.model_type = self._model.__class__.__name__
            self._build()
            self._model.eval()

    @classmethod
    def from_model(
        cls,
        model: torch.nn.Module,
        tile_px: int,
        layers: Optional[Union[str, List[str]]] = 'postconv',
        include_logits: bool = False,
        mixed_precision: bool = True,
        wsi_normalizer: Optional["StainNormalizer"] = None,
        device: Optional[torch.device] = None
    ):
        """Creates an activations interface from a loaded slideflow model which
        outputs feature activations at the designated layers.

        Intermediate layers are returned in the order of layers.
        Logits are returned last.

        Args:
            model (:class:`tensorflow.keras.models.Model`): Loaded model.
            tile_px (int): Width/height of input image size.
            layers (list(str), optional): Layers from which to generate
                activations.  The post-convolution activation layer is accessed
                via 'postconv'. Defaults to 'postconv'.
            include_logits (bool, optional): Include logits in output. Will be
                returned last. Defaults to False.
            wsi_normalizer (:class:`slideflow.norm.StainNormalizer`): Stain
                normalizer to use on whole-slide images. Is not used on
                individual tile datasets via __call__. Defaults to None.
            device (:class:`torch.device`, optional): Device for model.
                Defaults to torch.device('cuda')
        """
        obj = cls(None, layers, include_logits, mixed_precision, device)
        if isinstance(model, torch.nn.Module):
            obj._model = model.to(obj.device)
            obj._model.eval()
        else:
            raise errors.ModelError("Model is not a valid PyTorch model.")
        obj.hp = None
        if obj._model.__class__.__name__ == 'ModelWrapper':
            obj.model_type = obj._model.model.__class__.__name__
        else:
            obj.model_type = obj._model.__class__.__name__
        obj.tile_px = tile_px
        obj.wsi_normalizer = wsi_normalizer
        obj._build()
        return obj

    def __call__(
        self,
        inp: Union[Tensor, "sf.WSI"],
        **kwargs
    ) -> Union[List[Tensor], np.ndarray]:
        """Process a given input and return activations and/or logits. Expects
        either a batch of images or a :class:`slideflow.slide.WSI` object."""

        if isinstance(inp, sf.slide.WSI):
            return self._predict_slide(inp, **kwargs)
        else:
            return self._predict(inp)

    def _predict_slide(
        self,
        slide: "sf.WSI",
        *,
        img_format: str = 'auto',
        batch_size: int = 32,
        dtype: type = np.float16,
        **kwargs
    ) -> np.ndarray:
        """Generate activations from slide => activation grid array."""

        log.debug(f"Slide prediction (batch_size={batch_size}, "
                  f"img_format={img_format})")
        if img_format == 'auto' and self.img_format is None:
            raise ValueError(
                'Unable to auto-detect image format (png or jpg). Set the '
                'format by passing img_format=... to the call function.'
            )
        elif img_format == 'auto':
            assert self.img_format is not None
            img_format = self.img_format
        total_out = self.num_features + self.num_logits
        zeros_shape = (slide.grid.shape[1], slide.grid.shape[0], total_out)
        features_grid = np.zeros(zeros_shape, dtype=dtype)
        generator = slide.build_generator(
            shuffle=False,
            include_loc='grid',
            show_progress=True,
            img_format=img_format,
            **kwargs)
        if not generator:
            log.error(f"No tiles extracted from slide {col.green(slide.name)}")
            return

        class SlideIterator(torch.utils.data.IterableDataset):
            def __init__(self, parent, *args, **kwargs):
                super(SlideIterator).__init__(*args, **kwargs)
                self.parent = parent

            def __iter__(self):
                for image_dict in generator():
                    img = image_dict['image']
                    np_data = torch.from_numpy(np.fromstring(img,
                                                             dtype=np.uint8))
                    img = torchvision.io.decode_image(np_data)
                    if self.parent.wsi_normalizer:
                        img = img.permute(1, 2, 0)  # CWH => WHC
                        img = torch.from_numpy(
                            self.parent.wsi_normalizer.rgb_to_rgb(img.numpy())
                        )
                        img = img.permute(2, 0, 1)  # WHC => CWH
                    loc = np.array(image_dict['loc'])
                    img = img / 127.5 - 1
                    yield img, loc

        tile_dataset = torch.utils.data.DataLoader(
            SlideIterator(self),
            batch_size=batch_size
        )
        act_arr = []
        loc_arr = []
        for i, (batch_images, batch_loc) in enumerate(tile_dataset):
            model_out = sf.util.as_list(self._predict(batch_images))
            act_arr += [
                np.concatenate([m.cpu().detach().numpy()
                                for m in model_out])
            ]
            loc_arr += [batch_loc]

        act_arr = np.concatenate(act_arr)
        loc_arr = np.concatenate(loc_arr)

        for i, act in enumerate(act_arr):
            xi = loc_arr[i][0]
            yi = loc_arr[i][1]
            features_grid[yi][xi] = act

        return features_grid

    def _predict(self, inp: Tensor) -> List[Tensor]:
        """Return activations for a single batch of images."""
        _mp = self.mixed_precision
        with torch.cuda.amp.autocast() if _mp else no_scope():  # type: ignore
            with torch.no_grad():
                logits = self._model(inp.to(self.device))

        layer_activations = []
        if self.layers:
            for la in self.layers:
                act = self.activation[la]
                if la == 'postconv':
                    act = self._postconv_processing(act)
                layer_activations.append(act)

        if self.include_logits:
            layer_activations += [logits]
        self.activation = {}
        return layer_activations

    def _get_postconv(self):
        """Returns post-convolutional layer."""

        if self.model_type == 'ViT':
            return self._model.to_latent
        if self.model_type in ('ResNet', 'Inception3', 'GoogLeNet'):
            return self._model.avgpool
        if self.model_type in ('AlexNet', 'SqueezeNet', 'VGG', 'MobileNetV2',
                               'MobileNetV3', 'MNASNet'):
            return next(self._model.classifier.children())
        if self.model_type == 'DenseNet':
            return self._model.features.norm5
        if self.model_type == 'ShuffleNetV2':
            return list(self._model.conv5.children())[1]
        if self.model_type == 'Xception':
            return self._model.bn4
        raise errors.FeaturesError(f"'postconv' layer not configured for "
                                   f"model type {self.model_type}")

    def _postconv_processing(self, output: Tensor) -> Tensor:
        """Applies processing (pooling, resizing) to post-conv outputs,
        to convert output to the shape (batch_size, num_features)"""

        def pool(x):
            return torch.nn.functional.adaptive_avg_pool2d(x, (1, 1))

        def squeeze(x):
            return x.view(x.size(0), -1)

        if self.model_type in ('ViT', 'AlexNet', 'VGG', 'MobileNetV2',
                               'MobileNetV3', 'MNASNet'):
            return output
        if self.model_type in ('ResNet', 'Inception3', 'GoogLeNet'):
            return squeeze(output)
        if self.model_type in ('SqueezeNet', 'DenseNet', 'ShuffleNetV2',
                               'Xception'):
            return squeeze(pool(output))
        return output

    def _build(self) -> None:
        """Builds the interface model that outputs feature activations at the
        designated layers and/or logits. Intermediate layers are returned in
        the order of layers. Logits are returned last."""

        self.activation = {}

        def get_activation(name):
            def hook(model, input, output):
                self.activation[name] = output.detach()
            return hook

        if isinstance(self.layers, list):
            for la in self.layers:
                if la == 'postconv':
                    self._get_postconv().register_forward_hook(
                        get_activation('postconv')
                    )
                else:
                    getattr(self._model, la).register_forward_hook(
                        get_activation(la)
                    )
        elif self.layers is not None:
            raise errors.FeaturesError(f"Unrecognized type {type(self.layers)}"
                                       " for self.layers")

        # Calculate output and layer sizes
        rand_data = torch.rand(1, 3, self.tile_px, self.tile_px)
        output = self._model(rand_data.to(self.device))
        self.num_logits = output.shape[1] if self.include_logits else 0
        self.num_features = sum([f.shape[1] for f in self.activation.values()])

        if self.include_logits:
            log.debug(f'Number of logits: {self.num_logits}')
        log.debug(f'Number of activation features: {self.num_features}')


class UncertaintyInterface(Features):

    """Placeholder for uncertainty interface, which is not yet implemented for
    the PyTorch backend. Implementation is planned for a future update."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError
