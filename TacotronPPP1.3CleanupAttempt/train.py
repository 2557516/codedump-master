import os
import time
import argparse
import math
import numpy as np
from numpy import finfo

import torch
from distributed import apply_gradient_allreduce
import torch.distributed as dist
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import ReduceLROnPlateau

from model import load_model
from model import Tacotron2
from data_utils import TextMelLoader, TextMelCollate
from loss_function import Tacotron2Loss
from logger import Tacotron2Logger
from hparams import create_hparams
from utils import to_gpu
import time
from math import e

from tqdm import tqdm
import layers
from utils import load_wav_to_torch
from scipy.io.wavfile import read

import os.path

from metric import alignment_metric

from apex import amp
from apex import optimizers as apexopt

save_file_check_path = "save"

def get_progress_bar(x, tqdm_params, hparams, rank=0):
    """Returns TQDM iterator if hparams.tqdm, else returns input.
    x = iteration
    tqdm_params = dict of parameters to feed to tqdm
    hparams = A tf.contrib.training.HParams object, containing notebook and tqdm parameters.
    rank = GPU rank, 0 = Main process, greater than 0 = Distributed Processes
    Example usage:
    for i in get_progress_bar(range(0,10), dict(smoothing=0.01), hparams, rank=rank):
        print(i)
    
    Will should progress bar only if hparams.tqdm is True."""
    if rank != 0: # Extra GPU's do not need progress bars
        return x
    
    if hparams.tqdm:
        if hparams.notebook:
            from tqdm.notebook import tqdm
        else:
            from tqdm import tqdm
        return tqdm(x, **tqdm_params)
    else:
        return x


def cprint(*message, b_tqdm=False, **args):
    """Uses tqdm.write() if hparams.notebook else print()"""
    if b_tqdm:
        tqdm.write(" ".join([str(x) for x in message]), *args)
    else:
        print(*message, **args)


class StreamingMovingAverage:
    def __init__(self, window_size):
        self.window_size = window_size
        self.values = []
        self.sum = 0

    def process(self, value):
        self.values.append(value)
        self.sum += value
        if len(self.values) > self.window_size:
            self.sum -= self.values.pop(0)
        return float(self.sum) / len(self.values)


def reduce_tensor(tensor, n_gpus):
    rt = tensor.clone()
    dist.all_reduce(rt, op=dist.ReduceOp.SUM)
    rt /= n_gpus
    return rt


def init_distributed(hparams, n_gpus, rank, group_name):
    assert torch.cuda.is_available(), "Distributed mode requires CUDA."
    print("Initializing Distributed")

    # Set cuda device so everything is done on the right GPU.
    torch.cuda.set_device(rank % torch.cuda.device_count())

    # Initialize distributed communication
    dist.init_process_group(
        backend=hparams.dist_backend, init_method=hparams.dist_url,
        world_size=n_gpus, rank=rank, group_name=group_name)

    print("Done initializing distributed")


def prepare_dataloaders(hparams):
    # Get data, data loaders and collate function ready
    trainset = TextMelLoader(hparams.training_files, hparams)
    valset = TextMelLoader(hparams.validation_files, hparams,
                           speaker_ids=trainset.speaker_ids)
    collate_fn = TextMelCollate(hparams.n_frames_per_step, hparams.load_alignments)

    if hparams.distributed_run:
        train_sampler = DistributedSampler(trainset,shuffle=False)
        shuffle = False
    else:
        train_sampler = None
        shuffle = False
    
    train_loader = DataLoader(trainset, num_workers=1, shuffle=shuffle,
                              sampler=train_sampler,
                              batch_size=hparams.batch_size, pin_memory=False,
                              drop_last=True, collate_fn=collate_fn)
    return train_loader, valset, collate_fn, train_sampler, trainset


def prepare_directories_and_logger(output_directory, log_directory, rank):
    if rank == 0:
        if not os.path.isdir(output_directory):
            os.makedirs(output_directory)
            os.chmod(output_directory, 0o775)
        logger = Tacotron2Logger(os.path.join(output_directory, log_directory))
    else:
        logger = None
    return logger


