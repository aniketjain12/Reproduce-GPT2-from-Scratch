from dataclasses import dataclass
import inspect
import torch
import torch.nn as nn
from torch.nn import functional as F
import math
import time
import os

# ------------------------------------------------------------------------------------------

class CasualSelfAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        assert config.n_embd % config.n_head == 0
        # key, query, value projection for all heads, but in a batch
        self.c_attn = nn.Linear(config.n_embd, 3 * config.n_embd)
        # output projection
        self.c_proj = nn.Linear(config.n_embd, config.n_embd)
        self.c_proj.NANOGPT_SCALE_INIT = 1 # this is a flag to scale the weights for the initialization
        # regularization
        self.n_head = config.n_head
        self.n_embd = config.n_embd
        # not really a 'bias', more of a mask, but following the OpenAI/HF naming through
        self.register_buffer("bias", torch.tril(torch.ones(config.block_size,
         config.block_size)).view(1, 1,config.block_size, config.block_size))
        
    def forward(self, x):
        B, T, C = x.size() # batch size, sequence length, embedding dimensionality (n_embd)
        # calculate query, key, values for all heads in batch and move head forward to br the batch dim
        # nh is "number pf heads", he is "head size", and C (number of channels) = nh * hs
        # e,g. in GPT-2 (124M), n_head=12, hs=64, so nh*hs = c = 768 channels in the Transformer
        qkv = self.c_attn(x)
        q, k, v = qkv.split(self.n_embd, dim=2)
        k = k.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        q = q.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        v = v.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1))) # (B, nh, T, hs)

        # attention (materializes thhe large (T, T) matrix for all the queries and keys)
        # att = att.masked_fill(self.bias[:,:,:T,:T] == 0, float('-inf'))
        # att = F.softmax(att, dim=-1)
        # y = att @ v # (B, nh, T, T) * (B, nh, T, hs) -> (B, nh, T, hs)

        # Flash attention (does not materialize the large (T, T) matrix for all the queries and keys)
        y = F.scaled_dot_product_attention(q, k, v, is_causal=True)

        y = y.transpose(1, 2).contiguous().view(B, T, C) # re-assemble all head outputs side by side
        # output projection                     
        y = self.c_proj(y)
        return y
    
