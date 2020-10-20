"""Main module for training entity model."""

import argparse
import bisect
import datetime
import functools
import json
import os
import time

# numpy has come after pytorch due to MKL threading setup
import numpy as np
import torch
import torch.utils.data
import torch.utils.tensorboard

from sketchgraphs_models import distributed_utils, training
from sketchgraphs_models.graph import model as graph_model
from sketchgraphs_models.nn.distributed import SingleDeviceDistributedParallel
from .data_loading import initialize_datasets
from .harness import GraphModelHarness

_opt_factories = {
    'sgd': torch.optim.SGD,
    'adam': torch.optim.Adam,
    'adamax': torch.optim.Adamax,
    'rms': torch.optim.RMSprop
}


def _lr_schedule(epoch, warmup_epochs=5, decay_epochs=None):
    if decay_epochs is None:
        decay_epochs = []

    warmup_factor = min((epoch + 1) / warmup_epochs, 1)
    decay_factor = 0.1 ** (bisect.bisect_right(decay_epochs, epoch))

    return warmup_factor * decay_factor


def make_model_with_arguments(feature_dimensions, args):
    return graph_model.make_graph_model(
        args['hidden_size'], feature_dimensions,
        readout_entity_features=not args.get('disable_entity_features', False) or args.get(
            'force_entity_categorical_features', False),
        readout_edge_features=not args.get('disable_edge_features', False),
        readin_entity_features=False if args.get('disable_readin_entity', False) else None,
        readin_edge_features=False if args.get('disable_readin_edge', False) else None)


def _feature_dimension(mapping):
    if mapping is None:
        return {}

    return mapping.feature_dimensions


def _state_dict(mapping):
    if mapping is None:
        return None

    return mapping.state_dict()


def train(node_feature_mapping, edge_feature_mapping, dataloader_train, args, output_dir=None, dataloader_eval=None,
          batches_per_epoch=None, dist_config=None):
    print('Building model.')
    feature_dimensions = {**_feature_dimension(node_feature_mapping), **_feature_dimension(edge_feature_mapping)}
    model = make_model_with_arguments(feature_dimensions, args)
    num_params = sum([p.numel() for p in model.parameters()])
    print(f'model has: {num_params / 1e6:.4f} million parameters')

    if args['model_state']:
        state = torch.load(args['model_state'], map_location=torch.device('cpu'))

        # Remove "module." from beginning of keys
        new_state_dict = {}
        for key in state['model']:
            new_state_dict[key[7:]] = state['model'][key]
        state['model'] = new_state_dict

        model.load_state_dict(state['model'])
        epoch = state['epoch']
        global_step = state['global_step']
    else:
        epoch = 0
        global_step = 0

    if dist_config:
        gpu_id = dist_config.local_rank
        print('Creating model on GPU {0}'.format(gpu_id))
        device = torch.device('cuda', gpu_id)
        # Create parallel device. Note that we need to use find_unused_parameters, as due to the dynamic
        # nature of our computation graph, depending on the available targets in the dataset, not all
        # parameters will have gradients computed for them.
        model = SingleDeviceDistributedParallel(model.to(device), gpu_id, find_unused_parameters=True)
    else:
        device = torch.device('cuda')
        model.to(device)  # Set model device

    print('Model done building.')

    total_batch_size = args['batch_size']
    if dist_config:
        batch_size = total_batch_size // dist_config.world_size
    else:
        batch_size = total_batch_size

    opt = _opt_factories[args['optimizer']](model.parameters(), lr=args['learning_rate'] * 16)
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        opt, functools.partial(_lr_schedule, warmup_epochs=5, decay_epochs=[20, 40]))

    if distributed_utils.is_leader(dist_config):
        tb_writer_main = torch.utils.tensorboard.SummaryWriter(output_dir)
        tb_writer_eval = torch.utils.tensorboard.SummaryWriter(output_dir + '/eval/')
    else:
        tb_writer_main, tb_writer_eval = None, None

    harness = GraphModelHarness(
        model, opt,
        _feature_dimension(node_feature_mapping),
        _feature_dimension(edge_feature_mapping),
        training.TrainingConfig(
            dataloader_train,
            tb_writer_main,
            device,
            batch_size,
            batches_per_epoch),
        training.TrainingConfig(
            dataloader_eval,
            tb_writer_eval,
            device,
            batch_size)
        if dataloader_eval is not None
        else None,
        scheduler=scheduler,
        output_dir=output_dir,
        dist_config=dist_config,
        profile_enabled=args['profile'],
        additional_model_information={
            'node_feature_mapping': _state_dict(node_feature_mapping),
            'edge_feature_mapping': _state_dict(edge_feature_mapping),
            'model_configuration': {
                'embedding_dim': args['hidden_size'],
                'depth': args['num_prop_rounds'],
                'name': 'graph',
            }
        })

    while epoch < args['num_epochs']:
        epoch, global_step = harness.train_epochs(epoch, global_step)

    return model