def warm_start_force_model(checkpoint_path, model):
    assert os.path.isfile(checkpoint_path)
    print("Warm starting model from checkpoint '{}'".format(checkpoint_path))
    checkpoint_dict = torch.load(checkpoint_path, map_location='cpu')
    pretrained_dict = checkpoint_dict['state_dict']
    model_dict = model.state_dict()
    # Fiter out unneccessary keys
    filtered_dict = {k: v for k,v in pretrained_dict.items() if k in model_dict and pretrained_dict[k].shape == model_dict[k].shape}
    model_dict_missing = {k: v for k,v in pretrained_dict.items() if k not in model_dict}
    model_dict_mismatching = {k: v for k,v in pretrained_dict.items() if k in model_dict and pretrained_dict[k].shape != model_dict[k].shape}
    pretrained_missing = {k: v for k,v in model_dict.items() if k not in pretrained_dict}
    if model_dict_missing: print(list(model_dict_missing.keys()),'does not exist in the current model and is being ignored')
    if model_dict_mismatching: print(list(model_dict_mismatching.keys()),"is the wrong shape and has been reset")
    if pretrained_missing: print(list(pretrained_missing.keys()),"doesn't have pretrained weights and has been reset")
    model_dict.update(filtered_dict)
    model.load_state_dict(model_dict)
    iteration = 0
    return model, iteration


def warm_start_model(checkpoint_path, model, ignore_layers):
    assert os.path.isfile(checkpoint_path)
    print("Warm starting model from checkpoint '{}'".format(checkpoint_path))
    checkpoint_dict = torch.load(checkpoint_path, map_location='cpu')
    model_dict = checkpoint_dict['state_dict']
    if len(ignore_layers) > 0:
        model_dict = {k: v for k, v in model_dict.items()
                      if k not in ignore_layers}
        dummy_dict = model.state_dict()
        dummy_dict.update(model_dict)
        model_dict = dummy_dict
    model.load_state_dict(model_dict)
    iteration = checkpoint_dict['iteration']
    iteration = 0
    return model, iteration


def load_checkpoint(checkpoint_path, model, optimizer):
    assert os.path.isfile(checkpoint_path)
    print("Loading checkpoint '{}'".format(checkpoint_path))
    checkpoint_dict = torch.load(checkpoint_path, map_location='cpu')
    
    state_dict = {k.replace("encoder_speaker_embedding.weight","encoder.encoder_speaker_embedding.weight"): v for k,v in torch.load(checkpoint_path)['state_dict'].items()}
    model.load_state_dict(state_dict) # tmp for updating old models
    
    #model.load_state_dict(checkpoint_dict['state_dict']) # original
    
    if 'optimizer' in checkpoint_dict.keys(): optimizer.load_state_dict(checkpoint_dict['optimizer'])
    if 'amp' in checkpoint_dict.keys(): amp.load_state_dict(checkpoint_dict['amp'])
    if 'learning_rate' in checkpoint_dict.keys(): learning_rate = checkpoint_dict['learning_rate']
    #if 'hparams' in checkpoint_dict.keys(): hparams = checkpoint_dict['hparams']
    if 'best_validation_loss' in checkpoint_dict.keys(): best_validation_loss = checkpoint_dict['best_validation_loss']
    if 'average_loss' in checkpoint_dict.keys(): average_loss = checkpoint_dict['average_loss']
    if (start_from_checkpoints_from_zero):
        iteration = 0
    else:
        iteration = checkpoint_dict['iteration']
    print("Loaded checkpoint '{}' from iteration {}" .format(
        checkpoint_path, iteration))
    return model, optimizer, learning_rate, iteration, best_validation_loss


def save_checkpoint(model, optimizer, learning_rate, iteration, hparams, best_validation_loss, average_loss, speaker_id_lookup, filepath):
    from utils import load_filepaths_and_text
    tqdm.write("Saving model and optimizer state at iteration {} to {}".format(
        iteration, filepath))
    
    # get speaker names to ID
    speakerlist = load_filepaths_and_text(hparams.speakerlist)
    speaker_name_lookup = {x[2]: speaker_id_lookup[x[3]] for x in speakerlist}
    
    torch.save({'iteration': iteration,
                'state_dict': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'learning_rate': learning_rate,
                #'amp': amp.state_dict(),
                'hparams': hparams,
                'speaker_id_lookup': speaker_id_lookup,
                'speaker_name_lookup': speaker_name_lookup,
                'best_validation_loss': best_validation_loss,
                'average_loss': average_loss}, filepath)
    tqdm.write("Saving Complete")


