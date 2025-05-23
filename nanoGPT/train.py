"""
This training script can be run both on a single gpu in debug mode,
and also in a larger training run with distributed data parallel (ddp).

To run on a single GPU, example:
$ python train.py --batch_size=32 --compile=False

To run with DDP on 4 gpus on 1 node, example:
$ torchrun --standalone --nproc_per_node=4 train.py

To run with DDP on 4 gpus across 2 nodes, example:
- Run on the first (master) node with example IP 123.456.123.456:
$ torchrun --nproc_per_node=8 --nnodes=2 --node_rank=0 --master_addr=123.456.123.456 --master_port=1234 train.py
- Run on the worker node:
$ torchrun --nproc_per_node=8 --nnodes=2 --node_rank=1 --master_addr=123.456.123.456 --master_port=1234 train.py
(If your cluster does not have Infiniband interconnect prepend NCCL_IB_DISABLE=1)
"""

import os
import time
import math
import pickle
from contextlib import nullcontext

import numpy as np
import torch
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group
import hydra

from model import GPTConfig, GPT

from mx import finalize_mx_specs, mx_mapping

from tmrc.tmrc_core.training import data, train

# -----------------------------------------------------------------------------
# default config values designed to train a gpt2 (124M) on OpenWebText
# I/O
eval_interval = 500
log_interval = 10
eval_iters = 200
eval_only = False # if True, script exits right after the first eval
always_save_checkpoint = True # if True, always save a checkpoint after each eval
init_from = 'scratch' # 'scratch' or 'resume' or 'gpt2*'


# data
gradient_accumulation_steps = 5 * 8 # used to simulate larger batch sizes
block_size = 1024

# model
## normal
n_layer = 12
n_head = 12
n_embd = 768
## large
# n_layer = 36
# n_head = 20
# n_embd = 1280

dropout = 0.0 # for pretraining 0 is good, for finetuning try 0.1+
bias = False

# adamw optimizer
beta1 = 0.9
beta2 = 0.95
grad_clip = 1.0 # clip gradients at this value, or disable if == 0.0

# learning rate decay settings
decay_lr = True # whether to decay the learning rate
warmup_iters = 2000 # how many steps to warm up for

# DDP settings
backend = 'nccl' # 'nccl', 'gloo', etc.

# system
device = 'cuda' # examples: 'cpu', 'cuda', 'cuda:0', 'cuda:1' etc., or try 'mps' on macbooks
dtype = 'bfloat16' if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else 'float16' # 'float32', 'bfloat16', or 'float16', the latter will auto implement a GradScaler
compile = False # use PyTorch 2.0 to compile the model to be faster
# -----------------------------------------------------------------------------
config_keys = [k for k,v in globals().items() if not k.startswith('_') and isinstance(v, (int, float, bool, str))]
exec(open('configurator.py').read()) # overrides from command line or config file
config = {k: globals()[k] for k in config_keys} # will be useful for logging
# -----------------------------------------------------------------------------

# various inits, derived attributes, I/O setup
ddp = int(os.environ.get('RANK', -1)) != -1 # is this a ddp run?
if ddp:
    init_process_group(backend=backend)
    ddp_rank = int(os.environ['RANK'])
    ddp_local_rank = int(os.environ['LOCAL_RANK'])
    ddp_world_size = int(os.environ['WORLD_SIZE'])
    device = f'cuda:{ddp_local_rank}'
    torch.cuda.set_device(device)
    master_process = ddp_rank == 0 # this process will do logging, checkpointing etc.
    seed_offset = ddp_rank # each process gets a different seed
    # world_size number of processes will be training simultaneously, so we can scale
    # down the desired gradient accumulation iterations per process proportionally
    assert gradient_accumulation_steps % ddp_world_size == 0
    gradient_accumulation_steps //= ddp_world_size
else:
    # if not ddp, we are running on a single gpu, and one process
    master_process = True
    seed_offset = 0
    ddp_world_size = 1

config_path, config_name = train.get_config_path()
hydra.initialize(version_base=None, config_path=config_path)
dataset_config = hydra.compose(config_name=config_name)

train_loader, val_loader = data.create_dataloaders(dataset_config)
print(f"There are {len(train_loader)} batches in the training set")
train_iterator, val_iterator = iter(train_loader), iter(val_loader)

tokens_per_iter = gradient_accumulation_steps * ddp_world_size * dataset_config.training.batch_size * block_size
print(f"tokens per iteration will be: {tokens_per_iter:,}")

