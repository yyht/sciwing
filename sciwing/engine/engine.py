from torch.utils.data import Dataset
from torch.utils.data import DataLoader
import torch.nn as nn
import torch.optim as optim
from wasabi import Printer
from typing import Iterator, Callable, Any, List, Optional, Dict
from sciwing.meters.loss_meter import LossMeter
from tensorboardX import SummaryWriter
from sciwing.metrics.BaseMetric import BaseMetric
import numpy as np
import time
import logging
from torch.utils.data._utils.collate import default_collate
import torch
from sciwing.utils.tensor_utils import move_to_device
from copy import deepcopy
from sciwing.utils.class_nursery import ClassNursery
import logzero
import hashlib
import pathlib

try:
    import wandb
except ImportError:
    wandb = None


class Engine(ClassNursery):
    def __init__(
        self,
        model: nn.Module,
        train_dataset: Dataset,
        validation_dataset: Dataset,
        test_dataset: Dataset,
        optimizer: optim,
        batch_size: int,
        save_dir: str,
        num_epochs: int,
        save_every: int,
        log_train_metrics_every: int,
        metric: BaseMetric,
        experiment_name: Optional[str] = None,
        experiment_hyperparams: Optional[Dict[str, Any]] = None,
        tensorboard_logdir: str = None,
        track_for_best: str = "loss",
        collate_fn: Callable[[List[Any]], List[Any]] = default_collate,
        device=torch.device("cpu"),
        gradient_norm_clip_value: Optional[float] = 5.0,
        lr_scheduler: Optional[torch.optim.lr_scheduler._LRScheduler] = None,
        use_wandb: bool = False,
    ):

        if isinstance(device, str):
            device = torch.device(device)

        self.model = model
        self.train_dataset = train_dataset
        self.validation_dataset = validation_dataset
        self.test_dataset = test_dataset
        self.optimizer = optimizer
        self.batch_size = batch_size
        self.save_dir = pathlib.Path(save_dir)
        self.num_epochs = num_epochs
        self.msg_printer = Printer()
        self.save_every = save_every
        self.log_train_metrics_every = log_train_metrics_every
        self.tensorboard_logdir = tensorboard_logdir
        self.metric = metric
        self.summaryWriter = SummaryWriter(log_dir=tensorboard_logdir)
        self.track_for_best = track_for_best
        self.collate_fn = collate_fn
        self.device = device
        self.best_track_value = None
        self.set_best_track_value(self.best_track_value)
        self.gradient_norm_clip_value = gradient_norm_clip_value
        self.lr_scheduler = lr_scheduler
        self.lr_scheduler_is_plateau = isinstance(
            self.lr_scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau
        )
        self.use_wandb = wandb and use_wandb

        if experiment_name is None:
            hash_ = hashlib.sha1()
            hash_.update(str(time.time()).encode("utf-8"))
            digest = hash_.hexdigest()
            experiment_name = digest[:10]

        self.experiment_name = experiment_name
        self.experiment_hyperparams = experiment_hyperparams or {}

        if self.use_wandb:
            wandb.init(
                project="project-scwing",
                name=self.experiment_name,
                config=self.experiment_hyperparams,
            )

        if not self.save_dir.is_dir():
            self.save_dir.mkdir(parents=True)

        self.num_workers = 0
        self.model.to(self.device)

        self.train_loader = self.get_loader(self.train_dataset)
        self.validation_loader = self.get_loader(self.validation_dataset)
        self.test_loader = self.get_loader(self.test_dataset)

        # refresh the iters at the beginning of every epoch
        self.train_iter = None
        self.validation_iter = None
        self.test_iter = None

        # initializing loss meters
        self.train_loss_meter = LossMeter()
        self.validation_loss_meter = LossMeter()

        # get metric calculators
        self.train_metric_calc = deepcopy(metric)
        self.validation_metric_calc = deepcopy(metric)
        self.test_metric_calc = deepcopy(metric)

        self.msg_printer.divider("ENGINE STARTING")
        self.msg_printer.info(f"Number of training examples {len(self.train_dataset)}")
        self.msg_printer.info(
            f"Number of validation examples {len(self.validation_dataset)}"
        )
        self.msg_printer.info(
            f"Number of test examples {0}".format(len(self.test_dataset))
        )
        time.sleep(3)

        # get the loggers ready
        self.train_log_filename = self.save_dir.joinpath("train.log")
        self.validation_log_filename = self.save_dir.joinpath("validation.log")
        self.test_log_filename = self.save_dir.joinpath("test.log")

        self.train_logger = logzero.setup_logger(
            name="train-logger", logfile=self.train_log_filename, level=logging.INFO
        )
        self.validation_logger = logzero.setup_logger(
            name="valid-logger",
            logfile=self.validation_log_filename,
            level=logging.INFO,
        )
        self.test_logger = logzero.setup_logger(
            name="test-logger", logfile=self.test_log_filename, level=logging.INFO
        )

        if self.lr_scheduler_is_plateau:
            if self.best_track_value == "loss" and self.lr_scheduler.mode == "max":
                self.msg_printer.warn(
                    "You are optimizing loss and lr schedule mode is max instead of min"
                )
            if (
                self.best_track_value == "macro_fscore"
                and self.lr_scheduler.mode == "min"
            ):
                self.msg_printer.warn(
                    f"You are optimizing for macro_fscore and lr scheduler mode is min instead of max"
                )
            if (
                self.best_track_value == "micro_fscore"
                and self.lr_scheduler.mode == "min"
            ):
                self.msg_printer.warn(
                    f"You are optimizing for micro_fscore and lr scheduler mode is min instead of max"
                )

    def get_loader(self, dataset: Dataset) -> DataLoader:
        loader = DataLoader(
            dataset=dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            collate_fn=self.collate_fn,
            pin_memory=True,
        )
        return loader

    def is_best_lower(self, current_best=None):
        return True if current_best < self.best_track_value else False

    def is_best_higher(self, current_best=None):
        return True if current_best >= self.best_track_value else False

    def set_best_track_value(self, current_best=None):
        if self.track_for_best == "loss":
            self.best_track_value = np.inf if current_best is None else current_best
        elif self.track_for_best == "macro_fscore":
            self.best_track_value = 0 if current_best is None else current_best
        elif self.track_for_best == "micro_fscore":
            self.best_track_value = 0 if current_best is None else current_best

    def run(self):
        """
        Run the engine
        :return:
        """
        for epoch_num in range(self.num_epochs):
            self.train_epoch(epoch_num)
            self.validation_epoch(epoch_num)

        self.test_epoch(epoch_num)

    def train_epoch(self, epoch_num: int):
        """
        Run the training for one epoch
        :param epoch_num: type: int
        The current epoch number
        """

        # refresh everything necessary before training begins
        num_iterations = 0
        train_iter = self.get_iter(self.train_loader)
        self.train_loss_meter.reset()
        self.train_metric_calc.reset()
        self.model.train()

        self.msg_printer.info("starting training epoch")
        while True:
            try:
                # N*T, N * 1, N * 1
                iter_dict = next(train_iter)
                iter_dict = move_to_device(obj=iter_dict, cuda_device=self.device)
                labels = iter_dict["label"]
                batch_size = labels.size()[0]

                model_forward_out = self.model(
                    iter_dict, is_training=True, is_validation=False, is_test=False
                )
                self.train_metric_calc.calc_metric(
                    iter_dict=iter_dict, model_forward_dict=model_forward_out
                )

                try:
                    self.optimizer.zero_grad()
                    loss = model_forward_out["loss"]
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(
                        self.model.parameters(), max_norm=self.gradient_norm_clip_value
                    )
                    self.optimizer.step()
                    self.train_loss_meter.add_loss(loss.item(), batch_size)

                except KeyError:
                    self.msg_printer.fail(
                        "The model output dictionary does not have "
                        "a key called loss. Please check to have "
                        "loss in the model output"
                    )
                num_iterations += 1
                if (num_iterations + 1) % self.log_train_metrics_every == 0:
                    metrics = self.train_metric_calc.report_metrics()
                    print(metrics)
            except StopIteration:
                self.train_epoch_end(epoch_num)
                break

    def train_epoch_end(self, epoch_num: int):
        """

        Parameters
        ----------
        epoch_num : int
            The current epoch number (0 based)

        Returns
        -------
        None
        """
        self.msg_printer.divider("Training end @ Epoch {0}".format(epoch_num + 1))
        average_loss = self.train_loss_meter.get_average()
        self.msg_printer.text("Average Loss: {0}".format(average_loss))
        self.train_logger.info(
            "Average loss @ Epoch {0} - {1}".format(epoch_num + 1, average_loss)
        )
        metric = self.train_metric_calc.get_metric()

        # if wandb is not None:
        #     wandb.log({"train_loss": average_loss})
        #     if self.track_for_best != "loss":
        #         wandb.log({f"train_{self.track_for_best}": metric[self.track_for_best]})

        # save the model after every `self.save_every` epochs
        if (epoch_num + 1) % self.save_every == 0:
            torch.save(
                {
                    "epoch_num": epoch_num,
                    "optimizer_state": self.optimizer.state_dict(),
                    "model_state": self.model.state_dict(),
                    "loss": average_loss,
                },
                self.save_dir.joinpath(f"model_epoch_{epoch_num+1}.pt"),
            )

        # log loss to tensor board
        self.summaryWriter.add_scalars(
            "train_validation_loss",
            {"train_loss": average_loss or np.inf},
            epoch_num + 1,
        )

    def validation_epoch(self, epoch_num: int):
        self.model.eval()
        valid_iter = iter(self.validation_loader)
        self.validation_loss_meter.reset()
        self.validation_metric_calc.reset()

        while True:
            try:
                iter_dict = next(valid_iter)
                iter_dict = move_to_device(obj=iter_dict, cuda_device=self.device)
                labels = iter_dict["label"]
                batch_size = labels.size(0)

                with torch.no_grad():
                    model_forward_out = self.model(
                        iter_dict, is_training=False, is_validation=True, is_test=False
                    )
                loss = model_forward_out["loss"]
                self.validation_loss_meter.add_loss(loss, batch_size)
                self.validation_metric_calc.calc_metric(
                    iter_dict=iter_dict, model_forward_dict=model_forward_out
                )
            except StopIteration:
                self.validation_epoch_end(epoch_num)
                break

    def validation_epoch_end(self, epoch_num: int):

        self.msg_printer.divider(f"Validation @ Epoch {epoch_num+1}")

        metric_report = self.validation_metric_calc.report_metrics()

        average_loss = self.validation_loss_meter.get_average()
        print(metric_report)

        self.msg_printer.text(f"Average Loss: {average_loss}")

        self.validation_logger.info(
            f"Validation Loss @ Epoch {epoch_num+1} - {average_loss}"
        )

        if self.use_wandb:
            wandb.log({"validation_loss": average_loss})
            metric = self.validation_metric_calc.get_metric()
            if self.track_for_best != "loss":
                wandb.log(
                    {f"validation_{self.track_for_best}": metric[self.track_for_best]}
                )

        self.summaryWriter.add_scalars(
            "train_validation_loss",
            {"validation_loss": average_loss or np.inf},
            epoch_num + 1,
        )

        is_best: bool = None
        value_tracked: str = None
        if self.track_for_best == "loss":
            value_tracked = average_loss
            is_best = self.is_best_lower(average_loss)
        elif (
            self.track_for_best == "micro_fscore"
            or self.track_for_best == "macro_fscore"
        ):
            value_tracked = self.validation_metric_calc.get_metric()[
                self.track_for_best
            ]
            is_best = self.is_best_higher(current_best=value_tracked)

        if is_best:
            self.set_best_track_value(current_best=value_tracked)
            self.msg_printer.good(f"Found best model @ epoch {epoch_num + 1}")
            torch.save(
                {
                    "epoch_num": epoch_num,
                    "optimizer_state": self.optimizer.state_dict(),
                    "model_state": self.model.state_dict(),
                    "loss": average_loss,
                },
                self.save_dir.joinpath("best_model.pt"),
            )

    def test_epoch(self, epoch_num: int):
        self.msg_printer.divider("Running on test batch")
        self.load_model_from_file(self.save_dir.joinpath("best_model.pt"))
        self.model.eval()
        test_iter = iter(self.test_loader)
        while True:
            try:
                iter_dict = next(test_iter)
                iter_dict = move_to_device(obj=iter_dict, cuda_device=self.device)

                with torch.no_grad():
                    model_forward_out = self.model(
                        iter_dict, is_training=False, is_validation=False, is_test=True
                    )
                self.test_metric_calc.calc_metric(
                    iter_dict=iter_dict, model_forward_dict=model_forward_out
                )
            except StopIteration:
                self.test_epoch_end(epoch_num)
                break

    def test_epoch_end(self, epoch_num: int):
        metric_report = self.test_metric_calc.report_metrics()
        precision_recall_fmeasure = self.test_metric_calc.get_metric()
        self.msg_printer.divider("Test @ Epoch {0}".format(epoch_num + 1))
        print(metric_report)
        self.test_logger.info(
            f"Test Metrics @ Epoch {epoch_num+1} - {precision_recall_fmeasure}"
        )
        if self.use_wandb:
            metric = self.test_metric_calc.get_metric()
            wandb.run.summary[f"{self.track_for_best}"] = metric[
                f"{self.track_for_best}"
            ]

    def get_train_dataset(self):
        return self.train_dataset

    def get_validation_dataset(self):
        return self.validation_dataset

    def get_test_dataset(self):
        return self.test_dataset

    @staticmethod
    def get_iter(loader: DataLoader) -> Iterator:
        iterator = iter(loader)
        return iterator

    def load_model_from_file(self, filename: str):
        self.msg_printer.divider("LOADING MODEL FROM FILE")
        with self.msg_printer.loading(f"Loading Pytorch Model from file {filename}"):
            model_chkpoint = torch.load(filename)

        self.msg_printer.good("Finished Loading the Model")

        model_state = model_chkpoint["model_state"]
        self.model.load_state_dict(model_state)