def validate(model, criterion, valset, iteration, batch_size, n_gpus,
             collate_fn, logger, distributed_run, rank, val_teacher_force_till, val_p_teacher_forcing, teacher_force=1):
    """Handles all the validation scoring and printing"""
    model.eval()
    with torch.no_grad():
        val_sampler = DistributedSampler(valset) if distributed_run else None
        val_loader = DataLoader(valset, sampler=val_sampler, num_workers=1,
                                shuffle=False, batch_size=batch_size,
                                pin_memory=False, drop_last=True, collate_fn=collate_fn)
        if teacher_force == 1:
            val_teacher_force_till = 0
            val_p_teacher_forcing = 1.0
        elif teacher_force == 2:
            val_teacher_force_till = 0
            val_p_teacher_forcing = 0.0
        val_loss = 0.0
        diagonality = torch.zeros(1)
        avg_prob = torch.zeros(1)
        for i, batch in tqdm(enumerate(val_loader), desc="Validation", total=len(val_loader), smoothing=0): # i = index, batch = stuff in array[i]
            x, y = model.parse_batch(batch)
            y_pred = model(x, teacher_force_till=val_teacher_force_till, p_teacher_forcing=val_p_teacher_forcing)
            rate, prob = alignment_metric(x, y_pred)
            diagonality += rate
            avg_prob += prob
            loss, gate_loss = criterion(y_pred, y)
            if distributed_run:
                reduced_val_loss = reduce_tensor(loss.data, n_gpus).item()
            else:
                reduced_val_loss = loss.item()
            val_loss += reduced_val_loss
            # end forloop
        val_loss = val_loss / (i + 1)
        diagonality = (diagonality / (i + 1)).item()
        avg_prob = (avg_prob / (i + 1)).item()
        # end torch.no_grad()
    model.train()
    if rank == 0:
        tqdm.write("Validation loss {}: {:9f}  Average Max Attention: {:9f}".format(iteration, val_loss, avg_prob))
        #logger.log_validation(val_loss, model, y, y_pred, iteration)
        if True:#iteration != 0:
            if teacher_force == 1:
                logger.log_teacher_forced_validation(val_loss, model, y, y_pred, iteration, val_teacher_force_till, val_p_teacher_forcing, diagonality, avg_prob)
            elif teacher_force == 2:
                logger.log_infer(val_loss, model, y, y_pred, iteration, val_teacher_force_till, val_p_teacher_forcing, diagonality, avg_prob)
            else:
                logger.log_validation(val_loss, model, y, y_pred, iteration, val_teacher_force_till, val_p_teacher_forcing, diagonality, avg_prob)
    return val_loss


def calculate_global_mean(data_loader, global_mean_npy, hparams):
    if global_mean_npy and os.path.exists(global_mean_npy):
        global_mean = np.load(global_mean_npy)
        return to_gpu(torch.tensor(global_mean).half()) if hparams.fp16_run else to_gpu(torch.tensor(global_mean).float())
    sums = []
    frames = []
    print('calculating global mean...')
    for i, batch in tqdm(enumerate(data_loader), total=len(data_loader), smoothing=0.001):
        text_padded, input_lengths, mel_padded, gate_padded,\
            output_lengths, speaker_ids, torchmoji_hidden, preserve_decoder_states = batch
        # padded values are 0.
        sums.append(mel_padded.double().sum(dim=(0, 2)))
        frames.append(output_lengths.double().sum())
    global_mean = sum(sums) / sum(frames)
    global_mean = to_gpu(global_mean.half()) if hparams.fp16_run else to_gpu(global_mean.float())
    if global_mean_npy:
        np.save(global_mean_npy, global_mean.cpu().numpy())
    return global_mean