class MLP(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.c_fc = nn.Linear(config.n_embd, 4 * config.n_embd)
        self.gelu = nn.GELU() # gelu non-linear activation function
        self.c_proj = nn.Linear(4 * config.n_embd, config.n_embd)
        self.c_proj.NANOGPT_SCALE_INIT = 1

    def forward(self, x):
        x = self.c_fc(x)
        x = self.gelu(x)
        x = self.c_proj(x)
        return x

class Block(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.ln_1 = nn.LayerNorm(config.n_embd)
        self.attn = CasualSelfAttention(config)
        self.ln_2 = nn.LayerNorm(config.n_embd)
        self.mlp = MLP(config)

    def forward(self, x):
        x = x + self.attn(self.ln_1(x)) # Residual connection:  attention is a communication operation it is where all thr 1024 tokens lined up in a sequence and this is where the tokens communicate, this is where they talk to each other and exchange information so attention is aggergation function, pooling function it's a weighted sum function and it is a reduced operation
        x = x + self.mlp(self.ln_2(x)) # Map connection:  MLP is a feedforward neural network it is a non-linear function, it is a transformation function. It happens every single token individually, there is no information being collected or exchanges between the tokens, so the attention is reduced and mlp is the map 
        return x

@dataclass
class GPTConfig:
    block_size: int = 1024 # max sequence length
    vocab_size: int = 50257 # number of tokens: 50,000 BPE merges + 256 bytes tokens + 1 <endoftext> token
    n_layer: int = 12 # number of transformer layers
    n_head: int = 12 # number of heads
    n_embd: int = 768 # embedding dimension

class GPT(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config

        self.transformer = nn.ModuleDict(dict(
            wte = nn.Embedding(config.vocab_size, config.n_embd),
            wpe = nn.Embedding(config.block_size, config.n_embd),
            h = nn.ModuleList([Block(config) for _ in range(config.n_layer)]),
            ln_f = nn.LayerNorm(config.n_embd),
        ))
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)  

        # weigth sharing scheme
        self.transformer.wte.weight = self.lm_head.weight # it basically copies the data pointer and it copies the reference

        # init params
        self.apply(self._init_weights)

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            std = 0.02
            if hasattr(module, 'NANOGPT_SCALE_INIT'):
                std *= (2 * self.config.n_layer) ** -0.5
            torch.nn.init.normal_(module.weight, mean= 0.0, std=std)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
        

    def forward(self, idx, targets=None):
        # idx is of shape (B, T) where B is batch size and T is the sequence length
        B, T = idx.size()
        assert T <= self.config.block_size, f"Cannot forward sequence lenght {T}, block size is {self.config.block_size}"
        # forward the GPT model tokens and position embeddings
        pos = torch.arange(0, T, dtype=torch.long, device=idx.device) # shape (T)
        pos_emb = self.transformer.wpe(pos) # postion embedding of shape (T, n_embd)
        tok_emb = self.transformer.wte(idx) # token embedding of shape (B, T, n_embd)
        x = tok_emb + pos_emb # sum token and position embedding
        # forward the block of the transformer
        for block in self.transformer.h:
            x = block(x)
        # forward the final layernorm and the classifier
        x = self.transformer.ln_f(x) 
        logits = self.lm_head(x) # (B, T, vocab_size)
        loss = None
        if targets is not None:
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1))
        return logits, loss


    @classmethod
    def from_pretrained(cls, model_type):
        """Loads pretrained GPT-2 model weights from huggingface"""
        assert model_type in {'gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl'}
        from transformers import GPT2LMHeadModel
        print("loading weights from pretrained gpt: %s" % model_type)

        # n_layer, n_head and n_embd are determined from model_type
        config_args = {
            'gpt2': dict(n_layer=12, n_head=12, n_embd=768), # 124M parameters
            'gpt2-medium': dict(n_layer=24, n_head=16, n_embd=1024), # 350M parameters
            'gpt2-large': dict(n_layer=36, n_head=20, n_embd=1280), # 774M parameters
            'gpt2-xl': dict(n_layer=48, n_head=25, n_embd=1600), # 1558M parameters
        }[model_type]
        config_args['vocab_size'] = 50257 # always 50257 for GPT model checkpoints
        config_args['block_size'] = 1024 # always 1024 for GPT model checkpoints
        # create a from-scratch initialized minGPT model
        config = GPTConfig(**config_args)
        model = GPT(config)
        sd = model.state_dict()
        sd_keys = sd.keys()
        sd_keys = [k for k in sd_keys if not k.endswith('.attn.bias')] # discard this mask / buffer key and used for autorergressive mask

        # init a huggingface/transformers model
        model_hf = GPT2LMHeadModel.from_pretrained(model_type)
        sd_hf = model_hf.state_dict()

        # copy while ensuring all of the parameters are aligned and match in name and shapes
        sd_keys_hf = sd_hf.keys()
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.masked_bias')] # ignored these, just a buffer
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.bias')] # same just the mask (buffer)
        transposed = ['attn.c_attn.weight', 'attn.c_proj.weight', 'mlp.c_fc.weight', 'mlp.c_proj.weight']
        # basically the openai checkpoints use a "Conv1D" module, but we only want to use a vanilla Linear
        # this means that we have to transpose these weights when we import them
        assert len(sd_keys_hf) == len(sd_keys), f"mismatched keys: {len(sd_keys_hf)} != {len(sd_keys)}"
        for k in sd_keys_hf:
            if any(k.endswith(x) for x in transposed):
                # special treatment for the Conv1D weights we need to transpose
                assert sd_hf[k].shape[::-1] == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k].t())
            else:
                # vanilla copy over the other parameters
                assert sd_hf[k].shape == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k])
        return model
    

    def configure_optimizers(self, weight_decay, learning_rate, device):
        # start with all of the candidate parameters (that requires grad)
        param_dict = {pn: p for pn, p in self.named_parameters()}
        param_dict = {pn: p for pn, p in param_dict.items() if p.requires_grad}
        # create optim groups, Any parameters that is 2D will be weighted decayed, otherwise no.
        # i.e. all weight tensors in matmuls + embeddings decay, all biases and layernorms don't.
        decay_params = [p for n, p in param_dict.items() if p.dim() >= 2]
        nodecay_params = [p for n, p in param_dict.items() if p.dim() < 2]
        optim_groups = [
            {'params': decay_params, 'weight_decay': weight_decay},
            {"params": nodecay_params, 'weight_decay': 0.0}
        ]
        num_decay_params = sum(p.numel() for p in decay_params)
        num_nodecay_params = sum(p.numel() for p in nodecay_params)
        print(f"num decayed parameter tensors: {len(decay_params)}, with {num_decay_params:,} parameters")
        print(f"num non-decayed parameter tensors: {len(nodecay_params)}, with {num_nodecay_params:,} parameters")
        # Created Adamw optimizer and use the fused version if it is available 
        fused_available = 'fused' in inspect.signature(torch.optim.AdamW).parameters
        use_fused = fused_available and 'cuda' in device
        print(f"using fused AdamW: {use_fused}")
        optimizer = torch.optim.AdamW(optim_groups, lr = learning_rate, betas=(0.9, 0.95), eps= 1e-8, fused=use_fused)
        return optimizer    
