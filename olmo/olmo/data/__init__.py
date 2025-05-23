from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from torch.utils.data import DataLoader, DistributedSampler

from ..aliases import PathOrStr
from ..config import DataConfig, TrainConfig
from ..exceptions import OLMoConfigurationError
from ..torch_util import barrier, get_global_rank, get_world_size
from .collator import DataCollator
from .iterable_dataset import IterableDataset, IterableDatasetFixedIndex, IterableDatasetTrainVal, MixtureDataset
from .memmap_dataset import MemMapDataset
from .dict_memmap_dataset import DictMemmapDataset, DictMemmapWriter
from .datatrove_dataset import DatatroveFolderDataset

from ..tokenizer import Tokenizer
from ..eval.downstream import label_to_task_map
from ..config import EvaluatorConfig, TrainConfig
from ..eval.downstream import ICLMultiChoiceTaskDataset

from olmo.registry import DATA_DICT

from transformers import AutoTokenizer

import torch
from copy import deepcopy
import re
import random

__all__ = [
    "MemMapDataset",
    "DataCollator",
    "IterableDataset",
    "build_eval_dataloader",
    "build_train_dataloader",
    "DictMemmapDataset",
    "DictMemmapWriter",
    "build_train_dataloader_plus",
]


def get_path(path):
    if path in DATA_DICT:
        path = DATA_DICT[path]
    elif path.replace("_", "-") in DATA_DICT:
        path = DATA_DICT[path.replace("_", "-")]
    return Path(path)


def build_memmap_dataset(
    train_config: TrainConfig, data_config: DataConfig, include_instance_metadata: bool = True, mode: str = 'train'
) -> MemMapDataset:
    paths: List[str]
    metadata: List[Dict[str, Any]] = []
    if data_config.paths:
        if data_config.datasets:
            raise OLMoConfigurationError("DataConfig.paths is mutually exclusive with DataConfig.datasets")
        paths = data_config.paths
        if isinstance(paths, str):
            path = get_path(paths)
            paths = list(path.glob("*.bin")) + list(path.glob("*.npy")) + list(path.glob("*.ds"))
            if len(paths) == 0:
                paths = list(path.glob("*/*.npy")) + list(path.glob("*/*.ds"))
            if len(paths) == 0:
                paths = list(path.glob("*/*/*.npy")) + list(path.glob("*/*/*.ds"))
            if len(paths) == 0:
                raise OLMoConfigurationError(f"no data found at {path}")
        
        # Custom logic
        random.seed(42)  # Set seed for reproducibility

        bin_files = [(p, int(re.search(r'(\d+)\.bin$', p.name).group(1))) for p in paths]
        random.shuffle(bin_files)
        split_idx = int(len(bin_files) * 0.8)
        train_files = bin_files[:split_idx]
        val_files = bin_files[split_idx:]

        if mode == 'val':
            paths = [p[0] for p in val_files]
        else:
            paths = [p[0] for p in train_files]
        
        for path in paths:
            metadata.append({"path": str(path)})
    elif data_config.datasets:
        paths = []
        for label in sorted(data_config.datasets.keys()):
            label_paths = data_config.datasets[label]
            if isinstance(label_paths, str):
                path = get_path(label_paths)
                print(f"Loading {label} from {path}")
                label_paths = list(path.glob("*.bin")) + list(Path(path).glob("*.npy")) + list(Path(path).glob("*.ds"))
                if len(label_paths) == 0:
                    label_paths = list(Path(path).glob("*/*.npy")) + list(Path(path).glob("*/*.ds"))
                if len(label_paths) == 0:
                    label_paths = list(Path(path).glob("*/*/*.npy")) + list(Path(path).glob("*/*/*.ds"))
                if len(label_paths) == 0:
                    raise OLMoConfigurationError(f"no data found at {path}")
            
            # Custom logic for RedPajama
            # if mode == 'val':
            #     # label_paths = [p for p in label_paths if p.name.endswith(('_27.bin', '_28.bin', '_29.bin', '_30.bin', '_31.bin', '_32.bin', '_33.bin'))]
            #     label_paths = [p for p in label_paths if int(re.search(r'(\d+)\.bin$', p.name).group(1)) > 371]
            # else:
            #     # label_paths = [p for p in label_paths if not (p.name.endswith(('_27.bin', '_28.bin', '_29.bin', '_30.bin', '_31.bin', '_32.bin', '_33.bin')))]
            #     label_paths = [p for p in label_paths if int(re.search(r'(\d+)\.bin$', p.name).group(1)) < 371]

            random.seed(42)  # Set seed for reproducibility

            bin_files = [(p, int(re.search(r'(\d+)\.bin$', p.name).group(1))) for p in label_paths]
            random.shuffle(bin_files)
            split_idx = int(len(bin_files) * 0.8)
            train_files = bin_files[:split_idx]
            val_files = bin_files[split_idx:]

            if mode == 'val':
                label_paths = [p[0] for p in val_files]
            else:
                label_paths = [p[0] for p in train_files]

            paths.extend(label_paths)
            metadata.extend([{"label": label}] * len(label_paths))
    else:
        raise OLMoConfigurationError("One of DataConfig.paths or DataConfig.datasets is required")

    # NOTE: .ds files are actually readable as npy files
    # TODO: should we ever be using the datatrove dataset instead?
    # if train_config.datatrove_dataset:
    #     return DatatroveFolderDataset(
    #         *paths,
    #         seq_len=train_config.model.max_sequence_length,
    #         memmap_dtype=data_config.effective_memmap_dtype,
    #     )
    # else:
    return MemMapDataset(
        *paths,
        chunk_size=train_config.model.context_length,
        memmap_dtype=data_config.effective_memmap_dtype,
        metadata=metadata,
        include_instance_metadata=include_instance_metadata,
        pad_token_id=train_config.model.pad_token_id,
        generate_attention_mask=data_config.generate_attention_mask,
        label_mask_paths=cast(Optional[List[PathOrStr]], data_config.label_mask_paths),
    )