def train(output_directory, log_directory, checkpoint_path, warm_start, warm_start_force, n_gpus,
          rank, group_name, hparams):
    """Training and validation logging results to tensorboard and stdout

    Params
    ------
    output_directory (string): directory to save checkpoints
    log_directory (string) directory to save tensorboard logs
    checkpoint_path(string): checkpoint path
    n_gpus (int): number of gpus
    rank (int): rank of current gpu
    hparams (object): comma separated list of "name=value" pairs.
    """
    hparams.n_gpus = n_gpus
    hparams.rank = rank
    if hparams.distributed_run:
        init_distributed(hparams, n_gpus, rank, group_name)
    
    torch.manual_seed(hparams.seed)
    torch.cuda.manual_seed(hparams.seed)
    
    train_loader, valset, collate_fn, train_sampler, trainset = prepare_dataloaders(hparams)
    speaker_lookup = trainset.speaker_ids
    
    if hparams.drop_frame_rate > 0.:
        if rank != 0: # if global_mean not yet calcuated, wait for main thread to do it
            while not os.path.exists(hparams.global_mean_npy): time.sleep(1)
        global_mean = calculate_global_mean(train_loader, hparams.global_mean_npy, hparams)
        hparams.global_mean = global_mean
    
    model = load_model(hparams)

    model.eval() # test if this is needed anymore

    learning_rate = hparams.learning_rate
	if hparams.Apex_optimizer: # apex optimizer is slightly faster with slightly more vram usage in my testing. Helps in both fp32 and fp16.
    	optimizer = apexopt.FusedAdam(model.parameters(), lr=learning_rate, weight_decay=hparams.weight_decay)
	else:
	    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=hparams.weight_decay)
    
    if hparams.fp16_run:
        model, optimizer = amp.initialize(model, optimizer, opt_level='O2')
    
    if hparams.distributed_run:
        model = apply_gradient_allreduce(model)
    
    criterion = Tacotron2Loss(hparams)
    
    logger = prepare_directories_and_logger(
        output_directory, log_directory, rank)
    
    # Load checkpoint if one exists
    best_validation_loss = 0.8 # used to see when "best_model" should be saved, default = 0.8, load_checkpoint will update to last best value.
    iteration = 0
    epoch_offset = 0
    if checkpoint_path is not None:
        if warm_start:
            model, iteration = warm_start_model(
                checkpoint_path, model, hparams.ignore_layers)
        elif warm_start_force:
            model, iteration = warm_start_force_model(
                checkpoint_path, model)
        else:
            model, optimizer, _learning_rate, iteration, best_validation_loss = load_checkpoint(
                checkpoint_path, model, optimizer)
            if hparams.use_saved_learning_rate:
                learning_rate = _learning_rate
        iteration += 1  # next iteration is iteration + 1
        epoch_offset = max(0, int(iteration / len(train_loader)))
        print('Model Loaded')
	
    ## LEARNING RATE SCHEDULER
    if True:
        from torch.optim.lr_scheduler import ReduceLROnPlateau
        min_lr = 1e-5
        factor = 0.1**(1/5) # amount to scale the LR by on Validation Loss plateau
        scheduler = ReduceLROnPlateau(optimizer, 'min', factor=factor, patience=20, cooldown=2, min_lr=min_lr, verbose=True)
        print("ReduceLROnPlateau used as (optional) Learning Rate Scheduler.")
    else: scheduler=False
    
    model.train()
    is_overflow = False
	
    validate_then_terminate = 0 # I use this for testing old models with new metrics
    if validate_then_terminate:
        val_loss = validate(model, criterion, valset, iteration,
            hparams.batch_size, n_gpus, collate_fn, logger,
            hparams.distributed_run, rank)
        raise Exception("Finished Validation")
    
    for param_group in optimizer.param_groups:
        param_group['lr'] = learning_rate
    
    rolling_loss = StreamingMovingAverage(min(int(len(train_loader)), 200))
    # ================ MAIN TRAINNIG LOOP! ===================
    for epoch in tqdm(range(epoch_offset, hparams.epochs), initial=epoch_offset, total=hparams.epochs, desc="Epoch:", position=1, unit="epoch"):
        tqdm.write("Epoch:{}".format(epoch))
        
        if hparams.distributed_run: # shuffles the train_loader when doing multi-gpu training
            train_sampler.set_epoch(epoch)
        start_time = time.time()
        # start iterating through the epoch
        for i, batch in tqdm(enumerate(train_loader), desc="Iter:  ", smoothing=0, total=len(train_loader), position=0, unit="iter"):
                    # run external code every iter, allows the run to be adjusted without restarts
                    if (i==0 or iteration % param_interval == 0):
                        try:
                            with open("run_every_epoch.py") as f:
                                internal_text = str(f.read())
                                if len(internal_text) > 0:
                                    #code = compile(internal_text, "run_every_epoch.py", 'exec')
                                    ldict = {'iteration': iteration}
                                    exec(internal_text, globals(), ldict)
                                else:
                                    print("No Custom code found, continuing without changes.")
                        except Exception as ex:
                            print(f"Custom code FAILED to run!\n{ex}")
                        globals().update(ldict)
                        locals().update(ldict)
                        if show_live_params:
                            print(internal_text)
                    if not iteration % 50: # check actual learning rate every 20 iters (because I sometimes see learning_rate variable go out-of-sync with real LR)
                        learning_rate = optimizer.param_groups[0]['lr']
                    # Learning Rate Schedule
                    if custom_lr:
                        old_lr = learning_rate
                        if iteration < warmup_start:
                            learning_rate = warmup_start_lr
                        elif iteration < warmup_end:
                            learning_rate = (iteration-warmup_start)*((A_+C_)-warmup_start_lr)/(warmup_end-warmup_start) + warmup_start_lr # learning rate increases from warmup_start_lr to A_ linearly over (warmup_end-warmup_start) iterations.
                        else:
                            if iteration < decay_start:
                                learning_rate = A_ + C_
                            else:
                                iteration_adjusted = iteration - decay_start
                                learning_rate = (A_*(e**(-iteration_adjusted/B_))) + C_
                        assert learning_rate > -1e-8, "Negative Learning Rate."
                        if old_lr != learning_rate:
                            for param_group in optimizer.param_groups:
                                param_group['lr'] = learning_rate
                    else:
                        scheduler.patience = scheduler_patience
                        scheduler.cooldown = scheduler_cooldown
                        if override_scheduler_last_lr:
                            scheduler._last_lr = override_scheduler_last_lr
                            print("Scheduler last_lr overriden. scheduler._last_lr =", scheduler._last_lr)
                        if override_scheduler_best:
                            scheduler.best = override_scheduler_best
                            print("Scheduler best metric overriden. scheduler.best =", override_scheduler_best)
            
            model.zero_grad()
            x, y = model.parse_batch(batch) # move batch to GPU
            y_pred = model(x, teacher_force_till=teacher_force_till, p_teacher_forcing=p_teacher_forcing, drop_frame_rate=drop_frame_rate)
            
            loss, gate_loss, attention_loss_item = criterion(y_pred, y)
            
            if hparams.distributed_run:
                reduced_loss = reduce_tensor(loss.data, n_gpus).item()
                reduced_gate_loss = reduce_tensor(gate_loss.data, n_gpus).item()
            else:
                reduced_loss = loss.item()
                reduced_gate_loss = gate_loss.item()
            
            if hparams.fp16_run:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()
            
			if grad_clip_thresh: # I can't think of a reason to disable gradient clipping but I'll leave it as an option
		        if hparams.fp16_run:
		            grad_norm = torch.nn.utils.clip_grad_norm_(
		                amp.master_params(optimizer), grad_clip_thresh)
		        else:
		            grad_norm = torch.nn.utils.clip_grad_norm_(
		                model.parameters(), grad_clip_thresh)
		        is_overflow = math.isinf(grad_norm) or math.isnan(grad_norm)
            
            optimizer.step()
            
            for j, param_group in enumerate(optimizer.param_groups):
                learning_rate = (float(param_group['lr'])); break
            
            if not is_overflow and rank == 0:
                duration = time.time() - start_time
                average_loss = rolling_loss.process(reduced_loss)
                tqdm.write("{} [Train_loss {:.4f} Avg {:.4f}] [Gate_loss {:.4f}] [Grad Norm {:.4f}] "
                      "[{:.2f}s/it] [{:.3f}s/file] [{:.7f} LR]".format(
                    iteration, reduced_loss, average_loss, reduced_gate_loss, grad_norm, duration, (duration/(hparams.batch_size*n_gpus)), learning_rate))
                if iteration % 20 == 0:
                    diagonality, avg_prob = alignment_metric(x, y_pred)
                    logger.log_training(
                        reduced_loss, grad_norm, learning_rate, duration, iteration, teacher_force_till, p_teacher_forcing, diagonality=diagonality, avg_prob=avg_prob)
                else:
                    logger.log_training(
                        reduced_loss, grad_norm, learning_rate, duration, iteration, teacher_force_till, p_teacher_forcing)
                start_time = time.time()
            if is_overflow and rank == 0:
                tqdm.write("Gradient Overflow, Skipping Step")
            
            if not is_overflow and ((iteration % (hparams.iters_per_checkpoint/1) == 0) or (os.path.exists(save_file_check_path))):
                # save model checkpoint like normal
                if rank == 0:
                    checkpoint_path = os.path.join(
                        output_directory, "checkpoint_{}".format(iteration))
                    save_checkpoint(model, optimizer, learning_rate, iteration, hparams, best_validation_loss, average_loss, speaker_lookup, checkpoint_path)
            
            if not is_overflow and ((iteration % int((hparams.iters_per_checkpoint)/1) == 0) or (os.path.exists(save_file_check_path)) or (iteration < 1000 and (iteration % 250 == 0))):
                if rank == 0 and os.path.exists(save_file_check_path):
                    os.remove(save_file_check_path)
                # perform validation and save "best_model" depending on validation loss
                val_loss = validate(model, criterion, valset, iteration,
                         hparams.val_batch_size, n_gpus, collate_fn, logger,
                         hparams.distributed_run, rank, val_teacher_force_till, val_p_teacher_forcing, teacher_force=1) #teacher_force
                val_loss = validate(model, criterion, valset, iteration,
                         hparams.val_batch_size, n_gpus, collate_fn, logger,
                         hparams.distributed_run, rank, val_teacher_force_till, val_p_teacher_forcing, teacher_force=2) #infer
                val_loss = validate(model, criterion, valset, iteration,
                         hparams.val_batch_size, n_gpus, collate_fn, logger,
                         hparams.distributed_run, rank, val_teacher_force_till, val_p_teacher_forcing, teacher_force=0) #validate (0.8 forcing)
                if use_scheduler:
                    scheduler.step(val_loss)
                if (val_loss < best_validation_loss):
                    best_validation_loss = val_loss
                    if rank == 0:
                        checkpoint_path = os.path.join(output_directory, "best_model")
                        save_checkpoint(model, optimizer, learning_rate, iteration, hparams, best_validation_loss, average_loss, speaker_lookup, checkpoint_path)
            
            iteration += 1
            # end of iteration loop
        # end of epoch loop


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-o', '--output_directory', type=str, default='outdir',
                        help='directory to save checkpoints')
    parser.add_argument('-l', '--log_directory', type=str, default='logdir',
                        help='directory to save tensorboard logs')
    parser.add_argument('-c', '--checkpoint_path', type=str, default=None,
                        required=False, help='checkpoint path')
    parser.add_argument('--warm_start', action='store_true',
                        help='load model weights only, ignore specified layers')
    parser.add_argument('--warm_start_force', action='store_true',
                        help='load model weights only, ignore all missing/non-matching layers')
    parser.add_argument('--n_gpus', type=int, default=1,
                        required=False, help='number of gpus')
    parser.add_argument('--rank', type=int, default=0,
                        required=False, help='rank of current gpu')
    parser.add_argument('--group_name', type=str, default='group_name',
                        required=False, help='Distributed group name')
    parser.add_argument('--hparams', type=str,
                        required=False, help='comma separated name=value pairs')
	
    args = parser.parse_args()
    hparams = create_hparams(args.hparams)
	
    torch.backends.cudnn.enabled = hparams.cudnn_enabled
    torch.backends.cudnn.benchmark = hparams.cudnn_benchmark
	
    print("FP16 Run:", hparams.fp16_run)
    print("Dynamic Loss Scaling:", hparams.dynamic_loss_scaling)
    print("Distributed Run:", hparams.distributed_run)
    print("cuDNN Enabled:", hparams.cudnn_enabled)
    print("cuDNN Benchmark:", hparams.cudnn_benchmark)
    
    train(args.output_directory, args.log_directory, args.checkpoint_path,
          args.warm_start, args.warm_start_force, args.n_gpus, args.rank, args.group_name, hparams)