out_dir = 'out_' + dataset_config.model.w_mx_format + '_' + dataset_config.model.a_mx_format
print('w_mx_format', dataset_config.model.w_mx_format)
print('a_mx_format', dataset_config.model.a_mx_format)
if master_process:
    os.makedirs(out_dir, exist_ok=True)
torch.manual_seed(1337 + seed_offset)
torch.backends.cuda.matmul.allow_tf32 = True # allow tf32 on matmul
torch.backends.cudnn.allow_tf32 = True # allow tf32 on cudnn
device_type = 'cuda' if 'cuda' in device else 'cpu' # for later use in torch.autocast
# note: float16 data type will automatically use a GradScaler
ptdtype = {'float32': torch.float32, 'bfloat16': torch.bfloat16, 'float16': torch.float16}[dtype]
ctx = nullcontext() if device_type == 'cpu' else torch.amp.autocast(device_type=device_type, dtype=ptdtype)


# data_dir = os.path.join('data', dataset)
# def get_batch(split):
#     # We recreate np.memmap every batch to avoid a memory leak, as per
#     # https://stackoverflow.com/questions/45132940/numpy-memmap-memory-usage-want-to-iterate-once/61472122#61472122
#     if split == 'train':
#         data = np.memmap(os.path.join(data_dir, 'train.bin'), dtype=np.uint16, mode='r')
#     else:
#         data = np.memmap(os.path.join(data_dir, 'val.bin'), dtype=np.uint16, mode='r')
#     ix = torch.randint(len(data) - block_size, (batch_size,))
#     x = torch.stack([torch.from_numpy((data[i:i+block_size]).astype(np.int64)) for i in ix])
#     y = torch.stack([torch.from_numpy((data[i+1:i+1+block_size]).astype(np.int64)) for i in ix])
#     if device_type == 'cuda':
#         # pin arrays x,y, which allows us to move them to GPU asynchronously (non_blocking=True)
#         x, y = x.pin_memory().to(device, non_blocking=True), y.pin_memory().to(device, non_blocking=True)
#     else:
#         x, y = x.to(device), y.to(device)
#     return x, y


# init these up here, can override if init_from='resume' (i.e. from a checkpoint)
iter_num = 0
best_val_loss = 1e9

# attempt to derive vocab_size from the dataset
# meta_path = os.path.join(data_dir, 'meta.pkl')
meta_vocab_size = None
# if os.path.exists(meta_path):
#     with open(meta_path, 'rb') as f:
#         meta = pickle.load(f)
#     meta_vocab_size = meta['vocab_size']
#     print(f"found vocab_size = {meta_vocab_size} (inside {meta_path})")

# model init
model_args = dict(n_layer=n_layer, n_head=n_head, n_embd=n_embd, block_size=dataset_config.model.context_length,
                  bias=bias, vocab_size=None, dropout=dropout) # start with model_args from command line

# MXFP8_e5m2 matmuls with bfloat16 vector ops, forward pass only
mx_specs = {
        'scale_bits': 8,
        'w_elem_format': dataset_config.model.w_mx_format,
        'a_elem_format': dataset_config.model.a_mx_format,
        'block_size': 32,
        'bfloat': 16,
        'custom_cuda': True,
        # For quantization-aware finetuning, do backward pass in FP32
        'quantize_backprop': True,
        
    }
mx_specs = finalize_mx_specs(mx_specs)
mx_mapping.inject_pyt_ops(mx_specs)

if init_from == 'scratch':
    # init a new model from scratch
    print("Initializing a new model from scratch")
    # determine the vocab size we'll use for from-scratch training
    if meta_vocab_size is None:
        print("defaulting to vocab_size of GPT-2 to 50304 (50257 rounded up for efficiency)")

    model_args['vocab_size'] = meta_vocab_size if meta_vocab_size is not None else 50304
    gptconf = GPTConfig(**model_args)
    # model = GPT(gptconf, mx_specs)
    model = GPT(gptconf)

