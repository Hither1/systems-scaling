from __future__ import annotations

import cProfile
import gc
import logging
import math
import os
import random
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from pstats import SortKey
from typing import Any, Callable, Deque, Dict, List, Optional, TextIO, Tuple

import numpy as np
import torch
import torch.distributed as dist
import torch.nn.functional as F
import wandb
from packaging import version
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.utils.data import DataLoader

from .aliases import PathOrStr
from .checkpoint import Checkpointer, FullCheckpointer, build_sharded_checkpointer
from .config import (
    CheckpointType,
    SchedulerUnits,
    ShardedCheckpointerType,
    SpeedMonitorConfig,
    TrainConfig,
)
from .data import IterableDataset
from .eval import Evaluator
from .exceptions import OLMoConfigurationError
from .model import OLMo

from .optim import Optimizer, Scheduler
from .torch_util import (
    barrier,
    gc_cuda,
    get_fs_local_rank,
    get_global_rank,
    get_world_size,
    move_to_device,
    peak_gpu_memory,
    synchronize_flag,
    synchronize_value,
)
from .util import upload


import torch.distributed as dist
from collections import defaultdict


__all__ = ["SpeedMonitor", "LRMonitor", "Trainer"]

log = logging.getLogger(__name__)

@dataclass
class SpeedMonitor:
    cfg: SpeedMonitorConfig
    start_times: Deque[float] = field(default_factory=lambda: deque([]))
    global_total_tokens: int = 0
    device_interval_tokens: Deque[int] = field(default_factory=lambda: deque([]))

    def batch_start(self, global_total_tokens: int, device_batch_num_tokens: int, record: bool = True) -> None:
        self.global_total_tokens = global_total_tokens
        if record:
            if len(self.start_times) >= self.cfg.window_size:
                self.start_times.popleft()
                self.device_interval_tokens.popleft()
            self.start_times.append(time.monotonic())
            self.device_interval_tokens.append(device_batch_num_tokens)

    def reset(self) -> None:
        self.start_times.clear()
        self.device_interval_tokens.clear()

    def check(self) -> Dict[str, float]:
        metrics: Dict[str, float] = {"throughput/total_tokens": self.global_total_tokens}
        if self.start_times:
            interval_seconds = time.monotonic() - self.start_times[0]
            interval_batches = len(self.start_times)
            interval_tokens = sum(self.device_interval_tokens)
            metrics["throughput/device/tokens_per_second"] = interval_tokens / interval_seconds
            metrics["throughput/device/batches_per_second"] = interval_batches / interval_seconds
        return metrics


@dataclass
class LRMonitor:
    optim: torch.optim.Optimizer

    def check(self) -> Dict[str, float]:
        lrs = [group["lr"] for group in self.optim.param_groups]
        return {f"optim/learning_rate_group{idx}": lr for idx, lr in enumerate(lrs)}
    

def count_clipped_values(t: torch.Tensor,
                         block_size: int,
                         fmt: str
) -> tuple[int, int]:
    """
    Return (#clipped, #total) for a tensor that would be quantised
    MX block-wise with format `fmt`.
    """
    block_size = int(block_size)
    FORMAT_META = {
        "fp8_e4m3": dict(emax=8,   max_normal=448.0),   # 1111 110₂ × 2⁸ × 1.75
        "fp8_e5m2": dict(emax=15,  max_normal=57344.0),
        "fp6_e3m2": dict(emax=7,   max_normal=224.0),
        "fp4_e2m1": dict(emax=3,   max_normal=6.0),
    }
    
    def fmt_meta(fmt):
        if fmt not in FORMAT_META:
            raise ValueError(f"unknown MX format {fmt}")
        return FORMAT_META[fmt]["emax"], FORMAT_META[fmt]["max_normal"]
    
    emax, vmax = fmt_meta(fmt)           # vmax = max representable after scaling
    flat = t.detach().abs().view(-1, block_size)   #   (n_blocks, block_size)
    max_per_block = flat.max(dim=1, keepdim=True).values
    scales = 2.0 ** (torch.floor(torch.log2(max_per_block)) - emax)
    clipped = (flat > vmax * scales).sum().item()
    total   = flat.numel()
    return clipped, total



def save_instability_tensors(tensor, export_dir, global_step, blk_idx, name, step_threshold_min, step_threshold_max):
    """
    A useful helper function to call around when you have an instability to save stuff. It saves things when global
    train step is between step_threshold_min and step_threshold_max.
    """
    if global_step >= step_threshold_min and global_step <= step_threshold_max:
        torch.save(tensor, export_dir/f"step{global_step:05d}_blk{blk_idx:02d}_{name}.pt" )
    else:
        pass


def cross_entropy_loss(
    logits, labels, ignore_index: int = -100, reduction: str = "mean", compute_z_loss: bool = False
):
    loss = F.cross_entropy(logits, labels, ignore_index=ignore_index, reduction=reduction)

    if not compute_z_loss:
        return loss, None

    z_squared = logits.logsumexp(-1).pow(2)
    if reduction == "mean":
        z_squared = (z_squared * (labels != ignore_index)).mean()
    elif reduction == "sum":
        z_squared = (z_squared * (labels != ignore_index)).sum()

    z_loss = 1e-5 * z_squared

    return loss, z_loss