def build_eval_dataloader(
    train_config: TrainConfig,
    data_config: DataConfig,
    batch_size: int,
    shuffle: bool = True,
) -> DataLoader:
    dataset = build_memmap_dataset(train_config, data_config, include_instance_metadata=True, mode='val')
    collator = DataCollator(pad_direction=data_config.pad_direction, pad_token_id=train_config.model.pad_token_id)
    if data_config.drop_last:
        # Make sure batch size is small enough.
        samples_per_device = len(dataset) // get_world_size()
        batch_size = min(batch_size, samples_per_device)
        assert batch_size > 0, f"dataset for {data_config.paths} is too small"
    seed = data_config.seed if data_config.seed is not None else train_config.seed
    sampler = DistributedSampler(
        dataset,
        drop_last=data_config.drop_last,
        shuffle=shuffle,
        num_replicas=get_world_size(),
        rank=get_global_rank(),
        seed=seed,
    )
    return DataLoader(
        dataset,
        batch_size=batch_size,
        collate_fn=collator,
        num_workers=data_config.num_workers,
        sampler=sampler,
        pin_memory=data_config.pin_memory,
        prefetch_factor=None if data_config.num_workers == 0 else data_config.prefetch_factor,
        persistent_workers=False if data_config.num_workers == 0 else data_config.persistent_workers,
        timeout=data_config.timeout,
    )


def load_tokenizer(name):
    if name == "fineweb":
        tokenizer = AutoTokenizer.from_pretrained("HuggingFaceTB/fineweb-edu-classifier")
    elif name == "color-filter":
        tokenizer = AutoTokenizer.from_pretrained("allenai/eleuther-ai-gpt-neox-20b-pii-special")
    else:
        tokenizer = AutoTokenizer.from_pretrained(name)

    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
        tokenizer.pad_token_id = tokenizer.eos_token_id

    return tokenizer