elif init_from == 'resume':
    print(f"Resuming training from {out_dir}")
    # resume training from a checkpoint.
    ckpt_path = os.path.join(out_dir, 'ckpt.pt')
    checkpoint = torch.load(ckpt_path, map_location=device)
    checkpoint_model_args = checkpoint['model_args']
    # force these config attributes to be equal otherwise we can't even resume training
    # the rest of the attributes (e.g. dropout) can stay as desired from command line
    for k in ['n_layer', 'n_head', 'n_embd', 'block_size', 'bias', 'vocab_size']:
        model_args[k] = checkpoint_model_args[k]
    # create the model
    gptconf = GPTConfig(**model_args)
    model = GPT(gptconf)
    state_dict = checkpoint['model']
    # fix the keys of the state dictionary :(
    # honestly no idea how checkpoints sometimes get this prefix, have to debug more
    unwanted_prefix = '_orig_mod.'
    for k,v in list(state_dict.items()):
        if k.startswith(unwanted_prefix):
            state_dict[k[len(unwanted_prefix):]] = state_dict.pop(k)
    model.load_state_dict(state_dict)
    iter_num = checkpoint['iter_num']
    best_val_loss = checkpoint['best_val_loss']
elif init_from.startswith('gpt2'):
    print(f"Initializing from OpenAI GPT-2 weights: {init_from}")
    # initialize from OpenAI GPT-2 weights
    override_args = dict(dropout=dropout)
    model = GPT.from_pretrained(init_from, override_args)
    # read off the created config params, so we can store them into checkpoint correctly
    for k in ['n_layer', 'n_head', 'n_embd', 'block_size', 'bias', 'vocab_size']:
        model_args[k] = getattr(model.config, k)
# crop down the model block size if desired, using model surgery
if block_size < model.config.block_size:
    model.crop_block_size(block_size)
    model_args['block_size'] = block_size # so that the checkpoint will have the right value
model.to(device)

# initialize a GradScaler. If enabled=False scaler is a no-op
# scaler = torch.cuda.amp.GradScaler(enabled=(dtype == 'float16'))

# optimizer
optimizer = model.configure_optimizers(dataset_config.optimizer.weight_decay, dataset_config.optimizer.lr, (beta1, beta2), device_type)
if init_from == 'resume':
    optimizer.load_state_dict(checkpoint['optimizer'])
checkpoint = None # free up memory

# compile the model
if compile:
    print("compiling the model... (takes a ~minute)")
    unoptimized_model = model
    model = torch.compile(model) # requires PyTorch 2.0

# wrap model into DDP container
if ddp:
    model = DDP(model, device_ids=[ddp_local_rank])

# helps estimate an arbitrarily accurate loss over either split using many batches
@torch.no_grad()
def estimate_loss():
    out = {}
    model.eval()
    for split in ['train', 'val']:
        losses = torch.zeros(eval_iters)
        for k in range(eval_iters):
            # x, y = get_batch(split)
            if split == 'train':
                sample = next(train_iterator)
            else:
                sample = next(val_iterator)

            tok_ids, doc_ids = sample.get("token_ids").long(), sample.get("document_ids").long()
            Y = torch.roll(tok_ids, shifts=-1, dims=1)
            Y[:, -1] = -100 
            
            if device_type == 'cuda':
                X = tok_ids.to(device)
                Y = Y.to(device)
                doc_ids = doc_ids.to(device)
            else:
                X = tok_ids

            with ctx:
                logits, loss = model(X, Y)
                
            losses[k] = loss.item()

        out[split] = losses.mean()
    model.train()
    
    return out

# learning rate decay scheduler (cosine with warmup)
def get_lr(it):
    # 1) linear warmup for warmup_iters steps
    if it < warmup_iters:
        return dataset_config.optimizer.lr * (it + 1) / (warmup_iters + 1)
    # 2) if it > lr_decay_iters, return min learning rate
    if it > dataset_config.lr_decay_iters:
        return dataset_config.optimizer.min_lr
    # 3) in between, use cosine decay down to min learning rate
    decay_ratio = (it - warmup_iters) / (dataset_config.lr_decay_iters - warmup_iters)
    assert 0 <= decay_ratio <= 1
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio)) # coeff ranges 0..1
    return dataset_config.optimizer.min_lr + coeff * (dataset_config.optimizer.lr - dataset_config.optimizer.min_lr)

# logging
if dataset_config.wandb_log and master_process:
    import wandb
    wandb.init(project=dataset_config.wandb_project, 
               entity=dataset_config.wandb_entity, 
               group=dataset_config.wandb_group,
               name=dataset_config.wandb_run_name, 
               config=config
               )