@dataclass
class Trainer:
    cfg: TrainConfig
    model: OLMo
    fsdp_model: FSDP
    optim: Optimizer
    scheduler: Scheduler
    train_loader: DataLoader
    device: torch.device
    evaluators: List[Evaluator]
    epoch: Optional[int] = None
    global_step: int = 0
    global_train_examples_seen_this_epoch: int = 0
    """Tracks the global number of training examples seen in the current epoch for the purpose of restoring
    the data loader position on restarts."""
    global_train_tokens_seen: int = 0
    """Tracks the global total number of tokens trained on."""
    checkpoints: List[Path] = field(default_factory=list)
    unsharded_checkpoints: List[Path] = field(default_factory=list)
    ephemeral_checkpoints: List[Path] = field(default_factory=list)
    min_train_loss: float = float("inf")
    cur_train_loss: float = float("inf")
    indices_file: Optional[TextIO] = None
    _start_time: float = 0.0
    _gc_init_state: bool = True
    loss_fn: Callable[..., torch.Tensor] = field(default_factory=lambda: cross_entropy_loss)  # type: ignore
    last_sharded_checkpoint_step: Optional[int] = None
    last_unsharded_checkpoint_step: Optional[int] = None

    def __post_init__(self):
        if self.cfg.fused_loss:
            import flash_attn
            from flash_attn.ops.triton.cross_entropy import (  # type: ignore
                cross_entropy_loss,
            )

            # The `ignored_index` parameter of `cross_entropy_loss` was changed to `ignore_index` in v2.5.8 with commit https://github.com/Dao-AILab/flash-attention/commit/ec6d22143b5d375e253b2ebfc563b26a43f43684
            ce_loss_use_ignore_index_param = version.parse(flash_attn.__version__) >= version.parse("2.5.8")

            def fused_loss_fn(
                logits, labels, ignore_index: int = -100, reduction: str = "mean", compute_z_loss: bool = False
            ):
                if ce_loss_use_ignore_index_param:
                    ignore_index_kwarg = {"ignore_index": ignore_index}
                else:
                    ignore_index_kwarg = {"ignored_index": ignore_index}

                loss, z_loss = cross_entropy_loss(
                    logits,
                    labels,
                    label_smoothing=0.0,
                    logit_scale=1.0,
                    lse_square_scale=0.0,
                    ignored_index=ignore_index,
                    inplace_backward=False,
                    process_group=None,
                    **ignore_index_kwarg,
                )

                mask = labels != ignore_index

                if reduction == "mean":
                    loss = loss.sum() / mask.sum()
                elif reduction == "sum":
                    loss = loss.sum()
                else:
                    loss = loss

                if not compute_z_loss:
                    return loss, None

                if reduction == "mean":
                    z_loss = z_loss.sum() / mask.sum()
                elif reduction == "sum":
                    z_loss = z_loss.sum()
                else:
                    z_loss = z_loss

                return loss, z_loss

            self.loss_fn = fused_loss_fn
        
        # for activation clipping stats: layer_idx -> [clipped, total]
        self._act_clip_stats: Dict[int, List[int]] = defaultdict(lambda: [0, 0])
        self._act_hooks: List[torch.utils.hooks.RemovableHandle] = []
        # register hooks on each block.act
        for blk_idx, blk in enumerate(self.model.transformer.blocks):
            h = blk.act.register_forward_hook(
                lambda _mod, _inp, out, i=blk_idx: self._record_act_clip(i, out)
            )
            self._act_hooks.append(h)
    
    def _record_act_clip(self, layer_idx: int, act: torch.Tensor):
        # run on CPU to avoid GPU oOom.
        bs = int(self.cfg.model.block_size)
        clipped, total = count_clipped_values(
            act.detach().cpu(),
            bs,
            self.cfg.model.a_mx_format,
        )
        self._act_clip_stats[layer_idx][0] += clipped
        self._act_clip_stats[layer_idx][1] += total

    @property
    def dataset(self) -> IterableDataset:
        assert isinstance(self.train_loader.dataset, IterableDataset)
        return self.train_loader.dataset

    @property
    def tokens_per_batch(self) -> int:
        return self.cfg.training.batch_size * self.cfg.model.context_length

    @property
    def batches_per_epoch(self) -> int:
        if self.dataset.total_size == np.inf:
            return np.inf
        return self.dataset.total_size // self.cfg.training.batch_size

    @property
    def max_epochs(self) -> int:
        if isinstance(self.cfg.max_duration, str) and self.cfg.max_duration.endswith("ep"):
            return int(self.cfg.max_duration[:-2].strip())
        else:
            return max(int(math.ceil(self.max_steps / self.batches_per_epoch)), 1)

    @property
    def max_steps(self) -> int:
        if isinstance(self.cfg.max_duration, int):
            return self.cfg.max_duration
        elif isinstance(self.cfg.max_duration, str):
            if self.cfg.max_duration.endswith("T"):
                # convert to float *first* to handle scientific notation
                max_tokens = int(float(self.cfg.max_duration[:-1].strip()))
                tokens_remaining = max(max_tokens - self.global_train_tokens_seen, 0)
                steps_remaining = tokens_remaining // self.tokens_per_batch
                return self.global_step + steps_remaining
            elif self.cfg.max_duration.endswith("ep"):
                max_epochs = int(self.cfg.max_duration[:-2].strip())
                return max_epochs * self.batches_per_epoch
            else:
                # convert to float *first* to handle scientific notation
                return int(float(self.cfg.max_duration))
        else:
            raise TypeError(f"expected int or str for 'max_duration', found {type(self.cfg.max_duration)}")

    @property
    def max_tokens(self) -> int:
        if isinstance(self.cfg.max_duration, int):
            return (
                self.global_train_tokens_seen
                + max(self.cfg.max_duration - self.global_step, 0) * self.tokens_per_batch
            )
        elif isinstance(self.cfg.max_duration, str):
            if self.cfg.max_duration.endswith("T"):
                # convert to float *first* to handle scientific notation
                return int(float(self.cfg.max_duration[:-1].strip()))
            elif self.cfg.max_duration.endswith("ep"):
                max_epochs = int(self.cfg.max_duration[:-2].strip())
                return max_epochs * self.batches_per_epoch * self.tokens_per_batch
            else:
                # convert to float *first* to handle scientific notation
                return (
                    self.global_train_tokens_seen
                    + max(int(float(self.cfg.max_duration)) - self.global_step, 0) * self.tokens_per_batch
                )
        else:
            raise TypeError(f"expected int or str for 'max_duration', found {type(self.cfg.max_duration)}")

    @property
    def scheduler_current(self) -> int:
        if self.cfg.scheduler.units == SchedulerUnits.steps:
            return self.global_step
        elif self.cfg.scheduler.units == SchedulerUnits.tokens:
            return self.global_train_tokens_seen
        else:
            raise NotImplementedError(self.cfg.scheduler.units)

    @property
    def scheduler_max(self) -> int:
        if self.cfg.scheduler.units == SchedulerUnits.steps:
            return self.max_steps
        elif self.cfg.scheduler.units == SchedulerUnits.tokens:
            return self.max_tokens
        else:
            raise NotImplementedError(self.cfg.scheduler.units)

    def trainer_state_dict(self) -> Dict[str, Any]:
        return {
            "epoch": self.epoch,
            "global_step": self.global_step,
            "global_train_examples_seen_this_epoch": self.global_train_examples_seen_this_epoch,
            "global_train_tokens_seen": self.global_train_tokens_seen,
            "world_size": get_world_size(),
            "checkpoints": self.checkpoints,
            "unsharded_checkpoints": self.unsharded_checkpoints,
            "ephemeral_checkpoints": self.ephemeral_checkpoints,
            "rng": {
                "python": random.getstate(),
                "numpy": np.random.get_state(),
                "torch": torch.random.get_rng_state(),
                "cuda": torch.cuda.get_rng_state(),
            },
        }

    def load_trainer_state_dict(self, state_dict: Dict[str, Any]) -> None:
        # Checkpoint paths.
        self.checkpoints = [
            path
            for path in state_dict["checkpoints"]
            if path.is_dir() and path.resolve().parent == Path(self.cfg.save_folder).resolve()
        ]
        self.unsharded_checkpoints = [
            path
            for path in state_dict["unsharded_checkpoints"]
            if path.is_dir() and path.resolve().parent == Path(self.cfg.save_folder).resolve()
        ]
        self.ephemeral_checkpoints = [
            path
            for path in state_dict.get("ephemeral_checkpoints", [])
            if path.is_dir() and path.resolve().parent == Path(self.cfg.save_folder).resolve()
        ]

        # Dataset / dataloader position.
        checkpoint_epoch = state_dict.get("epoch", 0)
        self.global_step = state_dict["global_step"]
        self.global_train_examples_seen_this_epoch = state_dict.get(
            "global_train_examples_seen_this_epoch",
            state_dict.get(  # for backwards compatibility
                "global_train_examples_seen",
                state_dict.get("global_data_step", self.global_step) * self.cfg.training.batch_size,
            ),
        )
        self.global_train_tokens_seen = state_dict.get(
            "global_train_tokens_seen",
            state_dict.get("global_data_step", self.global_step)  # for backwards compatibility
            * self.cfg.training.batch_size
            * self.cfg.model.context_length,
        )

        if not self.cfg.restore_dataloader:
            self.epoch = 0
            self.global_train_tokens_seen = 0
            self.global_train_examples_seen_this_epoch = 0
        elif self.epoch is None:
            self.epoch = checkpoint_epoch
        elif checkpoint_epoch != self.epoch:
            log.info(f"Starting new epoch (epoch = {self.epoch})")
            self.global_train_examples_seen_this_epoch = 0

        if self.cfg.fast_forward_batches:
            log.info(f"Fast-forwarding data loader by {self.cfg.fast_forward_batches:,d} steps")
            # Technically we don't "see" these batches that we fast-forward through, but we use
            # this variable to update the position of the dataset so we need to include them here.
            self.global_train_examples_seen_this_epoch += (
                self.cfg.fast_forward_batches * self.cfg.training.batch_size
            )
            # NOTE: on the other hand we don't add anything to 'self.global_train_tokens_seen' here because
            # that variable is meant to track the actual number of tokens trained on.

        if self.global_train_examples_seen_this_epoch > 0:
            assert isinstance(self.dataset, IterableDataset)
            log.info(f"Data loader will start at instance index {self.global_train_examples_seen_this_epoch:,d}")
            self.dataset.start_index = self.global_train_examples_seen_this_epoch

        # Reset learning rate and weight decay to the values from the config, not the checkpoint.
        log.info("Resetting learning rate...")
        new_learning_rate = self.scheduler.get_lr(
            self.cfg.optimizer.learning_rate, self.scheduler_current, self.scheduler_max
        )
        for group in self.optim.param_groups:
            group["lr"] = new_learning_rate
            group["initial_lr"] = self.cfg.optimizer.learning_rate
            if "weight_decay" in group and group["weight_decay"] > 0.0:
                group["weight_decay"] = self.cfg.optimizer.weight_decay

        # RNG states.
        if "rng" in state_dict and state_dict.get("world_size", get_world_size()) == get_world_size():
            log.info("Restoring RNG states...")
            rng_state = state_dict["rng"]
            self.restore_rng_state(rng_state)
        else:
            log.warning(
                "Trainer will not restore RNG states since the RNG states in the checkpoint are missing or invalid. "
                "This typically happens when restoring from an unsharded checkpoint or a checkpoint that was saved "
                "with a different world size. If that's the case you can safely ignore this warning."
            )

    def restore_rng_state(self, rng_state: Dict[str, Any]) -> None:
        random.setstate(rng_state["python"])
        np.random.set_state(rng_state["numpy"])
        torch.set_rng_state(rng_state["torch"])
        torch.cuda.set_rng_state(rng_state["cuda"])

    def _save_checkpoint(
        self, checkpointer: Checkpointer, checkpoint_type: CheckpointType
    ) -> Tuple[PathOrStr, Optional[PathOrStr]]:
        if checkpoint_type == CheckpointType.sharded:
            suffix = ""
            current_checkpoints = self.checkpoints
            link_latest = get_fs_local_rank() == 0
            num_checkpoints_to_keep = self.cfg.save_num_checkpoints_to_keep
        elif checkpoint_type == CheckpointType.unsharded:
            suffix = "-unsharded"
            current_checkpoints = self.unsharded_checkpoints
            link_latest = get_global_rank() == 0
            num_checkpoints_to_keep = self.cfg.save_num_unsharded_checkpoints_to_keep
        elif checkpoint_type == CheckpointType.sharded_ephemeral:
            suffix = ""
            current_checkpoints = self.ephemeral_checkpoints
            link_latest = get_fs_local_rank() == 0
            num_checkpoints_to_keep = 1
        else:
            raise NotImplementedError(checkpoint_type)

        # Zero-gradients to avoid gathering them.
        self.optim.zero_grad(set_to_none=True)

        # Flush data indices file.
        # TODO: upload the indices files?
        if self.indices_file is not None:
            self.indices_file.flush()

        checkpoint_dir = Path(self.cfg.save_folder) / f"step{self.global_step}{suffix}"
        remote_checkpoint_dir: Optional[str] = None
        if self.cfg.remote_save_folder is not None:
            remote_checkpoint_dir = f"{self.cfg.remote_save_folder.rstrip('/')}/{checkpoint_dir.name}"
        current_checkpoints.append(checkpoint_dir)

        # Save the checkpoint.
        try:
            checkpointer.save_checkpoint(
                checkpoint_dir,
                self.fsdp_model,
                self.optim,
                self.trainer_state_dict(),
                upload_to=remote_checkpoint_dir,
            )
        except FileExistsError:
            raise OLMoConfigurationError(
                f"Checkpoint for step {self.global_step} already exists, use --save-overwrite to overwrite it"
            )

        if link_latest:
            # Link to 'latest'.
            latest_path = Path(self.cfg.save_folder) / f"latest{suffix}"
            latest_path.unlink(missing_ok=True)
            try:
                latest_path.symlink_to(checkpoint_dir.name, target_is_directory=True)
            except FileExistsError:
                # Same as above, caught when another (file-system) local rank 0 has already made the 'latest' symlink.
                # This can happen when nodes are saving to a common NFS drive but otherwise have distinct
                # file-systems.
                if latest_path.resolve().name != checkpoint_dir.name:
                    raise

        # Remove old checkpoints.
        if num_checkpoints_to_keep > 0:
            while len(current_checkpoints) > num_checkpoints_to_keep:
                self.remove_checkpoint(0, checkpoint_type)

        barrier()

        if remote_checkpoint_dir is not None:
            return remote_checkpoint_dir, checkpoint_dir
        else:
            return checkpoint_dir, None

    def save_sharded_checkpoint(self) -> Tuple[PathOrStr, Optional[PathOrStr]]:
        checkpointer = build_sharded_checkpointer(self.cfg)
        result = self._save_checkpoint(checkpointer, CheckpointType.sharded)
        self.last_sharded_checkpoint_step = self.global_step
        return result

    def save_ephemeral_checkpoint(self) -> Tuple[PathOrStr, Optional[PathOrStr]]:
        checkpointer = build_sharded_checkpointer(self.cfg)
        result = self._save_checkpoint(checkpointer, CheckpointType.sharded_ephemeral)
        self.last_sharded_checkpoint_step = self.global_step
        return result

    def _remove_sharded_checkpoint(self, idx: int, checkpoints: List[Path]):
        oldest_checkpoint = checkpoints.pop(idx)
        barrier()
        if get_fs_local_rank() == 0 and oldest_checkpoint.is_dir():
            shutil.rmtree(oldest_checkpoint, ignore_errors=True)
            latest_path = Path(self.cfg.save_folder) / "latest"
            if latest_path.resolve() == oldest_checkpoint.resolve():
                latest_path.unlink()
        barrier()

    def remove_sharded_checkpoint(self, idx: int = 0):
        self._remove_sharded_checkpoint(idx, self.checkpoints)

    def remove_ephemeral_checkpoint(self, idx: int = 0):
        self._remove_sharded_checkpoint(idx, self.ephemeral_checkpoints)

    def restore_sharded_checkpoint(
        self,
        load_path: PathOrStr,
        local_cache: Optional[PathOrStr] = None,
        *,
        load_optimizer_state: bool = True,
        load_trainer_state: bool = True,
        sharded_checkpointer: Optional[ShardedCheckpointerType] = None,
    ):
        # Zero-gradients to avoid gathering them.
        self.optim.zero_grad(set_to_none=True)
        checkpointer = build_sharded_checkpointer(self.cfg, name=sharded_checkpointer)
        trainer_state = checkpointer.restore_checkpoint(
            load_path,
            self.fsdp_model,
            self.optim,
            local_cache=local_cache,
            load_optimizer_state=load_optimizer_state,
        )
        if load_trainer_state:
            self.load_trainer_state_dict(trainer_state)
        barrier()

    def save_unsharded_checkpoint(self) -> Tuple[PathOrStr, Optional[PathOrStr]]:
        checkpointer = FullCheckpointer(self.cfg)
        result = self._save_checkpoint(checkpointer, CheckpointType.unsharded)
        self.last_unsharded_checkpoint_step = self.global_step
        return result

    def remove_unsharded_checkpoint(self, idx: int = 0):
        barrier()
        oldest_checkpoint = self.unsharded_checkpoints.pop(idx)
        if get_global_rank() == 0 and oldest_checkpoint.is_dir():
            shutil.rmtree(oldest_checkpoint, ignore_errors=True)
            latest_path = Path(self.cfg.save_folder) / "latest-unsharded"
            if latest_path.resolve() == oldest_checkpoint.resolve():
                latest_path.unlink()
        barrier()

    def restore_unsharded_checkpoint(
        self,
        load_path: PathOrStr,
        local_cache: Optional[PathOrStr] = None,
        *,
        load_optimizer_state: bool = True,
        load_trainer_state: bool = True,
    ):
        # Zero-gradients to avoid gathering them.
        if self.optim is not None:
            self.optim.zero_grad(set_to_none=True)
        checkpointer = FullCheckpointer(self.cfg)
        trainer_state = checkpointer.restore_checkpoint(
            load_path,
            self.fsdp_model,
            self.optim,
            local_cache=local_cache,
            load_optimizer_state=load_optimizer_state,
        )
        if load_trainer_state:
            self.load_trainer_state_dict(trainer_state)
        barrier()

    def save_checkpoint(
        self, checkpoint_type: CheckpointType = CheckpointType.sharded
    ) -> Tuple[PathOrStr, Optional[PathOrStr]]:
        result: Tuple[PathOrStr, Optional[PathOrStr]]
        if checkpoint_type == CheckpointType.sharded:
            result = self.save_sharded_checkpoint()
        elif checkpoint_type == CheckpointType.unsharded:
            result = self.save_unsharded_checkpoint()
        elif checkpoint_type == CheckpointType.sharded_ephemeral:
            result = self.save_ephemeral_checkpoint()
        else:
            raise NotImplementedError(checkpoint_type)

        gc_cuda()
        return result

    def restore_checkpoint(
        self,
        load_path: PathOrStr,
        *,
        checkpoint_type: Optional[CheckpointType] = None,
        local_cache: Optional[PathOrStr] = None,
        load_optimizer_state: bool = True,
        load_trainer_state: bool = True,
        sharded_checkpointer: Optional[ShardedCheckpointerType] = None,
    ):
        if checkpoint_type == CheckpointType.unsharded or (
            checkpoint_type is None and str(load_path).rstrip("/").endswith("-unsharded")
        ):
            self.restore_unsharded_checkpoint(
                load_path,
                local_cache=local_cache,
                load_optimizer_state=load_optimizer_state,
                load_trainer_state=load_trainer_state,
            )
        elif checkpoint_type == CheckpointType.sharded or checkpoint_type is None:
            self.restore_sharded_checkpoint(
                load_path,
                local_cache=local_cache,
                load_optimizer_state=load_optimizer_state,
                load_trainer_state=load_trainer_state,
                sharded_checkpointer=sharded_checkpointer,
            )
        elif checkpoint_type is not None:
            raise NotImplementedError(checkpoint_type)

        gc_cuda()

    def remove_checkpoint(self, idx: int = 0, checkpoint_type: CheckpointType = CheckpointType.sharded):
        if checkpoint_type == CheckpointType.sharded:
            self.remove_sharded_checkpoint(idx=idx)
        elif checkpoint_type == CheckpointType.unsharded:
            self.remove_unsharded_checkpoint(idx=idx)
        elif checkpoint_type == CheckpointType.sharded_ephemeral:
            self.remove_ephemeral_checkpoint(idx=idx)
        else:
            raise NotImplementedError(checkpoint_type)

    def get_labels(self, batch: Dict[str, Any]) -> torch.Tensor:
        # Labels are just input IDs shifted to the left (first item is ignored).
        labels, label_mask, attention_mask = (
            batch["input_ids"].clone(),
            batch.get("label_mask"),
            batch.get("attention_mask"),
        )
        if label_mask is not None:
            labels.masked_fill_(~label_mask, -100)
        if attention_mask is not None:
            labels.masked_fill_(attention_mask == 0.0, -100)
        return labels[..., 1:].contiguous()

    def model_forward(
        self,
        batch: Dict[str, Any],
        loss_reduction: str = "mean",
        compute_z_loss: bool = False,
        return_logits: bool = True,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], torch.Tensor]:
        # shape: (batch_size, seq_len, vocab_size)
        logits = self.fsdp_model(
            input_ids=batch["input_ids"],
            attention_mask=batch.get("attention_mask"),
            attention_bias=batch.get("attention_bias"),
        ).logits
        logits_for_loss = logits[..., :-1, :].contiguous()
        # shape: (batch_size * seq_len, vocab_size)
        logits_for_loss = logits_for_loss.view(-1, logits_for_loss.size(-1))
        # shape: (batch_size, seq_len)
        labels = self.get_labels(batch)
        # shape: (batch_size * seq_len,)
        labels = labels.view(-1)
        ce_loss, z_loss = self.loss_fn(
            logits_for_loss, labels, ignore_index=-100, reduction=loss_reduction, compute_z_loss=compute_z_loss
        )
        if loss_reduction == "none":
            # Reshape (batch_size * seq_len,) -> (batch_size, seq_len)
            ce_loss = ce_loss.view(batch["input_ids"].shape[0], -1)
            if z_loss is not None:
                z_loss = z_loss.view(batch["input_ids"].shape[0], -1)
        if return_logits:
            return ce_loss, z_loss, logits
        else:
            return ce_loss, z_loss

    def train_batch(self, batch: Dict[str, Any]) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
        # Split into micro-batches.
        micro_batches = self.split_batch(batch)

        # In case this helps with memory utilization.
        del batch

        ce_batch_loss = torch.tensor(0.0, device=self.device)
        z_batch_loss = None if not self.cfg.softmax_auxiliary_loss else torch.tensor(0.0, device=self.device)
        for micro_batch in micro_batches:
            with torch.autocast("cuda", enabled=True, dtype=self.cfg.autocast_precision):
                # Run forward pass.
                ce_loss, z_loss, logits = self.model_forward(
                    micro_batch, compute_z_loss=self.cfg.softmax_auxiliary_loss
                )
                ce_loss = ce_loss / len(micro_batches)

                # In case this helps with memory utilization.
                del micro_batch

                # Update overall CE batch loss.
                ce_batch_loss += ce_loss.detach()

                # Get loss to optimize for.
                if self.cfg.softmax_auxiliary_loss:
                    assert z_loss is not None
                    assert z_batch_loss is not None
                    z_loss = z_loss / len(micro_batches)
                    loss = ce_loss + z_loss

                    # Update overall Z batch loss.
                    z_batch_loss += z_loss.detach()
                else:
                    loss = ce_loss

                del logits

            # Run backward pass.
            loss.backward()

        
       



        return ce_batch_loss, z_batch_loss

    def optim_step(self, should_log_metrics: bool = False):
        optim_metrics = self.optim.clip_grads_and_collect_metrics(
            self.global_step,
            collect_param_metrics=should_log_metrics,
            # passing this process group here ensures metrics are reduced correctly when we're using
            # HYBRID sharding.
            process_group=self.fsdp_model.process_group,
        )
        # Adjust the learning rate.
        for group in self.optim.param_groups:
            # TODO (epwalsh): if we want to enable different LRs or gradient clipping settings per group
            # we should pass `group["initial_lr"]` or `group["initial_max_grad_norm"]` here instead of
            # the corresponding values from `self.cfg`.
            group["lr"] = self.scheduler.get_lr(
                self.cfg.optimizer.learning_rate, self.scheduler_current, self.scheduler_max
            )
            group["max_grad_norm"] = self.scheduler.get_max_grad_norm(
                self.cfg.max_grad_norm, self.scheduler_current, self.scheduler_max
            )
            group["max_grad_norm_ratio"] = self.scheduler.get_max_grad_norm(
                self.cfg.max_grad_norm_ratio, self.scheduler_current, self.scheduler_max
            )
        # Optimizer step.
        self.optim.step()
        return optim_metrics
    
    def collect_layernorm_clipping_metrics(self, model: FSDP) -> Dict[str, float]:
        """Collect metrics about layernorm weight clipping for MX quantization."""
        metrics = {}
        
        
        # Only collect if we have MX formats configured
        if not hasattr(self.cfg.model, 'w_mx_format'):
            return metrics
        
        total_clipped = 0
        total_weights = 0
        block_size = 16  # Default block size for MX quantization
        
        # Iterate through all modules and find LayerNorm instances
        with FSDP.summon_full_params(self.fsdp_model, writeback=False, rank0_only=True):
            if get_global_rank() == 0:
                for name, module in self.fsdp_model.named_modules():
                    # Check if this is a LayerNorm module by its class name
                    if module.__class__.__name__ == 'LayerNorm':
                        for param_name, param in module.named_parameters():
                            if 'weight' in param_name:
                                try:
                                    clipped, total = count_clipped_values(
                                        param.data, 
                                        block_size, 
                                        self.cfg.model.w_mx_format
                                    )
                                    total_clipped += clipped
                                    total_weights += total
                                    
                                    # Log per-layer metrics for detailed analysis
                                    layer_clip_rate = clipped / total if total > 0 else 0.0
                                    metrics[f"layernorm_clipping/{name}.{param_name}_clip_rate"] = layer_clip_rate
                                    metrics[f"layernorm_clipping/{name}.{param_name}_clipped_count"] = clipped
                                    
                                except Exception as e:
                                    log.warning(f"Failed to compute clipping metrics for {name}.{param_name}: {e}")
        
        # Overall statistics
        if total_weights > 0:
            overall_clip_rate = total_clipped / total_weights
            metrics["layernorm_clipping/overall_clip_rate"] = overall_clip_rate
            metrics["layernorm_clipping/total_clipped"] = total_clipped
            metrics["layernorm_clipping/total_weights"] = total_weights
        
        return metrics

    def train_step(self, batch: Dict[str, Any], reduce_global_loss: bool = True) -> Dict[str, float]:
        metrics: Dict[str, float] = {}

        # Write data-indices to file.
        if self.indices_file is not None and "index" in batch:
            indices = "\t".join(str(int(i)) for i in batch["index"])
            self.indices_file.write(f"{self.global_step}\t{indices}\n")

        # Zero-gradients.
        self.optim.zero_grad(set_to_none=True)

        # Move tensors to the right device.
        batch = move_to_device(batch, self.device)

        # Run forward-backward pass.
        ce_batch_loss, z_batch_loss = self.train_batch(batch)

        # Collect loss, potentially reducing over all ranks.
        if reduce_global_loss:
            dist.reduce(ce_batch_loss, 0)
            ce_batch_loss.div_(get_world_size())
            if z_batch_loss is not None:
                dist.reduce(z_batch_loss, 0)
                z_batch_loss.div_(get_world_size())

        # Clip gradient norms and collect param/gradient/optim metrics.
        should_log_optim_metrics_this_step = self.should_log_optim_metrics_this_step()
        optim_metrics = self.optim_step(should_log_metrics=should_log_optim_metrics_this_step)

        if should_log_optim_metrics_this_step: # and get_global_rank() == 0:
            layernorm_metrics = self.collect_layernorm_clipping_metrics(self.fsdp_model)
            metrics.update(layernorm_metrics)

        # Collect metrics and check for NaN loss.
        # NOTE: this involves a bunch of host-device syncs so we wait until the last moment to do this.
        if torch.isnan(ce_batch_loss):
            raise ValueError("nan loss encountered")
        if z_batch_loss is not None and torch.isnan(z_batch_loss):
            raise ValueError("nan loss encountered")
        for key, value in optim_metrics.items():
            metrics[f"optim/{key}"] = value.item()
        self.cur_train_loss = ce_batch_loss.item()
        self.min_train_loss = min(self.min_train_loss, self.cur_train_loss)
        metrics["train/CrossEntropyLoss"] = self.cur_train_loss
        metrics["train/Perplexity"] = math.exp(self.cur_train_loss)
        if z_batch_loss is not None:
            metrics["train/ZLoss"] = z_batch_loss.item()

        # Maybe collect post-step optimizer-specific metrics.
        if should_log_optim_metrics_this_step:
            # 1) build per‐layer clipped & total lists
            n_layers = self.cfg.model.n_layers
            local_cl = [self._act_clip_stats.get(i, [0, 0])[0] for i in range(n_layers)]
            local_tot = [self._act_clip_stats.get(i, [0, 0])[1] for i in range(n_layers)]
            # 2) all‐reduce them across ranks
            device = self.device
            cl_tensor  = torch.tensor(local_cl, dtype=torch.long, device=device)
            tot_tensor = torch.tensor(local_tot, dtype=torch.long, device=device)
            dist.all_reduce(cl_tensor,  op=dist.ReduceOp.SUM, group=self.fsdp_model.process_group)
            dist.all_reduce(tot_tensor, op=dist.ReduceOp.SUM, group=self.fsdp_model.process_group)
            # 3) on rank0 convert to metrics
            if dist.get_rank() == 0:
                global_cl = cl_tensor.tolist()
                global_tot = tot_tensor.tolist()
                overall_cl = sum(global_cl)
                overall_tot = sum(global_tot)
                for i, (cl, tot) in enumerate(zip(global_cl, global_tot)):
                    if tot > 0:
                        metrics[f"act_clip/layer{i}"] = cl / tot
                if overall_tot > 0:
                    metrics["act_clip/overall"] = overall_cl / overall_tot
            # 4) clear for next step
            self._act_clip_stats.clear()

        return metrics

    def eval_batch(self, batch: Dict[str, Any]) -> Tuple[torch.Tensor, torch.Tensor]:
        with torch.autocast("cuda", enabled=True, dtype=self.cfg.autocast_precision):
            ce_loss, _, logits = self.model_forward(batch, loss_reduction="none")
        return ce_loss.mean(dim=-1), logits

    def eval_step(self, batch: Dict[str, Any], evaluator: Evaluator) -> None:
        # Move tensors to the right device.
        batch = move_to_device(batch, self.device)

        # Run forward pass.
        with torch.no_grad():  # NOTE: 'torch.inference_mode()' doesn't work with 'torch.compile()'.
            ce_loss, logits = self.eval_batch(batch)

        # Update metrics.
        evaluator.update_metrics(
            batch, ce_loss, logits
        )  # batch includes all keys that the downstream evaluation needs

        barrier()

    def split_batch(self, batch: Dict[str, Any]) -> List[Dict[str, Any]]:
        microbatch_size = self.cfg.device_train_microbatch_size
        batch_size = batch["input_ids"].shape[0]
        if batch_size <= microbatch_size:
            return [batch]
        else:
            micro_batches = {}
            for key, value in batch.items():
                if isinstance(value, torch.Tensor):
                    micro_batches[key] = value.split(microbatch_size, dim=0)
                elif isinstance(value, list):
                    micro_batches[key] = [
                        value[microbatch_size * i : microbatch_size * i + microbatch_size]
                        for i in range(math.ceil(batch_size / microbatch_size))
                    ]
                else:
                    raise ValueError(f"unexpected item in batch: '{key}={value}'")
            return [
                {key: value[i] for key, value in micro_batches.items()}  # type: ignore
                for i in range(len(micro_batches["input_ids"]))
            ]

    def system_metrics(self) -> Dict[str, float]:
        metrics = {}
        if self.global_step < 3 or self.global_step % 10 == 0:
            peak_gpu_mb = peak_gpu_memory()
            if peak_gpu_mb is not None:
                metrics["System/Peak GPU Memory (MB)"] = peak_gpu_mb
        return metrics

    def log_metrics_to_console(self, prefix: str, metrics: Dict[str, float]):
        def format_float(value: float) -> str:
            if value < 0.0001:
                return str(value)  # scientific notation
            elif value > 1000:
                return f"{int(value):,d}"
            elif value > 100:
                return f"{value:.1f}"
            elif value > 10:
                return f"{value:.2f}"
            elif value > 1:
                return f"{value:.3f}"
            else:
                return f"{value:.4f}"

        log.info(
            f"{prefix}\n"
            + "\n".join(
                [
                    f"    {name}={format_float(value)}"
                    for name, value in metrics.items()
                    if not name.startswith("optim/")  # there's too many optimizer metrics
                ]
            )
        )

    def should_log_optim_metrics_this_step(self) -> bool:
        if self.cfg.wandb is None:
            # We only log optimizer-specific metrics to W&B, since there are usually too many metrics
            # to log to the console.
            return False
        optim_log_interval = self.cfg.optimizer.metrics_log_interval
        if optim_log_interval is None:
            optim_log_interval = self.cfg.wandb.log_interval
        else:
            optim_log_interval = max(optim_log_interval, self.cfg.wandb.log_interval)
        return self.global_step % optim_log_interval == 0

    def should_log_this_step(self) -> bool:
        if self.global_step % self.cfg.console_log_interval == 0:
            return True
        elif self.cfg.wandb is not None and self.global_step % self.cfg.wandb.log_interval == 0:
            return True
        else:
            return False

    def eval(self) -> Dict[str, Any]:
        # Zero gradients and set model to 'eval' mode.
        self.optim.zero_grad(set_to_none=True)
        self.fsdp_model.eval()

        eval_metrics = {}
        for evaluator in self.evaluators:
            log.info(f"Running evaluation for '{evaluator.label}'...")

            # Reset metrics.
            evaluator.reset_metrics()

            # Initialize data loader iterator.
            eval_batches = iter(evaluator.eval_loader)

            # Adjust how many batches to evaluate on.
            num_eval_batches = (
                evaluator.subset_num_batches
                if evaluator.subset_num_batches is not None
                else self.cfg.eval_subset_num_batches
            )
            if num_eval_batches > 0:
                num_eval_batches = min(num_eval_batches, len(evaluator.eval_loader))
                eval_batches = islice(eval_batches, num_eval_batches)

            # Run model over batches.
            for eval_step, eval_batch in enumerate(eval_batches):
                self.eval_step(eval_batch, evaluator)

                # Log to console.
                if eval_step + 1 == num_eval_batches or (eval_step + 1) % self.cfg.console_log_interval == 0:
                    log.info(f"[eval_step={eval_step + 1}/{num_eval_batches}]")

            # Get final metrics.
            metrics = evaluator.compute_metrics()
            eval_metrics.update(metrics)
            self.log_metrics_to_console(f"{evaluator.label}", metrics)

            del eval_batches

        return eval_metrics

    def check_if_cancelled(self) -> Tuple[bool, int]:
        should_cancel = False
        cancel_reason: Optional[str] = None
        extra_steps = 0
        if get_global_rank() == 0:
            if self.cfg.time_limit is not None and time.time() - self._start_time >= self.cfg.time_limit:
                # First check if we've reached the training time limit.
                should_cancel = True
                cancel_reason = "time limit reached"
                extra_steps = self.cfg.extra_steps_after_cancel
            elif (
                self.cfg.early_stopping_factor is not None
                and self.global_step > self.cfg.scheduler.t_warmup
                and self.cur_train_loss > self.cfg.early_stopping_factor * self.min_train_loss
            ):
                # Next check if early stopping loss criteria is met.
                should_cancel = True
                cancel_reason = "early stopping from loss increase"
            elif wandb.run is not None and (api_key := os.environ.get("WANDB_API_KEY")) is not None:
                # Finally, check if someone canceled the run from W&B by adding the 'cancel' / 'canceled' tag..
                # We won't see it in the run object. So we have to use the import/export API to check.
                from requests.exceptions import RequestException
                from wandb.errors import CommError

                try:
                    api = wandb.Api(api_key=api_key)
                    run = api.run(wandb.run.path)
                    for tag in run.tags or []:
                        if tag.lower() in {"cancel", "canceled", "cancelled"}:
                            should_cancel = True
                            cancel_reason = "Weights & Biases tag"
                            extra_steps = self.cfg.extra_steps_after_cancel
                            break
                except (RequestException, CommError):
                    log.info("Failed to check if W&B run is cancelled, continuing run.")

        run_canceled = synchronize_flag(should_cancel, self.device)
        if run_canceled:
            extra_steps = synchronize_value(extra_steps, self.device)
            if cancel_reason is None:
                if extra_steps > 0:
                    log.warning(f"Run canceled, stopping in {extra_steps} more steps...")
                else:
                    log.warning("Run canceled")
            else:
                if extra_steps > 0:
                    log.warning(f"Run canceled due to {cancel_reason}, stopping in {extra_steps} more steps...")
                else:
                    log.warning(f"Run canceled due to {cancel_reason}")

        return run_canceled, extra_steps

    def fit(self):
        if self.cfg.stop_after is not None:
            if self.cfg.stop_at is None:
                self.cfg.stop_at = self.global_step + self.cfg.stop_after
            else:
                self.cfg.stop_at = min(self.cfg.stop_at, self.global_step + self.cfg.stop_after)

        self._start_time = time.time()
        self._gc_init_state = gc.isenabled()  # cache if garbage collection is enabled, reset on close.

        # Disable automatic garbage collection, FSDP doesn't work well with it.
        if self.cfg.gen1_gc_interval is not None:
            gc.disable()

        if self.cfg.load_path is not None and self.cfg.eval_on_load:  # and self.global_step > 0
            eval_metrics = self.eval()
            if wandb.run is not None:
                wandb.log(eval_metrics, step=self.global_step)

        # Set model to 'train' mode.
        self.fsdp_model.train()

        # Initialize monitors.
        assert self.cfg.device_train_batch_size is not None
        speed_monitor = SpeedMonitor(self.cfg.speed_monitor)
        lr_monitor = LRMonitor(self.optim)

        # Log system metrics at the start of training.
        sys_metrics = self.system_metrics()
        if sys_metrics:
            self.log_metrics_to_console("Pre-train system metrics", sys_metrics)
            if wandb.run is not None:
                wandb.log(sys_metrics, step=0)

        # Python Profiler stuff
        if self.cfg.python_profiling:
            python_profiler = cProfile.Profile()
        else:
            python_profiler = None

        # PyTorch Profiler stuff
        if self.cfg.torch_profiling and get_global_rank() == 0:
            from torch.profiler import schedule

            profiling_schedule = schedule(wait=1, warmup=5, active=3, repeat=1)

            def on_trace_ready(p):
                profiler_output_dir = Path(self.cfg.save_folder) / "profiler"
                profiler_output_dir.mkdir(exist_ok=True)

                output = p.key_averages().table(sort_by="self_cuda_time_total", row_limit=32)
                log.info(f"Profile by total GPU time at step {p.step_num}:\n{output}")
                output = p.key_averages().table(sort_by="self_cpu_time_total", row_limit=32)
                log.info(f"Profile by total CPU time at step {p.step_num}:\n{output}")

                p.export_chrome_trace(
                    str(trace_path := (profiler_output_dir / f"{p.step_num}.chrome_trace.json.gz"))
                )
                if self.cfg.remote_save_folder is not None:
                    upload_folder = f"{self.cfg.remote_save_folder.rstrip('/')}/profiler"
                    log.info(f"Tracing complete, uploading results to '{upload_folder}'...")
                    upload(trace_path, f"{upload_folder}/{trace_path.name}")

            from torch.profiler import ProfilerActivity

            torch_profiler = torch.profiler.profile(
                activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
                record_shapes=False,
                profile_memory=False,
                with_stack=True,
                schedule=profiling_schedule,
                on_trace_ready=on_trace_ready,
            )
            del profiling_schedule
        else:
            import contextlib

            torch_profiler = contextlib.nullcontext()

        # Train.
        first_batch: bool = True
        cancel_initiated: bool = False
        stop_at: Optional[int] = self.cfg.stop_at
        save_checkpoints: bool = True

        if stop_at is None and self.max_epochs == 1:
            stop_at = self.max_steps

        with torch_profiler as p:
            for epoch in range(self.epoch or 0, self.max_epochs):
                for batch in self.train_loader:
                    # Bookkeeping.
                    # NOTE: To track the global batch size / number of tokens per batch we make the assumption that all
                    # batches see the same number of tokens, which should be the case for language model pre-training
                    # (at least when drop_last=True).
                    # Alternatively we'd have to use a distributed all reduce over seq_len here, but I don't want that
                    # overhead. So for now I'm putting these assertions here so if the assumption is violated it will
                    # fail loudly.

                    batch_size, seq_len = batch["input_ids"].shape
                    assert (
                        seq_len == self.cfg.model.context_length
                    ), f"{seq_len} is not {self.cfg.model.context_length}"
                    assert (
                        batch_size == self.cfg.device_train_batch_size
                    ), f"{batch_size} is not {self.cfg.device_train_batch_size}"
                    global_batch_size = batch_size * get_world_size()  # assumes batch size equal across ranks
                    self.global_step += 1
                    self.global_train_examples_seen_this_epoch += global_batch_size
                    self.global_train_tokens_seen += global_batch_size * seq_len
                    speed_monitor.batch_start(
                        self.global_train_tokens_seen,
                        batch_size * seq_len,  # num tokens in batch for this device
                        # We start monitoring speed after the first batch since the first
                        # batch might be an outlier due to compiling and other initialization overhead.
                        record=not first_batch,
                    )

                    should_log_this_step = self.should_log_this_step()

                    # Run train step on batch.
                    metrics = self.train_step(batch, reduce_global_loss=should_log_this_step)

                    # Maybe collect other metrics.
                    if should_log_this_step:
                        # Speed metrics.
                        metrics.update(speed_monitor.check())
                        # System metrics.
                        metrics.update(self.system_metrics())
                        # Learning rate metrics.
                        metrics.update(lr_monitor.check())

                    # Log metrics to console.
                    if self.global_step % self.cfg.console_log_interval == 0:
                        if get_global_rank() == 0:
                            self.log_metrics_to_console(f"[step={self.global_step}/{self.max_steps}]", metrics)
                        else:
                            log.info(f"[step={self.global_step}/{self.max_steps}]")

                    # Log metrics to W&B.
                    if (
                        wandb.run is not None
                        and self.cfg.wandb is not None
                        and self.global_step % self.cfg.wandb.log_interval == 0
                    ):
                        wandb.log(metrics, step=self.global_step)

                    # Check if/when run should be canceled.
                    if not cancel_initiated and self.global_step % self.cfg.canceled_check_interval == 0:
                        cancel_initiated, extra_steps = self.check_if_cancelled()
                        if cancel_initiated:
                            stop_at = (
                                self.global_step + extra_steps
                                if stop_at is None
                                else min(self.global_step + extra_steps, stop_at)
                            )

                    # Maybe save sharded checkpoint.
                    if save_checkpoints and (
                        cancel_initiated
                        or (
                            self.global_step % self.cfg.save_interval == 0
                            and self.cfg.save_num_checkpoints_to_keep != 0
                        )
                    ):
                        log.info("Saving checkpoint...")
                        checkpoint_path, _ = self.save_checkpoint(CheckpointType.sharded)
                        log.info(f"Checkpoint saved to {checkpoint_path}")

                        # Remove any ephemeral checkpoints.
                        while self.ephemeral_checkpoints:
                            self.remove_ephemeral_checkpoint()

                        # Reset speed monitor so that we don't count the time taken to save checkpoints.
                        speed_monitor.reset()

                        # If the run was just canceled this will be the final checkpoint.
                        if cancel_initiated:
                            save_checkpoints = False
                    elif (
                        self.cfg.save_interval_ephemeral is not None
                        and self.global_step % self.cfg.save_interval_ephemeral == 0
                    ):
                        log.info("Saving ephemeral checkpoint...")
                        checkpoint_path, _ = self.save_checkpoint(CheckpointType.sharded_ephemeral)
                        log.info(f"Checkpoint saved to {checkpoint_path}")

                        # Reset speed monitor so that we don't count the time taken to save checkpoints.
                        speed_monitor.reset()

                    # Maybe save unsharded checkpoint.
                    if (
                        save_checkpoints
                        and self.cfg.save_interval_unsharded is not None
                        and self.global_step % self.cfg.save_interval_unsharded == 0
                        and self.cfg.save_num_unsharded_checkpoints_to_keep != 0
                    ):
                        log.info("Saving unsharded checkpoint...")
                        checkpoint_path, _ = self.save_checkpoint(CheckpointType.unsharded)
                        log.info(f"Unsharded checkpoint saved to {checkpoint_path}")

                        # Reset speed monitor so that we don't count the time taken to save checkpoints.
                        speed_monitor.reset()

                    # Maybe run evaluations.
                    if not cancel_initiated and self.global_step % self.cfg.eval_interval == 0:
                        eval_metrics = self.eval()

                        # Log metrics to W&B.
                        if wandb.run is not None:
                            wandb.log(eval_metrics, step=self.global_step)

                        # Reset speed monitor so that we don't count the time taken to run evaluations.
                        speed_monitor.reset()

                        # Reset model to 'train' mode.
                        self.fsdp_model.train()
                    
                    # if self.cfg.log_weight_clipping and self.global_step % self.cfg.weight_clipping_log_interval == 0 and get_global_rank() == 0:
                    #     log.info(f"Performing detailed weight/activation logging for global_step {self.global_step}...")
                    #     with torch.no_grad():
                    #         with FSDP.summon_full_params(self.fsdp_model, writeback=False, rank0_only=True):
                    #             model_to_inspect = self.fsdp_model.module # Get the underlying OLMo model

                    #             current_batch_input_ids = batch["input_ids"]
                    #             #attention_mask = batch.get("attention_mask")

                    #             x = model_to_inspect.transformer.wte(current_batch_input_ids)
                    #             if hasattr(model_to_inspect.transformer, "wpe") and model_to_inspect.transformer.wpe is not None:
                    #                 seq_len_dim = current_batch_input_ids.size(1) # Renamed to avoid conflict
                    #                 pos_ids = torch.arange(seq_len_dim, device=current_batch_input_ids.device).unsqueeze(0)
                    #                 x = x + model_to_inspect.transformer.wpe(pos_ids)
                    #             if hasattr(model_to_inspect.transformer, 'emb_drop') and model_to_inspect.transformer.emb_drop is not None:
                    #                 x = model_to_inspect.transformer.emb_drop(x)

                    #             export_dir = Path(self.cfg.save_folder) / "instability_tensors"
                    #             export_dir.mkdir(parents=True, exist_ok=True)

                    #             clipped_total_h_accum, elems_total_h_accum = 0, 0
                    #             clipped_total_layer_accum, elems_total_layer_accum = 0, 0
                    #             all_input_after_mlp_layernorm = []

                    #             current_block_input = x

                    #             # Corrected iteration over blocks:
                    #             all_transformer_blocks = []
                    #             if model_to_inspect.config.block_group_size == 1:
                    #                 all_transformer_blocks = list(model_to_inspect.transformer.blocks)
                    #             else:
                    #                 for group in model_to_inspect.transformer.block_groups:
                    #                     for block_in_group in group: # OLMoBlockGroup is an nn.ModuleList of OLMoBlock
                    #                         all_transformer_blocks.append(block_in_group)

                    #             for blk_idx, blk in enumerate(all_transformer_blocks):
                    #                 # blk_idx is the global 0-indexed layer ID.
                    #                 # blk.layer_id should also be this global ID.
                    #                 assert blk_idx == blk.layer_id, f"Mismatch in block index: enumerated {blk_idx} vs blk.layer_id {blk.layer_id}"

                    #                 _x_attn_part = current_block_input
                    #                 if blk.attn_norm is not None: # OLMoSequentialBlock has attn_norm
                    #                     normed_x_attn = blk.attn_norm(_x_attn_part)
                    #                 else:
                    #                     normed_x_attn = _x_attn_part
                                    
                    #                 # Assuming blk is OLMoSequentialBlock for att_proj
                    #                 # For OLMoLlamaBlock, q,k,v projections are separate (q_proj, k_proj, v_proj)
                    #                 if hasattr(blk, 'att_proj'): # OLMoSequentialBlock
                    #                     qkv = blk.att_proj(normed_x_attn)
                    #                     q, k, v = qkv.split(blk.fused_dims, dim=-1)
                    #                 elif hasattr(blk, 'q_proj'): # OLMoLlamaBlock
                    #                     q = blk.q_proj(normed_x_attn)
                    #                     k = blk.k_proj(normed_x_attn)
                    #                     v = blk.v_proj(normed_x_attn)
                    #                 else:
                    #                     raise NotImplementedError(f"Block type {type(blk)} not fully handled for QKV projection in logging.")

                    #                 attn_output, _, _ = blk.attention( # Call OLMoBlock's attention method
                    #                     q, k, v, # Pass q,k,v directly
                    #                     attention_bias=None, # Simplified for this forward pass, actual attention_bias is complex
                    #                     use_cache=False
                    #                 )
                    #                 mlp_part_input = _x_attn_part + attn_output
                    #                 save_instability_tensors(mlp_part_input, export_dir, self.global_step, blk_idx, "input_to_mlp", self.cfg.weight_clip_threshold_min, self.cfg.weight_clip_threshold_max)

                                    
                    #                 # OLMoSequentialBlock always has ff_norm (ff layernorm). We'll assume it's used.

                    #                 z_norm = blk.ff_norm(mlp_part_input) # Use blk.ff_norm for MLP LayerNorm

                    #                 save_instability_tensors(z_norm, export_dir, self.global_step, blk_idx, "postMLPNorm", self.cfg.weight_clip_threshold_min, self.cfg.weight_clip_threshold_max)
                    #                 all_input_after_mlp_layernorm.append(z_norm)

                    #                 # Log gamma/beta for blk.ff_norm (MLP norm)
                    #                 if hasattr(blk.ff_norm, 'weight') and blk.ff_norm.weight is not None:
                    #                     gamma = blk.ff_norm.weight.data.detach().cpu()
                    #                     c_gamma, t_gamma = count_clipped_values(gamma, 32, self.cfg.elem_format)
                    #                     wandb.log({f"clipW/mlp_ff_norm{blk_idx}_gamma_clipped_frac": (c_gamma / t_gamma) if t_gamma > 0 else 0.0}, step=self.global_step)
                    #                     wandb.log({f"act/mlp_ff_norm{blk_idx}_gamma_mean": gamma.mean().item(), f"act/mlp_ff_norm{blk_idx}_gamma_var":  gamma.var(unbiased=False).item()}, step=self.global_step)
                    #                     save_instability_tensors(gamma, export_dir, self.global_step, blk_idx, "gamma_ff_norm", self.cfg.weight_clip_threshold_min, self.cfg.weight_clip_threshold_max)
                    #                 if hasattr(blk.ff_norm, 'bias') and blk.ff_norm.bias is not None:
                    #                     beta = blk.ff_norm.bias.data.detach().cpu()
                    #                     c_beta, t_beta = count_clipped_values(beta, 32, self.cfg.elem_format)
                    #                     wandb.log({f"clipW/mlp_ff_norm{blk_idx}_beta_clipped_frac": (c_beta / t_beta) if t_beta > 0 else 0.0}, step=self.global_step)
                    #                     wandb.log({f"act/mlp_ff_norm{blk_idx}_beta_mean":  beta.mean().item(), f"act/mlp_ff_norm{blk_idx}_beta_var":   beta.var(unbiased=False).item()}, step=self.global_step)
                    #                     save_instability_tensors(beta, export_dir, self.global_step, blk_idx, "beta_ff_norm", self.cfg.weight_clip_threshold_min, self.cfg.weight_clip_threshold_max)

                    #                 a1 = blk.ff_proj(z_norm)  # Use blk.ff_proj for MLP up-projection
                    #                 h_act = blk.act(a1)      # Use blk.act for MLP activation (renamed to h_act)
                    #                 save_instability_tensors(h_act, export_dir, self.global_step, blk_idx, "hidden_mlp_act", self.cfg.weight_clip_threshold_min, self.cfg.weight_clip_threshold_max)

                    #                 c_h, t_h = count_clipped_values(h_act, 32, self.cfg.elem_format)
                    #                 wandb.log({f"ClipAct/blk{blk_idx}_hidden_mlp_act_clipped_frac": (c_h / t_h) if t_h > 0 else 0.0}, step=self.global_step)
                    #                 clipped_total_h_accum += c_h;  elems_total_h_accum += t_h
                    #                 wandb.log({f"act/mean_hid_mlp_act{blk_idx}": h_act.mean().item(),
                    #                         f"act/var_hid_mlp_act{blk_idx}":  h_act.var(unbiased=False).item()}, step=self.global_step)

                    #                 #W2 = blk.ff_out.weight # Use blk.ff_out.weight for MLP down-projection weights
                    #                 # Reshape h_act for matmul if it's 3D (B, S, D) -> (B*S, D)
                    #                 # W2 is (d_model, d_ff), h_act.T should be (d_ff, B*S)
                    #                 # h_act_for_matmul = h_act.view(-1, h_act.size(-1)) if h_act.ndim == 3 else h_act
                    #                 # proj = torch.matmul(W2, h_act_for_matmul.T).abs() # h_act_for_matmul.T is (d_ff, B*S)
                    #                 # numer = proj.mean(dim=1) # (d_model,)
                                    
                    #                 # w_norm = (W2 ** 2).sum(dim=1, keepdim=True).sqrt()      # (d_model, 1)
                    #                 # h_act_norm_val = (h_act_for_matmul  ** 2).sum(dim=1, keepdim=True).sqrt().T    # (1, B*S)
                    #                 # denom  = (w_norm * h_act_norm_val.mean(dim=1, keepdim=True)) # Broadcasting (d_model,1) * (1,1) -> (d_model,1)
                    #                 # denom = denom.squeeze() + 1e-12 # (d_model,)
                    #                 # align  = (numer / denom).mean()   # scalar
                    #                 # wandb.log({f"align/blk{blk_idx}_mlp": align.item()}, step=self.global_step)

                    #                 output_of_mlp_fc2 = blk.ff_out(h_act) # Use blk.ff_out for MLP down-projection
                                    
                    #                 mlp_part_output_with_residual = mlp_part_input + output_of_mlp_fc2

                    #                 c_z, t_z = count_clipped_values(mlp_part_output_with_residual, 32, self.cfg.elem_format)
                    #                 wandb.log({f"ClipAct/blk{blk_idx}_mlp_output_res_clipped_frac": (c_z / t_z) if t_z > 0 else 0.0}, step=self.global_step)
                    #                 clipped_total_layer_accum += c_z;  elems_total_layer_accum += t_z
                                    
                    #                 current_block_input = mlp_part_output_with_residual
                    #             # End of loop over blocks

                    #         if elems_total_h_accum > 0:
                    #             wandb.log({"ClipAct/total_frac_hidden_mlp_act_all_layers": clipped_total_h_accum / elems_total_h_accum}, step=self.global_step)
                    #         if elems_total_layer_accum > 0:
                    #             wandb.log({"ClipAct/total_frac_mlp_output_res_all_layers": clipped_total_layer_accum / elems_total_layer_accum}, step=self.global_step)
                                
                    #         for k_idx, act_val in enumerate(all_input_after_mlp_layernorm):
                    #             wandb.log({f"act/mean_layer{k_idx}_input_after_mlp_ln": act_val.mean().item(),
                    #                     f"act/var_layer{k_idx}_input_after_mlp_ln":  act_val.var(unbiased=False).item()}, step=self.global_step)
                    #     log.info(f"Detailed weight/activation logging for global_step {self.global_step} complete.")

                    # End of batch.
                    first_batch = False
                    if p is not None:
                        p.step()

                    if stop_at is not None and self.global_step >= stop_at:
                        break

                    # Run generation 1 garbage collection.
                    if self.cfg.gen1_gc_interval is not None and self.global_step % self.cfg.gen1_gc_interval == 0:
                        gc.collect(1)

                    # Python Profiler stuff
                    # We do this now, at the bottom of this loop, so we capture the work of getting the next batch.
                    if python_profiler is not None:
                        if self.global_step == 5:
                            python_profiler.enable()
                        elif self.global_step == 8:
                            python_profiler.disable()
                            python_profiler.print_stats(sort=SortKey.CUMULATIVE)
                            python_profiler = None
                else:
                    log.info("Training epoch complete")
                    self.epoch = epoch + 1
                    self.global_train_examples_seen_this_epoch = 0
                    if self.epoch < self.max_epochs:
                        self.dataset.reshuffle()
                    continue

                break

        # Save final checkpoint.
        if save_checkpoints:
            if (
                self.cfg.save_interval_unsharded is not None
                and self.last_unsharded_checkpoint_step != self.global_step
                and self.cfg.save_num_unsharded_checkpoints_to_keep != 0
            ):
                log.info("Saving final unsharded model checkpoint...")
                checkpoint_path, _ = self.save_checkpoint(CheckpointType.unsharded)
                log.info(f"Unsharded checkpoint saved to {checkpoint_path}")
            elif (
                self.cfg.save_num_checkpoints_to_keep != 0
                and self.last_sharded_checkpoint_step != self.global_step
            ):
                log.info("Saving final checkpoint...")
                checkpoint_path, _ = self.save_checkpoint(CheckpointType.sharded)
                log.info(f"Checkpoint saved to {checkpoint_path}")

    def save_sequences(self, batch: Dict[str, Any]) -> None:
        # below are for reading out tokens and save them into json file
        from .tokenizer import Tokenizer
        import json

        tokenizer = Tokenizer.from_train_config(self.cfg)
        sequences = tokenizer.batch_decode(batch["input_ids"].tolist())

        # log.info(self.cfg.data.paths)
        file_path = str(self.cfg.data.paths[0]).replace("npy", "jsonl")
        with open(file_path, "a") as file:
            for s in sequences:
                json_line = json.dumps({"text": s})
                file.write(json_line + "\n")
        log.info(f"Writing {len(sequences)} sequences to {file_path} jsonl file")

    def close(self, exit_code: int = 0) -> None:
        gc_cuda()

        if self.indices_file is not None:
            self.indices_file.flush()
            self.indices_file.close()
        if self._gc_init_state:
            gc.enable()
        else:
            gc.disable()
        if wandb.run is not None:
            wandb.finish(exit_code=exit_code, quiet=True)

    def __enter__(self) -> Trainer:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        del exc_val, exc_tb
        self.close(0 if exc_type is None else 1)
