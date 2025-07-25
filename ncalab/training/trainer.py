from __future__ import annotations
import copy
import logging
from pathlib import Path, PosixPath  # for type hint
from typing import Callable, Dict, Optional, List, Tuple

import numpy as np

import torch  # type: ignore[import-untyped]
import torch.optim as optim  # type: ignore[import-untyped]
from torch.utils.data import DataLoader  # for type hint
from torch.utils.tensorboard import SummaryWriter  # for type hint

from matplotlib.figure import Figure  # type: ignore[import-untyped]

from tqdm import tqdm  # type: ignore[import-untyped]

from ..models.basicNCA import BasicNCAModel  # for type hint
from ..utils import pad_input

from .earlystopping import EarlyStopping
from .pool import Pool
from .trainingsummary import TrainingSummary


class BasicNCATrainer:
    """
    Trainer class for any model subclassing BasicNCA.
    """

    def __init__(
        self,
        nca: BasicNCAModel,
        model_path: Optional[Path | PosixPath] = None,
        gradient_clipping: bool = False,
        steps_range: tuple = (90, 110),
        steps_validation: int = 100,
        lr: Optional[float] = None,
        lr_gamma: float = 0.9999,
        adam_betas=(0.9, 0.99),
        batch_repeat: int = 2,
        max_epochs: int = 200,
        optimizer_method: str = "adamw",
        pool: Optional[Pool] = None,
    ):
        """
        Initialize trainer object.

        :param nca (BasicNCAModel): NCA model instance to train.
        :param model_path (Optional[str  |  Path  |  PosixPath], optional): Path to saved models. If None, models are not saved. Defaults to None.
        :param gradient_clipping (bool, optional): Whether to clip gradients. Defaults to False.
        :param steps_range (tuple, optional): Inclusive range of NCA time steps, randomized in each forward pass. Defaults to (90, 110).
        :param steps_validation (int, optional): Number of steps to use during validation. Defaults to 100.
        :param lr (float, optional): Initial learning rate. Defaults to 16e-4.
        :param lr_gamma (float, optional): Exponential learning rate decay. Defaults to 0.9999.
        :param adam_betas (tuple, optional): Beta values for Adam optimizer. Defaults to (0.9, 0.99).
        :param batch_repeat (int, optional): How often each batch will be duplicated. Defaults to 2.
        :param max_epochs (int, optional): Maximum number of epochs in training. Defaults to 200.
        :param optimizer_method: Optimization method. Defaults to 'adamw'.
        """
        assert batch_repeat >= 1
        assert steps_range[0] < steps_range[1]
        assert max_epochs > 0
        assert optimizer_method.lower() in (
            "adam",
            "adamw",
            "adagrad",
            "adafactor",
            "rmsprop",
            "sgd",
        )
        self.nca = nca
        self.model_path = model_path
        self.gradient_clipping = gradient_clipping
        self.steps_range = steps_range
        self.steps_validation = steps_validation
        self.lr_gamma = lr_gamma
        self.adam_betas = adam_betas
        self.batch_repeat = batch_repeat
        self.max_epochs = max_epochs
        self.optimizer_method = optimizer_method
        if lr is None:
            if optimizer_method.lower() == "sgd":
                self.lr = 1e-2
            elif optimizer_method.lower() in ("adam", "adamw"):
                self.lr = 16e-4
            elif optimizer_method.lower() == "rmsprop":
                self.lr = 1e-2
            elif optimizer_method.lower() == "adagrad":
                self.lr = 1e-2
            else:
                self.lr = 1e-2
        else:
            self.lr = lr
        self.pool = pool

    def info(self) -> str:
        """
        Shows a markdown-formatted info string with training parameters.
        Useful for showing info on tensorboard to keep track of parameter changes.

        :returns [str]: Markdown-formatted info string.
        """
        s = "BasicNCATrainer Info\n"
        s += "-------------------\n"
        for attribute in (
            "model_path",
            "lr",
            "lr_gamma",
            "gradient_clipping",
            "adam_betas",
            "batch_repeat",
            "max_epochs",
            "optimizer_method",
        ):
            attribute_f = attribute.title().replace("_", " ")
            s += f"**{attribute_f}:** {getattr(self, attribute)}\n"
        return s

    def train_iteration(
        self,
        x: torch.Tensor,
        y: torch.Tensor,
        steps: int,
        optimizer: torch.optim.Optimizer,
        scheduler: torch.optim.lr_scheduler.LRScheduler,
        total_batch_iterations: int,
        summary_writer,
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        """
        Run a single training iteration.

        :param x [Tensor]: Input training images.
        :param y [Tensor]: Input training labels.
        :param steps [int]: Number of NCA inference time steps.
        :param optimizer [torch.optim.Optimizer]: Optimizer.
        :param scheduler [torch.optim.lr_scheduler.LRScheduler]: Scheduler.
        :param total_batch_iterations [int]: Total training batch iterations

        :returns [Tensor]: Predicted image.
        """
        device = self.nca.device
        self.nca.train()
        optimizer.zero_grad()
        x_pred = x.clone().to(self.nca.device)
        x_pred = self.nca(x_pred, steps=steps)
        losses = self.nca.loss(x_pred, y.to(device))
        losses["total"].backward()

        if self.gradient_clipping:
            torch.nn.utils.clip_grad_norm_(self.nca.parameters(), 1.0)
        optimizer.step()
        scheduler.step()
        if summary_writer:
            for key in losses:
                summary_writer.add_scalar(
                    f"Loss/train_{key}", losses[key], total_batch_iterations
                )
        return x_pred, losses

    def train(
        self,
        dataloader_train: DataLoader,
        dataloader_val: Optional[DataLoader] = None,
        dataloader_test: Optional[DataLoader] = None,
        save_every: Optional[int] = None,
        summary_writer: Optional[SummaryWriter] = None,
        plot_function: Optional[
            Callable[[np.ndarray, np.ndarray, np.ndarray, BasicNCAModel], Figure]
        ] = None,
        earlystopping: Optional[EarlyStopping] = None,
    ) -> TrainingSummary:
        """
        Execute basic NCA training loop with a single function call.

        :param dataloader_train [DataLoader]: Training DataLoader
        :param dataloader_val [DataLoader]: Validation DataLoader
        :param save_every [int]: How often to save model state (in epochs). Useful for very small datasets, like growing lizard.
        :param summary_writer [SummaryWriter] Tensorboard SummaryWriter. Defaults to None.
        :param plot_function: Plot function override. If None, use model's default. Defaults to None.
        :param earlystopping (EarlyStopping, optional): EarlyStopping object. Defaults to None.

        :returns [TrainingSummary]: TrainingSummary object.
        """
        logging.basicConfig(encoding="utf-8", level=logging.INFO)

        if save_every is None:
            save_every = 1
        assert save_every > 0

        # Use default plot function for NCA flavor if no override is explicitly given
        if not plot_function:
            if self.nca.plot_function:
                plot_function = self.nca.plot_function

        if summary_writer is not None:
            summary_writer.add_text("Training Info", self.info())

        optimizer: None | optim.Optimizer = None
        if self.optimizer_method.lower() == "adamw":
            optimizer = optim.AdamW(
                self.nca.parameters(), lr=self.lr, betas=self.adam_betas
            )
        elif self.optimizer_method.lower() == "sgd":
            optimizer = optim.SGD(
                self.nca.parameters(), lr=self.lr, momentum=0.9, nesterov=True
            )
        elif self.optimizer_method.lower() == "rmsprop":
            optimizer = optim.RMSprop(self.nca.parameters(), lr=self.lr)
        elif self.optimizer_method.lower() == "adagrad":
            optimizer = optim.Adagrad(self.nca.parameters(), lr=self.lr)
        elif self.optimizer_method.lower() == "adafactor":
            optimizer = optim.Adafactor(self.nca.parameters(), lr=self.lr)
        else:
            optimizer = optim.Adam(
                self.nca.parameters(), lr=self.lr, betas=self.adam_betas
            )
        scheduler = optim.lr_scheduler.ExponentialLR(optimizer, self.lr_gamma)
        best_acc = 0.0
        best_training_loss: float = np.inf
        best_model = self.nca
        if self.model_path:
            best_path = Path(self.model_path).with_suffix(".best.pth")
        else:
            best_path = None

        # MAIN LOOP
        total_batch_iterations = 0
        for iteration in tqdm(range(self.max_epochs), desc="Epochs"):
            if earlystopping is not None:
                if earlystopping.done():
                    break

            # disable tqdm progress bar if dataset has only one sample, e.g. in the growing task
            gen = iter(dataloader_train)
            if len(dataloader_train) > 3:
                gen = tqdm(gen, desc="Batches")  # type: ignore[assignment]

            all_losses = []
            # TRAINING
            for sample in gen:
                x, y = sample  # x: BCWH

                # Typically, our dataloader supplies a binary, grayscale, RGB or RGBA image.
                # But the NCA operates on multiple hidden channels and output channels, so we
                # need to pad the input image with zeros.
                x = pad_input(x, self.nca, noise=self.nca.pad_noise)
                # Call model-specific input preparation hook
                x = self.nca.prepare_input(x)
                if self.pool is not None:
                    x = self.pool.sample(x)

                # Batch duplication, slightly stabelizes the training
                if self.batch_repeat > 1:
                    x = torch.cat(self.batch_repeat * [x])
                    y = torch.cat(self.batch_repeat * [y])

                steps = np.random.randint(*self.steps_range)
                x_pred, losses = self.train_iteration(
                    x,
                    y,
                    steps,
                    optimizer,
                    scheduler,
                    total_batch_iterations,
                    summary_writer,
                )
                with torch.no_grad():
                    total_batch_iterations += 1
                    if self.pool is not None:
                        self.pool.update(x_pred)
                    all_losses.append(losses["total"].item())

            with torch.no_grad():
                # SAVE TRAINING LOSS
                mean_training_loss = np.mean(all_losses).astype(float)
                if mean_training_loss < best_training_loss:
                    best_training_loss = mean_training_loss

                # VISUALIZATION
                # TODO visualize samples in training and validation batches
                if (
                    plot_function
                    and summary_writer
                    and (iteration + 1) % save_every == 0
                ):
                    figure = plot_function(
                        x.detach().cpu().numpy(),
                        x_pred.detach().cpu().numpy(),
                        y.detach().cpu().numpy(),
                        self.nca,
                    )
                    summary_writer.add_figure("Training Batch", figure, iteration)

                # VALIDATION
                self.nca.eval()
                if self.model_path:
                    torch.save(self.nca.state_dict(), self.model_path)

                if dataloader_val:
                    all_metrics: Dict[str, List[float]] = {}
                    for sample in dataloader_val:
                        x, y = sample
                        # TODO: move validation/inference steps parameter to NCA model itself
                        metrics, _ = self.nca.validate(x, y, self.steps_validation)
                        for name in metrics:
                            if name == "prediction":
                                continue
                            if name not in all_metrics:
                                all_metrics[name] = []
                            all_metrics[name].append(metrics[name])
                    avg_metrics: Dict[str, float] = {}
                    for name in all_metrics:
                        avg_metrics[name] = float(np.mean(all_metrics[name]))
                        if summary_writer is not None:
                            summary_writer.add_scalar(
                                f"Acc/Val/{name}", avg_metrics[name], iteration
                            )
                    if self.nca.validation_metric in avg_metrics:
                        val_acc = avg_metrics.get(self.nca.validation_metric, 0)
                        if val_acc > best_acc:
                            logging.info(
                                f"Accuracy improvement: {best_acc:.5f} --> {val_acc:.5f}"
                            )
                            logging.info(f"  In Epoch {iteration}")
                            if best_path:
                                torch.save(self.nca.state_dict(), best_path)
                            best_acc = val_acc
                            best_model = copy.deepcopy(self.nca)
                        if earlystopping is not None:
                            earlystopping.step(val_acc)
        # After training: Compute metrics on test set for training summary
        with torch.no_grad():
            metrics = {}
            if dataloader_test is not None:
                best_model.eval()
                for image, label in dataloader_test:
                    metrics.update(
                        best_model.metrics(
                            image.to(self.nca.device), label.to(self.nca.device)
                        )
                    )
        return TrainingSummary(best_acc, best_path, best_training_loss, metrics)