def build_train_iterable_dataset(
    train_config: TrainConfig, data_config: DataConfig, world_size: Optional[int] = None, index: int = 0
) -> IterableDataset:
    assert train_config.device_train_batch_size is not None
    dataset = build_memmap_dataset(train_config, data_config, include_instance_metadata=False)
    work_dir = Path(train_config.save_folder) / f"train_data_{index}"
    if get_global_rank() == 0:
        if work_dir.is_dir() and not train_config.save_overwrite:
            raise OLMoConfigurationError(
                "train data working directory already exists, use --save_overwrite to overwrite"
            )
        else:
            work_dir.mkdir(exist_ok=True, parents=True)
    barrier()
    seed = data_config.seed if data_config.seed is not None else train_config.seed
    return IterableDataset(
        dataset,  # type: ignore
        train_config.training.batch_size,
        seed=seed + (train_config.epoch or 0),
        shuffle=True,
        drop_last=data_config.drop_last,
        world_size=world_size,
        work_dir=work_dir,
    )


def build_train_dataloader(train_config: TrainConfig, world_size: Optional[int] = None) -> DataLoader:
    if train_config.datasets.weighted_paths is None:
        dataset = build_train_iterable_dataset(train_config, train_config.datasets, world_size)
    else:
        paths = list(train_config.datasets.weighted_paths.keys())
        weights = list(train_config.datasets.weighted_paths.values())
        print(f"Using mixture dataset with weights {weights} for paths {paths}")
        assert len(paths) == len(weights)
        assert sum(weights) == 1.0
        datasets = []
        for i, path in enumerate(paths):
            data_config = deepcopy(train_config.data)
            data_config.paths = path
            datasets.append(build_train_iterable_dataset(train_config, data_config, world_size, index=i))
        dataset = MixtureDataset(datasets, weights)

    if train_config.score_hf:
        if "new" in train_config.load_path:
            collator = DataCollator(
                pad_direction=train_config.datasets.pad_direction, pad_token_id=train_config.model.pad_token_id
            )
        else:
            old_tokenizer = load_tokenizer(train_config.tokenizer.identifier)
            new_tokenizer = load_tokenizer(train_config.load_path)
            collator = DataCollator(
                pad_direction=train_config.datasets.pad_direction,
                pad_token_id=train_config.model.pad_token_id,
                old_tokenizer=old_tokenizer,
                new_tokenizer=new_tokenizer,
            )
    else:
        collator = DataCollator(
            pad_direction=train_config.datasets.pad_direction, pad_token_id=train_config.model.pad_token_id
        )

    
    print(train_config.device_train_batch_size, train_config.datasets.drop_last, 'num_workers', train_config.datasets.num_workers,
          train_config.datasets.pin_memory, train_config.datasets.num_workers, train_config.datasets.prefetch_factor)

    dataloader = DataLoader(
        dataset,
        batch_size=train_config.device_train_batch_size,
        drop_last=train_config.datasets.drop_last,
        collate_fn=collator,
        num_workers=train_config.datasets.num_workers,
        pin_memory=train_config.datasets.pin_memory,
        prefetch_factor=None if train_config.datasets.num_workers == 0 else train_config.datasets.prefetch_factor,
        persistent_workers=False if train_config.datasets.num_workers == 0 else train_config.datasets.persistent_workers,
        timeout=train_config.datasets.timeout,
    )
    return dataloader


def build_train_dataloader_fixed_index(train_config: TrainConfig) -> DataLoader:
    assert train_config.device_train_batch_size is not None
    collator = DataCollator(
        pad_direction=train_config.datasets.pad_direction, pad_token_id=train_config.model.pad_token_id
    )
    dataset = build_memmap_dataset(train_config, train_config.datasets, include_instance_metadata=False)
    barrier()
    return DataLoader(
        IterableDatasetFixedIndex(
            dataset,  # type: ignore
            train_config.training.batch_size,
            input_index_path=train_config.datasets.index_path,
            seed=train_config.seed + (train_config.epoch or 0),
            shuffle=True,
            drop_last=train_config.datasets.drop_last,
        ),
        batch_size=train_config.device_train_batch_size,
        drop_last=train_config.datasets.drop_last,
        collate_fn=collator,
        num_workers=train_config.datasets.num_workers,
        pin_memory=train_config.datasets.pin_memory,
        prefetch_factor=None if train_config.datasets.num_workers == 0 else train_config.datasets.prefetch_factor,
        persistent_workers=False if train_config.datasets.num_workers == 0 else train_config.datasets.persistent_workers,
        timeout=train_config.datasets.timeout,
    )