# ------------------------------------------------------------------------------------------
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
num_return_sequences = 5
max_length = 30
# prefix tokens
from helloswag import render_example
import tiktoken
import numpy as np
from numpy import split


def load_tokens(filename):
    npt = np.load(filename)
    ptt = torch.tensor(npt, dtype=torch.long)
    return ptt

class DataLoaderLite:
    def __init__(self, B, T, process_rank, num_processes):
        self.B = B
        self.T = T
        self.process_rank = process_rank
        self.num_processes = num_processes
        assert split in {'train', 'val'}

        # get the shard filenames
        data_root = "edu_fineweb10B"
        shards = os.listdir(data_root)
        shards = [s for s in shards if split in s]
        shards = sorted(shards)
        shards = [os.path.join(data_root, s) for s in shards]
        self.shards = shards
        assert len(shards) > 0, f"no shards found in split {split}"
        if master_process:
            print(f"found {len(shards)} shards in split {split}")
        self.reset() # reset the state of the dataloader

    def reset(self):       
        #state
        self.current_shard = 0 # current position in the data
        self.tokens= load_tokens(self.shards[self.current_shard])
        self.current_position = self.B * self.T * self.process_rank # start at the beginning of the data for this process

    def next_batch(self):
        B, T = self.B, self.T
        buf = self.tokens[self.current_position : self.current_position + B * T + 1]
        x = (buf[:-1]).view(B, T) # inputs
        y = (buf[1:]).view(B, T) # tragets

        # advance the position in the tensor
        self.current_position += B * T * self.num_processes # move the position forward by the number of tokens in the batch
        # if loading the next batch would be out of bounds, reset the position
        if self.current_position + (B * T * self.num_processes + 1) > len(self.tokens):
            self.current_shard = (self.current_shard + 1) % len(self.shards) # move to the next shard
            self.tokens = load_tokens(self.shards[self.current_shard]) # load the next shard
            self.current_position = self.B * self.T * self.process_rank
        return x, y
    
# -----------------------------------------------------------------------------
# helper function for HellaSwag eval
# takes tokens, mask, and logits, returns the index of the completion with the lowest loss