def get_argsparser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--description', default=None,
                        help='Message describing the current run.')
    parser.add_argument('--output_dir', default='../output',
                        help='Directory for output files.')

    parser.add_argument('--dataset_train', required=True,
                        help='Dataset for training data.')
    parser.add_argument('--dataset_auxiliary', default=None, help='Path to auxiliary dataset containing metadata')
    parser.add_argument('--dataset_test', required=False, default=None,
                        help='Dataset for validation data.')
    parser.add_argument('--model_state', default=None, help='Path to saved model state_dict.')
    parser.add_argument('--num_quantize_length', type=int, default=383, help='number of quantization values for length')
    parser.add_argument('--num_quantize_angle', type=int, default=127, help='number of quantization values for angle')
    parser.add_argument('--batch_size', type=int, default=2048,
                        help='Training batch size.')
    parser.add_argument('--learning_rate', type=float, default=2e-5)
    parser.add_argument('--optimizer', default='adam', choices=list(_opt_factories.keys()))
    parser.add_argument('--hidden_size', type=int, default=384)
    parser.add_argument('--num_prop_rounds', type=int, default=3)
    parser.add_argument('--num_epochs', type=int, default=60,
                        help='Number of training epochs.')
    parser.add_argument('--num_workers', type=int, default=0,
                        help='Number of dataloader workers.')
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--world_size', type=int, default=1, help='Number of GPUs to use.')
    parser.add_argument('--profile', action='store_true', help='Whether to produce autograd profiles')

    parser.add_argument('--disable_entity_features', action='store_true',
                        help='Disable using and predicting entity features')
    parser.add_argument('--disable_edge_features', action='store_true',
                        help='Disable using and predicting edge features')

    parser.add_argument('--disable_readin_entity', action='store_true',
                        help='Disable reading in entity features')
    parser.add_argument('--disable_readin_edge', action='store_true',
                        help='Disable reading in edge features')
    parser.add_argument('--force_entity_categorical_features', action='store_true')

    return parser


# These keys are converted to absolute paths on save
_ARGS_PATH_KEYS = (
    'output_dir',
    'dataset_train',
    'dataset_auxiliary',
    'dataset_test',
    'model_state'
)


def run(args, distributed_config=None):
    """Runs the entire training process according to the given configuration.
    """
    # Set seeds
    np.random.seed(args['seed'])
    torch.manual_seed(args['seed'])

    for key in _ARGS_PATH_KEYS:
        if args[key] is not None:
            args[key] = os.path.abspath(args[key])

    print('Loading datasets')
    dl_train, dl_test, batches_per_epoch, node_feature_mapping, edge_feature_mapping = initialize_datasets(
        args, distributed_config)
    print('Data loaded. Creating output folder.')

    # Derive save_dir
    if distributed_utils.is_leader(distributed_config):
        output_dir = '{}/{}/time_{}'.format(args['output_dir'],
                                            time.strftime('%m%d'),
                                            time.strftime('%H%M%S'))

        os.makedirs(output_dir)

        with open(os.path.join(output_dir, 'args.txt'), 'w') as file_:
            json.dump(args, file_, indent=4)
    else:
        output_dir = None

    print('Starting training.')
    start_time = time.perf_counter()
    _ = train(
        node_feature_mapping,
        edge_feature_mapping,
        dl_train, args,
        output_dir=output_dir, dataloader_eval=dl_test, batches_per_epoch=batches_per_epoch,
        dist_config=distributed_config)
    end_time = time.perf_counter()
    print(f'Done training. Total time: {datetime.timedelta(seconds=end_time - start_time)}.')


def main():
    """Default main function."""
    parser = get_argsparser()
    args = parser.parse_args()

    if args.world_size > 1:
        distributed_utils.train_boostrap_distributed(vars(args), run)
    else:
        run(vars(args))