def build_sft_dataloader(
    train_config: TrainConfig,
    eval_configs: EvaluatorConfig,
) -> DataLoader:
    tokenizer = Tokenizer.from_train_config(train_config)
    datasets = []
    for eval_config in eval_configs:
        task_kwargs = {}
        task_class = label_to_task_map[eval_config.label]
        if isinstance(task_class, tuple):
            task_class, task_kwargs = task_class
        task_kwargs["sft_use_label"] = eval_config.sft_use_label
        task_kwargs["sft"] = eval_config.sft
        task_kwargs["model_ctx_len"] = train_config.model.context_length
        dataset = task_class(tokenizer=tokenizer, **task_kwargs)
        datasets.append(dataset)
        assert isinstance(dataset, ICLMultiChoiceTaskDataset)  # NOTE collate only implemented for ICL
    collate_fn = datasets[0].collate_fn

    sft_dataset = torch.utils.data.ConcatDataset(datasets)
    work_dir = Path(train_config.save_folder) / "train_data"
    if get_global_rank() == 0:
        if work_dir.is_dir() and not train_config.save_overwrite:
            raise OLMoConfigurationError(
                "train data working directory already exists, use --save_overwrite to overwrite"
            )
        else:
            work_dir.mkdir(exist_ok=True, parents=True)
    barrier()
    return DataLoader(
        IterableDataset(
            sft_dataset,  # type: ignore
            train_config.training.batch_size,
            seed=train_config.seed + (train_config.epoch or 0),
            shuffle=True,
            drop_last=train_config.datasets.drop_last,
            work_dir=work_dir,
        ),
        batch_size=train_config.device_train_batch_size,
        drop_last=train_config.datasets.drop_last,
        collate_fn=collate_fn,
        num_workers=train_config.datasets.num_workers,
        pin_memory=train_config.datasets.pin_memory,
        prefetch_factor=None if train_config.datasets.num_workers == 0 else train_config.datasets.prefetch_factor,
        persistent_workers=False if train_config.datasets.num_workers == 0 else train_config.datasets.persistent_workers,
        timeout=train_config.datasets.timeout,
    )


def build_train_dataloader_val(train_config: TrainConfig, val=False) -> DataLoader:
    assert train_config.device_train_batch_size is not None
    collator = DataCollator(
        pad_direction=train_config.datasets.pad_direction, pad_token_id=train_config.model.pad_token_id
    )
    dataset = build_memmap_dataset(train_config, train_config.datasets, include_instance_metadata=False)
    barrier()
    return DataLoader(
        IterableDatasetTrainVal(
            dataset,  # type: ignore
            train_config.training.batch_size,
            input_index_path=train_config.datasets.index_path,
            seed=train_config.seed + (train_config.epoch or 0),
            shuffle=True,
            drop_last=train_config.datasets.drop_last,
            val_percentage=0.01,
            val=val,
        ),
        batch_size=train_config.device_train_batch_size,
        drop_last=train_config.datasets.drop_last,
        collate_fn=collator,
        num_workers=train_config.datasets.num_workers,
        pin_memory=train_config.datasets.pin_memory,
        prefetch_factor=None if train_config.datasets.num_workers == 0 else train_config.datasets.prefetch_factor,
        persistent_workers=False if train_config.datasets.num_workers == 0 else train_config.datasets.persistent_workers,
        timeout=train_config.datasets.timeout,
    )