# training loop
# X, Y = get_batch('train') # fetch the very first batch
sample = next(train_iterator)
tok_ids, doc_ids = sample.get("token_ids").long(), sample.get("document_ids").long()
Y = torch.roll(tok_ids, shifts=-1, dims=1)
Y[:, -1] = -100 
if device_type == 'cuda':
    X = tok_ids.to(device)
    Y = Y.to(device)
    doc_ids = doc_ids.to(device)
else:
    X = tok_ids

t0 = time.time()
local_iter_num = 0 # number of iterations in the lifetime of this process
raw_model = model.module if ddp else model # unwrap DDP container if needed
running_mfu = -1.0
while True:
    # determine and set the learning rate for this iteration
    lr = get_lr(iter_num) if decay_lr else dataset_config.optimizer.lr
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

    # evaluate the loss on train/val sets and write checkpoints
    if iter_num % eval_interval == 0 and master_process:
        losses = estimate_loss()
        print(f"step {iter_num}: train loss {losses['train']:.4f}, val loss {losses['val']:.4f}")
        if dataset_config.wandb_log:
            wandb.log({
                "iter": iter_num,
                "train/loss": losses['train'],
                "val/loss": losses['val'],
                "lr": lr,
                "mfu": running_mfu*100, # convert to percentage
            })
        if losses['val'] < best_val_loss or always_save_checkpoint:
            best_val_loss = losses['val']
            if iter_num > 0:
                checkpoint = {
                    'model': raw_model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'model_args': model_args,
                    'iter_num': iter_num,
                    'best_val_loss': best_val_loss,
                    'config': config,
                }
                print(f"saving checkpoint to {out_dir}")
                torch.save(checkpoint, os.path.join(out_dir, 'ckpt.pt'))
    if iter_num == 0 and eval_only:
        break

    # forward backward update, with optional gradient accumulation to simulate larger batch size
    # and using the GradScaler if data type is float16
    # for batch_idx, sample in enumerate(train_loader):
    for micro_step in range(gradient_accumulation_steps):
        if ddp:
            # in DDP training we only need to sync gradients at the last micro step.
            # the official way to do this is with model.no_sync() context manager, but
            # I really dislike that this bloats the code and forces us to repeat code
            # looking at the source of that context manager, it just toggles this variable
            model.require_backward_grad_sync = (micro_step == gradient_accumulation_steps - 1)
        # import pdb; pdb.set_trace()
        with ctx:
            logits, loss = model(X, Y)
            loss = loss / gradient_accumulation_steps # scale the loss to account for gradient accumulation
        
        # immediately async prefetch next batch while model is doing the forward pass on the GPU
        # X, Y = get_batch('train')
        sample = next(train_iterator)
        tok_ids, doc_ids = sample.get("token_ids").long(), sample.get("document_ids").long()

        Y = torch.roll(tok_ids, shifts=-1, dims=1)
        Y[:, -1] = -100 

        if device_type == 'cuda':
            X = tok_ids.to(device)
            Y = Y.to(device)
            doc_ids = doc_ids.to(device)
        else:
            X = tok_ids
     
        # optimizer.zero_grad()
        # backward pass, with gradient scaling if training in fp16
        loss.backward()
        # scaler.scale(loss).backward()
    # clip the gradient
    # if grad_clip != 0.0:
    #     # scaler.unscale_(optimizer)
    #     torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
    # step the optimizer and scaler if training in fp16
    # scaler.step(optimizer)
    # scaler.update()
    # flush the gradients as soon as we can, no need for this memory anymore
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)

    # timing and logging
    t1 = time.time()
    dt = t1 - t0
    t0 = t1
    if iter_num % log_interval == 0 and master_process:
        # get loss as float. note: this is a CPU-GPU sync point
        # scale up to undo the division above, approximating the true total loss (exact would have been a sum)
        lossf = loss.item() * gradient_accumulation_steps
        if local_iter_num >= 5: # let the training loop settle a bit
            mfu = raw_model.estimate_mfu(dataset_config.training.batch_size * gradient_accumulation_steps, dt)
            running_mfu = mfu if running_mfu == -1.0 else 0.9*running_mfu + 0.1*mfu
        print(f"iter {iter_num}: loss {lossf:.4f}, time {dt*1000:.2f}ms, mfu {running_mfu*100:.2f}%")
    iter_num += 1
    local_iter_num += 1

    # termination conditions
    if iter_num > dataset_config.max_iters:
        break

if ddp:
    destroy_process_group()