def get_most_likely_row(tokens, mask, logits):
    # evaluate the autoregressive loss at all positions
    shift_logits = (logits[..., :-1, :]).contiguous()
    shift_tokens = (tokens[..., 1:]).contiguous()
    flat_shift_logits = shift_logits.view(-1, shift_logits.size(-1))
    flat_shift_tokens = shift_tokens.view(-1)
    shift_losses = F.cross_entropy(flat_shift_logits, flat_shift_tokens, reduction='none')
    shift_losses = shift_losses.view(tokens.size(0), -1)
    # now get the average loss just for the completion region (where mask == 1), in each row
    shift_mask = (mask[..., 1:]).contiguous() # we must shift mask, so we start at the last prompt token
    masked_shift_losses = shift_losses * shift_mask
    # sum and divide by the number of 1s in the mask
    sum_loss = masked_shift_losses.sum(dim=1)
    avg_loss = sum_loss / shift_mask.sum(dim=1)
    # now we have a loss for each of the 4 completions
    # the one with the lowest loss should be the most likely
    pred_norm = avg_loss.argmin().item()
    return pred_norm
    
# ------------------------------------------------------------------------------------------
# simple launch:
# python gpt2_train.py
# DDP launch for e.g. 8 GPUs:
# torchrun --standalone --nproc_per_node=8 for_multi_gpus.py

# ------------------------------------------------------------------------------------------
# attend to autodetect the device

# run the training loop
from torch.distributed import init_process_group, destroy_process_group
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.distributed as dist

# set up DPP (distributed data parallel).
# torchrun command is used the env variables RANK, LOCAL_RANK, WORLD_SIZE
ddp = int(os.environ.get('RANK', -1)) != -1 # is this a dpp run?
if ddp:
    #use of dpp atm depends CUDA, we set the device appropriately according to rank
    assert torch.cuda.is_available(), "for now i think we need CUDA for DPP"
    init_process_group(backend='nccl')
    ddp_rank = int(os.environ['RANK'])
    ddp_local_rank = int(os.environ['LOCAL_RANK'])
    ddp_world_size = int(os.environ['WORLD_SIZE']) # e.g.: 8 GPUs
    device = f"cuda:{ddp_local_rank}"
    torch.cuda.set_device(device)
    master_process = ddp_rank == 0 # this process will do logging, checkpointing, etc.
else:
    # vanilla, non-DDP run
    ddp_rank = 0
    ddp_local_rank = 0
    ddp_world_size = 1
    master_process = True  
    # attempt to autodetect device 
    device = 'cpu'
    if torch.cuda.is_available():
        device = 'cuda'
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        device = 'mps'
    print(f"using device: {device}")

torch.manual_seed(1337)
if torch.cuda.is_available():
    torch.cuda.manual_seed(1337)

total_batch_size = 524288 # 2 **19, ~0.5M, in number of tokens
B = 64 # micro batch size
T = 1024 # sequence length
# B = 32 # micro batch size for GPT-3
# T = 2048 # sequence length for GPT-3
assert total_batch_size % (B * T * ddp_world_size) == 0, "make sure total_batch_size is divisible by B * T * ddp_world_size"
grad_accum_steps = total_batch_size // (B * T * ddp_world_size) # number of micro steps to take before updating the weights
if master_process:
    print(f"total desired batch size: {total_batch_size}")
    print(f"=> calculate gradient accumulation steps: {grad_accum_steps}")


train_loader = DataLoaderLite(B=B, T=T, process_rank = ddp_rank, num_processes = ddp_world_size, split = 'train') 
val_loader = DataLoaderLite(B=B, T=T, process_rank = ddp_rank, num_processes = ddp_world_size, split = 'val') 
#train_loader = DataLoaderLite(B=16, T=1024) # Batch size = 0.5e6 * 1024 / 16 = 32768 tokens per second or 0.5e6 /1024 = 488.28 tokens per second 
torch.set_float32_matmul_precision('high')
# create the model
model = GPT(GPTConfig(vocab_size=50304))
#model = GPT.from_pretrained("gpt2") # load the pretrained weights from huggingface or init from OpenAI GPT-2
model.to(device)

use_compile = False # torch.compile interference with Helloswag eval and Generation, TODO fix
if use_compile:
    #model = torch.compile(model, mode="reduce-overhead", fullgraph=True)
    model = torch.compile(model) # 2x speedup on MPS and CUDA, 1.5x on CPU

