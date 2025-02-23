# Copyright 2020 HKBU. All Rights Reserved.
# Copyright 2018 Uber Technologies, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""
Support Bayesian Optimization to fine-tune the buffer size for tensor fusion.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time
import torch
import collections
import numpy as np
import wfbp.torch as hvd

from mpi4py import MPI


# from comm_core import rank, size, Communicator, init as comm_init
from tensorfusion import CommReduceScatter, CollectiveOp



from tuner import Tuner
import utils


#comm_init()
mpi_comm = MPI.COMM_WORLD
comm = None
all_gather_comm = None
reduce_scatter_comm = None

NUM_NEARBY_LAYERS = 8
THRESHOLD = 25  # default: 25 mb
THRESHOLD_TUNING = 1
NSTREAMS = 1

def init():
    # global comm
    # global all_gather_comm 
    # global reduce_scatter_comm 
    # comm = Communicator(NSTREAMS)
    # reduce_scatter_comm = CommReduceScatter(op=CollectiveOp.REDUCE_SCATTER)
    # all_gather_comm = CommReduceScatter(op=CollectiveOp.ALL_GATHER)
    
    return


DEBUG = False

class _DistributedOptimizer(torch.optim.Optimizer):
    def __init__(self, params, model, 
            num_nearby_layers=NUM_NEARBY_LAYERS, 
            threshold=THRESHOLD, 
            threshold_tuning=THRESHOLD_TUNING, 
            exclude_parts=''):
        r"""Distributed optimizer with overlapping reduceScatter and allGather and tensor fusion.

        Args:
            params: optimizer parameters.
            model: training model.
            num_nearby_layers: number of neaby layers merged for tensor fusion.
            threshold: buffer size threshold for tensor fusion.
            threshold_tuning: enable BO to finetune threshold.
        """
        super(self.__class__, self).__init__(params)
        self._model = model
        self._threshold = threshold
        self._threshold_tuning = threshold_tuning
        self._num_nearby_layers = num_nearby_layers
        self._exclude_parts=exclude_parts
        self._num_steps = 0
        self._grad_accs = []
        
        self._tuner = None
        self._next_threshold = None

        # parameter names
        named_parameters = list(model.named_parameters())
        if len(named_parameters) > 0:
            self._param_names = {v: k for k, v in sorted(named_parameters)}
        else:
            self._param_names = {v: 'param.noname.%s' % i
                                     for param_group in self.param_groups
                                     for i, v in enumerate(param_group['params'])}

        if size() > 1:
            self._register_hooks()
            if self._threshold is not None:
                self._generate_groups_with_threshold()
                if self._threshold_tuning:
                    self._tuner = Tuner(x=threshold, bound=(1.0, 256.0))
            else:
                self._generate_groups_with_nearby_layers()

    def _generate_groups_with_nearby_layers(self):
        """
        Generate groups with nearby layers for tensor fusion.
        """
        module_groups = []
        current_group = []
        for i, module in enumerate(self._register_modules):
            current_group.append(module)
            if not self._num_nearby_layers < 0 and (i+1) % self._num_nearby_layers == 0: 
                module_groups.append(current_group)
                current_group = []
        if len(current_group) > 0:
            module_groups.append(current_group)
        self._prepare_tensor_fusion(module_groups)
        
    def _generate_groups_with_threshold(self):
        """
        Generate groups with buffer size threshold (in MB) for tensor fusion. 
        """
        module_sizes = {}
        for module in self._register_modules:
            module_name = self._module_names[module]
            tot_size = 0
            for p in self._module_direct_parameters[module_name]:
                tot_size += p.data.numel()
            module_sizes[module_name] = tot_size*4/1024/1024

        module_groups = []
        current_group = []
        tot_size = 0
        for module in self._register_modules: # forward order
            mod_size = module_sizes.get(self._module_names[module])
            if tot_size == 0 or tot_size + mod_size < self._threshold:
                current_group.append(module)
                tot_size += mod_size
            else:
                module_groups.append(current_group)
                current_group = [module]
                tot_size = mod_size
        if len(current_group) > 0:
            module_groups.append(current_group)
        self._prepare_tensor_fusion(module_groups)

    def _update_groups_with_threshold(self, threshold):
        """
        Update groups with buffer size threshold for fine-tuning tensor fusion. 
        """
        # bcast next threshold from rank 0 to other workers
        threshold = mpi_comm.bcast(threshold, root=0)

        #threshold=torch.tensor(threshold).cuda()
        #comm.bcast(threshold, 0)
        #comm.synchronize()
        
        #if rank() == 0:
        #    print("Step [%d]: finetune the threshold to %.2f" % (self._num_steps, threshold))

        # clear buffers
        for pad_buffer in self._pad_buffers:
            del pad_buffer
        for shard_buffer in self._shard_buffers:
            del shard_buffer

        # update tensor fusion groups
        self._threshold = threshold
        self._next_threshold = None
        self._generate_groups_with_threshold()

    def _prepare_tensor_fusion(self, module_groups):
        """
        Prepare tensor fusion based on module groups, e.g. [[m1, m2], [m3]] in forward order.
        """
        assert module_groups[0][0] == self._register_modules[0], "Module groups are not in forward order."
        self._pad_buffers = []       # group buffers with padding
        self._shard_buffers = []     # sharded group buffers
        self._module_group_idx = {}  # get group idx of module by name
        self._param_group_idx = {}   # get group idx of parameter by name
        
        start_p = 0
        param_groups = []
        for group_idx, module_group in enumerate(module_groups):
            current_param_group = []
            start_p = 0
            for sub_idx, module in enumerate(module_group):
                module_name = self._module_names[module]
                self._module_group_idx[module_name] = (group_idx, sub_idx)

                for p in self._module_direct_parameters[module_name]:
                    param_name = self._param_names[p]
                    numel = p.data.numel()
                    self._param_group_idx[param_name] = (group_idx, len(current_param_group),
                            start_p, start_p+numel)
                    current_param_group.append(param_name)
                    start_p += numel

            param_groups.append(current_param_group)        
            _, pad_tensor, shard_tensor = self._get_pad_tensor(p.data, start_p, size())
            self._pad_buffers.append(pad_tensor)
            self._shard_buffers.append(shard_tensor)

        assert len(module_groups) == len(param_groups)
        self._num_groups = len(module_groups)
        self._module_group_flags = [0]*len(module_groups) # check whether module group is gathered
        self._param_group_flags = [[0]*len(g) for g in param_groups] # check whether param group is ready

        if rank() == 0: 
            print('#Tensor fusion groups:', len(module_groups))
            print('Buffer sizes (MB):', 
                    ', '.join('{:.2f}'.format(buf.numel()*4/1024/1024) for buf in self._pad_buffers))
            #print('module groups:', module_groups)
            #print('parameter groups:', param_groups)
    
    @torch.no_grad()
    def _get_pad_tensor(self, tensor, numel, size): 
        """
        Get padding tensors
        """
        pad_num = size - numel % size
        pad_tensor = tensor.new_empty(numel+pad_num)
        shard_tensor = tensor.new_empty((numel+pad_num) // size)
        return pad_num, pad_tensor, shard_tensor

    def _register_hooks(self):
        """
        Register hooks for both feed-forward and back-propagation. 
        """
        # find all trainable modules and parameters
        self._register_modules = []
        self._register_parameters = []
        self._module_names = {}             # get module name
        self._module_direct_parameters = {} # get module direct params by name
        
        register_param_names = []
        for module in self._model.modules():
            params = [p for p in module.parameters(recurse=False) if p.requires_grad]
            direct_params = []
            for p in params:
                # avoid repeat registration, e.g. shared parameters
                p_name = self._param_names.get(p)
                if p_name not in register_param_names:
                    register_param_names.append(p_name)
                    direct_params.append(p)
            if len(direct_params) > 0:
                module_name = 'module_name_%s_%d' % (module.__class__.__name__, 
                        len(self._register_modules))
                self._module_names[module] = module_name
                self._register_modules.append(module)
                self._register_parameters.extend(direct_params)
                self._module_direct_parameters[module_name] = direct_params
        
        # register forward hooks
        for i, module in enumerate(self._register_modules):
            module.register_forward_pre_hook(self._forward_pre_hook)
        
        # register backward hooks
        for i, p in enumerate(self._register_parameters):
            p.grad = p.data.new(p.size()).zero_()
            p_tmp = p.expand_as(p)
            grad_acc = p_tmp.grad_fn.next_functions[0][0]
            grad_acc.register_hook(self._make_hook(p))
            self._grad_accs.append(grad_acc)
            #if rank() == 0:
            #    print("register hook for %s" % self._param_names.get(p))

    def _make_hook(self, p):
        """
        Add hooks for backward propagation. 
        """
        def hook(*ignore):
            assert not p.grad.requires_grad
            name = self._param_names.get(p)
            tensor = p.grad.data
            # Merging gradient tensors with padding for reduce_scatter
            new_name, pad_grad, shard_grad = self._push_to_buffer(name, tensor)
            if pad_grad is not None: 
                handle = reduce_scatter_comm.collective_async_(new_name, pad_grad, shard_grad)
        return hook

    def _push_to_buffer(self, name, tensor):
        """
        Push tensor to buffer for fusion.
        """
        group_idx, sub_idx, start_p, end_p = self._param_group_idx[name]
        with torch.no_grad():
            pad_buffer = self._pad_buffers[group_idx]
            pad_buffer[start_p:end_p].copy_(tensor.view(-1))
            self._param_group_flags[group_idx][sub_idx] = 1
            for flag in self._param_group_flags[group_idx]:
                if flag == 0: # not ready
                    return name, None, None
            comm_name = 'reduceScatter-group-%d' % group_idx
            shard_buffer = self._shard_buffers[group_idx]
            return comm_name, pad_buffer, shard_buffer

    def _forward_pre_hook(self, module, input):
        """
        Add hooks for pre-feedfoward.
        """
        if torch.is_grad_enabled() and self._num_steps > 0:
            name = self._module_names.get(module)
            group_idx, sub_idx = self._module_group_idx[name]

            # sync allGather for this group and send for next group
            if sub_idx == 0 and self._module_group_flags[group_idx] == 0:
                all_gather_comm.synchronize()
                self._module_group_flags[group_idx] = 1  # done
                if group_idx < self._num_groups - 1 and self._module_group_flags[group_idx+1] == 0:
                    self._allgather_one_group(group_idx+1)

            # update params for this module
            self._update_one_module(module, name, group_idx)
            
            # temporary solution: update tensor fusion just before the last module
            if self._threshold_tuning and self._next_threshold is not None:
                if module == self._register_modules[-1]:
                    self._update_groups_with_threshold(self._next_threshold)
    
    

    def _update_one_module(self, module, module_name, group_idx):
        """Update model parameters in the module"""
        pad_grad = self._pad_buffers[group_idx]

        for p in self._module_direct_parameters[module_name]:
            # copy grad values from buffers
            name = self._param_names.get(p)
            #if name not in self._param_group_idx:
            #    continue
            group_idx_p, _, start_p, end_p = self._param_group_idx[name]
            assert group_idx_p == group_idx
            p.grad.data.view(-1).copy_(pad_grad[start_p:end_p])
            # to be checked: average the grad here
            p.grad.data.div_(size())
            # apply one optimizer step
            self._sgd(p)

    def _sgd(self, p):
        """Apply SGD to update parameter p"""
        for group in self.param_groups:
            weight_decay = group['weight_decay']
            momentum = group['momentum']
            dampening = group['dampening']
            nesterov = group['nesterov']

            d_p = p.grad.data
            if weight_decay != 0:
                wd = p.data
                d_p.add_(wd, alpha=weight_decay)
            if momentum > 0:
                param_state = self.state[p]
                if 'momentum_buffer' not in param_state:
                    param_state['momentum_buffer'] = torch.zeros_like(p.data)
                    buf = param_state['momentum_buffer']
                    buf.mul_(momentum).add_(d_p)
                else:
                    buf = param_state['momentum_buffer']
                    buf.mul_(momentum).add_(d_p, alpha=1 - dampening)
                if nesterov:
                    d_p = d_p.add(momentum, buf)
                else:
                    d_p = buf
            p.data.add_(d_p, alpha=-group['lr'])
            p.grad.fill_(0.0) # zero grad

    def zero_grad(self):
        pass

    def _allgather_one_group(self, group_idx):
        """Apply allgather on one group"""
        pad_grad = self._pad_buffers[group_idx]
        shard_grad = self._shard_buffers[group_idx]
        comm_name = "allGather-group-%d" % group_idx
        all_gather_comm.collective_async_(comm_name, pad_grad, shard_grad)        

    def _bp_barrier(self):
        """
        Synchronize the reduce-scatter operations and start the all-gather on the first group.
        """
        reduce_scatter_comm.synchronize()
        self._allgather_one_group(group_idx=0)
        #if rank() == 0:
        #    print("param group flags:", self._param_group_flags)
        #    print("module group flags:", self._module_group_flags)
        
        # Clear flags
        for group_idx in range(len(self._param_group_flags)):
            self._param_group_flags[group_idx] = [0] * len(self._param_group_flags[group_idx])
        self._module_group_flags = [0] * self._num_groups

    def step(self, closure=None):
        """
        Performs a single optimization step.
        """
        if size() > 1:
            self._bp_barrier()
        #else:
        #    todo: step with non-distributed optimzier
        
        if self._threshold_tuning:
            self._next_threshold = self._tuner.step()
        
        # Note: the last step is skipped
        self._num_steps += 1


def DistributedOptimizer(optimizer, model, compression=None, is_sparse=False, density=0.001, seq_layernames=None, layerwise_times=None, norm_clip=None, threshold=0, writer=None, gradient_path=None, fp16=False, mgwfbp=False, rdma=False, multi_job_scheduling=False, exclude_parts=''):
    """
    Wrap optimizer to gurantee the consistency. 
    Warning: some functions are not supported now, so we will simply skip these parameters.
    Arguments:
        optimizer: Optimizer to use for computing gradients and applying updates.
        named_parameters: A mapping between parameter names and values. Used for naming of
                          allreduce operations. Typically just `model.named_parameters()`.
        compression: Compression algorithm used during allreduce to reduce the amount
                     of data sent during the each parameter update step.  Defaults to
                     not using compression.
    """
    # We dynamically create a new class that inherits from the optimizer that was passed in.
    # The goal is to override the `step()` method with an allreduce implementation.
    cls = type(optimizer.__class__.__name__, (optimizer.__class__,),
               dict(_DistributedOptimizer.__dict__))

    return cls(optimizer.param_groups, model, exclude_parts=exclude_parts)

def broadcast_parameters(params, root_rank):
    """
    Broadcasts the parameters from root rank to all other processes.
    Typical usage is to broadcast the `model.state_dict()`,
    `model.named_parameters()`, or `model.parameters()`.

    Arguments:
        params: One of the following:
            - list of parameters to broadcast
            - dict of parameters to broadcast
        root_rank: The rank of the process from which parameters will be
                   broadcasted to all other processes.
    """
    if isinstance(params, dict):
        params = sorted(params.items())
    elif isinstance(params, list):
        # support both named_parameters() and regular parameters()
        params = [p if isinstance(p, tuple) else (None, p) for p in params]
    else:
        raise ValueError('invalid params of type: %s' % type(params))

    # Run asynchronous broadcasts.
    for name, p in params:
        if p is not None:
            comm.bcast(p.view(-1), root_rank)
    comm.synchronize()


def broadcast_optimizer_state(optimizer, root_rank):
    """
    Broadcasts an optimizer state from root rank to all other processes.

    Arguments:
        optimizer: An optimizer.
        root_rank: The rank of the process from which the optimizer will be
                   broadcasted to all other processes.
    """
    if isinstance(optimizer, torch.optim.LBFGS):
        # TODO(travis): L-BFGS cannot be easily supported without serializing
        # the entire state_dict, as its structure is deeply nested and contains
        # None type parameter values
        raise ValueError('cannot broadcast torch.optim.LBFGS state')

    state_dict = optimizer.state_dict()

    # Newly created optimizers will not have their state initialized, so
    # do that initialization here
    if len(state_dict['state']) == 0:
        for group in optimizer.param_groups:
            for p in group['params']:
                p.grad = p.data.new(p.size()).zero_()
        # This function accepts a torch.optim.Optimizer or a DistributedOptimizer
        # wrapped around a torch optimizer. Calling step() with a DistributedOptimizer
        # forces allreduce on all model parameters, which will result in deadlock
        # unless every rank calls step(). Therefore, to finish state initialization
        # only call optimizer.step() with a torch.optim.Optimizer.
        if optimizer.__module__ == DistributedOptimizer.__module__:
            super(optimizer.__class__, optimizer).step()
        else:
            optimizer.step()
        state_dict = optimizer.state_dict()

    # If the state_dict is still empty after initialization, then
    # the optimizer is stateless, and there is nothing to broadcast.
    # Furthermore, attempting to access the state dict would result in
    # an error.
    if len(state_dict['state']) == 0:
        return

    params = []
    callbacks = {}
    occurrences = collections.defaultdict(int)

    # Returns the full type structure of the possibly nested objects for recursive casting back
    def _get_types(x):
        if isinstance(x, collections.Iterable):
            return type(x), [_get_types(xi) for xi in x]
        else:
            return type(x)

    # Casts an object encoded in a tensor back into its original type and subtypes
    def _recursive_cast(x, dtype):
        if isinstance(dtype, tuple):
            t, dtypes = dtype
            x = t(x)
            return t([_recursive_cast(x[i], dtypes[i]) for i in range(len(x))])
        else:
            return dtype(x)

    # Some optimizer parameters may be represented as scalars instead of
    # tensors.  In such cases, we need to wrap the scalar in a tensor, then
    # broadcast, then update the appropriate value in the state_dict with the
    # new unwrapped scalar value via a callback.
    def _create_callback(pid, name, t, p):
        def _from_tensor():
            state_dict['state'][pid][name] = t(p.numpy()[0])
        return _from_tensor

    def _create_option_callback(index, option_key, option_tensor, dtypes):
        def _from_tensor():
            optimizer.param_groups[index][option_key] = _recursive_cast(option_tensor.numpy()[0], dtypes)
        return _from_tensor

    # Param groups are an ordered list, normally there is only one per model,
    # but users can add additional param groups for example to train
    # previously frozen layers
    for index, group in enumerate(state_dict['param_groups']):
        # Broadcast options like learning rate
        for option_key, option_value in group.items():
            if option_key == 'params':
                continue

            # Options like the learning rate are scalar, and need to be wrapped in tensors
            key = '%s.%d' % (option_key, index)
            dtypes = _get_types(option_value)
            option_tensor = torch.Tensor([option_value])
            callbacks[key] = _create_option_callback(index, option_key, option_tensor, dtypes)
            params.append((key, option_tensor))

        # The params list here is ordered by the layers in the model
        for pid in group['params']:
            param_state = state_dict['state'][pid]
            for name, p in param_state.items():
                # Some parameter names may appear more than once, in which
                # case we ensure they have a unique identifier defined by
                # their order
                occurrences[name] += 1
                key = '%s.%d' % (str(name), occurrences[name])

                if p is not None and not torch.is_tensor(p):
                    # Wrap the scalar in a FloatTensor, and remember its type
                    # so we can cast it back after unwrapping
                    t = type(p)
                    p = torch.Tensor([p])
                    callbacks[key] = _create_callback(pid, name, t, p)

                params.append((key, p))

    # Synchronized broadcast of all parameters
    broadcast_parameters(params, root_rank)

    # Post-broadcast clenaup for non-tensor parameters
    for key, _ in params:
        if key in callbacks:
            callbacks[key]()

def allreduce(tensor, name=None):
    comm.allReduce(tensor.view(-1))
    comm.synchronize()
    return tensor/size()