if ddp:
    model = DDP(model, device_ids=[ddp_local_rank])
raw_model = model.module if ddp else model


max_lr = 6e-4
min_lr = max_lr * 0.1
warmup_steps = 715 # 375e6 / 2**19
max_steps = 19073 * 4 # 19073 steps is ~1 epoch, if the data is 10B tokens # 10e9 / 2**19 

def get_lr(it):
    # 1) linear warmup for warmup_iters steps
    if it < warmup_steps:
        return max_lr * (it + 1) / warmup_steps
    # 2) if it > lr_decay_iters, return min learning rate
    if it > max_steps:
        return min_lr
    # 3) in between, use cosine decay down to min learning rate
    decay_ratio = (it - warmup_steps) / (max_steps - warmup_steps)
    assert 0 <= decay_ratio <= 1
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio)) # coeff starts at 1 and goes to 0
    return min_lr + coeff * (max_lr - min_lr)

# optimize!
# can also use eleuthar eval harness
optimizer = raw_model.configure_optimizers(weight_decay = 0.1, learning_rate = 6e-4, device=device)
log_dir = "log"
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"log.txt")
with open(log_file, "w") as f:
    pass

for step in range(max_steps):
    t0 = time.time()
    last_step = (step == max_steps - 1)

    if step % 250 == 0 or last_step:
        model.eval()
        val_loader.reset()
        with torch.no_grad():
            val_loss_accum = 0.0
            val_loss_steps = 20
            for _ in range(val_loss_steps):
                x, y = val_loader.next_batch()
                x, y = x.to(device), y.to(device)
                with torch.autocast(device_type = device, dtype=torch.bfloat16):
                    logits, loss = model(x, y)
                loss = loss / val_loss_steps # scale the loss by the number of micro steps
                val_loss_accum += loss.detach()
        if ddp:
            dist.all_reduce(val_loss_accum, op = dist.ReduceOp.AVG)
        if master_process:
            print(f"validation loss: {val_loss_accum.item():.4f}")
            with open(log_file, "a") as f:
                f.write(f"{step} val {val_loss_accum.item():.4f}\n")
            if step > 0 and (step % 5000 == 0 or last_step):
                print("saving checkpoint")
                checkpoint_path = os.path.join(log_dir, f"model_{step:05d}.pt")
                checkpoint = {
                    'model': raw_model.state_dict(),
                    'config': raw_model.config,
                    'step': step,
                    'val_loss': val_loss_accum.item(),
                    #'optimizer': optimizer.state_dict(),
                }
                # you might also want to add optimizer.state_dict() to the checkpoint and
                # rng seed etc., if you want to more exactly resume training
                torch.save(checkpoint, checkpoint_path)

    # once in a while evaluate helloswag
    if (step % 250 == 0 or last_step) and (not use_compile):
        num_currect_norm = 0
        num_total = 0
        for i, example in enumerate(val_loader.iterate_examples("val")):
            if i % ddp_world_size != ddp_rank:
                continue
            _, tokens, mask, label = render_example(example)
            tokens = tokens.to(device)
            mask = mask.to(device)
            with torch.no_grad():
                with torch.autocast(device_typr=device, dtype=torch.bfloat16):
                    logits, loss = model(tokens)
                pred_norm = get_most_likely_row(tokens, mask, logits)
                num_total += 1
                num_correct_norm += int(pred_norm == label)
        if ddp:
            num_total = torch.tensor(num_total, dtype=torch.long, device=device)
            num_correct_norm = torch.tensor(num_correct_norm, dtype=torch.long, device=device)
            dist.all_reduce(num_total, op=dist.ReduceOp.SUM)
            dist.all_reduce(num_correct_norm, op=dist.ReduceOp.SUM)
            num_total = num_total.item()
            num_correct_norm = num_correct_norm.item()
        acc_norm = num_correct_norm / num_total
        if master_process:
            print(f"Helloswag accuracy: {num_correct_norm}/{num_total} = {acc_norm:.4f}")
            with open(log_file, "a") as f:
                f.write(f"{step} hella {acc_norm:.4f}\n")
                
    # once in a while generate from the model (except step 0, which is noise)
    if ((step > 0 and step % 250 == 0) or last_step) and (not use_compile):
        model.eval()
        num_return_sequences = 4
        max_length = 32
        enc = tiktoken.get_encoding('gpt2')
        tokens = enc.encode("Hello, I.m a language model,")
        tokens = torch.tensor(tokens, dtype=torch.long)
        tokens = tokens.unsqueeze(0).repeat(num_return_sequences, 1)
        xgen = tokens.to(device)
        sample_rng = torch.Generator(device=device)
        sample_rng.manual_seed(42 + ddp_rank)
        while xgen.size(1) < max_length:
            with torch.no_grad():
                with torch.autocast(device_type=device, dtype=torch.bfloat16):
                    # take the logits at the last position: (B, vocab_size)
                    logits, loss = model(xgen) # (B, T, vocab_size)
                    # take the logits at the last postion
                    logits = logits[:, -1, :]   # (B, vocab_size)
                    # get the probabilities
                    probs = F.softmax(logits, dim=-1)
                    # do top-k sampling of 50 (hugging face pipeline default)
                    # topk_probs here becomes (5, 50), topk_indices is (5, 50)
                    topk_probs, topk_indices = torch.topk(probs, 50, dim=-1)
                    # select a token from the top-k probabilities
                    # note: multinomial does not demand the input to sum to 1
                    ix = torch.multinomial(topk_probs, 1, generator=sample_rng) # (B, 1)
                    # gather the corresponding indices
                    xcol = torch.gather(topk_indices, -1, ix)
                    # append to the sequence
                    xgen = torch.cat((xgen, xcol), dim=1)
        # print the generated sequences
        for i in range(num_return_sequences):
            tokens = xgen[i, :max_length].tolist()
            decode = enc.decode(tokens)
            print(f"rank {ddp_rank} sample {i}: {decode}")

    # training loop
    model.train()
    optimizer.zero_grad()
    loss_accum = 0.0
    for micro_step in range(grad_accum_steps):
        x, y = train_loader.next_batch()
        x, y = x.to(device), y.to(device)
        with torch.autocast(device_type = device, dtype=torch.bfloat16):
            logits, loss = model(x, y)
        # we have to scale the loss to account for gradient accumulation,
        # because the gradients just add on each succesive backward(),
        # addition of gradients corresponds to a SUM in the objective, but
        # instead of a SUM we want Mean, Scale the loss here so it comes out to the same
        loss = loss / grad_accum_steps # scale the loss by the number of micro steps
        loss_accum += loss.detach()
        if ddp:
            model.required_backward_grad_sync = (micro_step == grad_accum_steps - 1) # sync gradients only on the last micro step to be True

        loss.backward()
    if ddp:
        dist.all_reduce(loss_accum, op = dist.ReduceOp.AVG)
    norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0) # gradient clipping
    # determine and set the learning rate for the iteration
    lr = get_lr(step) # get the learning rate for this step
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    optimizer.step() # step function in optimizer will update the paramerters and to decrease the loss
    torch.cuda.synchronize()
    t1 = time.time()
    dt = (t1 - t0) * 1000 # time difference in milliseconds
    tokens_per_second = train_loader.B * train_loader.T * grad_accum_steps * ddp_world_size
    tokens_per_sec = tokens_per_second / dt 
    if master_process:
        print(f"step {step:.4f} | loss: {loss_accum.item():.6f} | lr: {lr:.4e} | norm: {norm:.4f} | dt: {dt:.2f}ms | token_per_sec: {tokens_per_sec: .2f}") # loss is a tensor with a single element and it lives on the GPU
        with open(log_file, "a") as f:
            f.write(f"{step} train {loss_accum.item():.f6}\n")
if ddp:
    destroy_process_group()


