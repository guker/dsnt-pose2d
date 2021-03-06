#!/usr/bin/env python3

"""
This script will train a model on the MPII Human Pose dataset.

It is expected that the full dataset is available in `/datasets/mpii`.
"""

import argparse
import datetime
import json
import os

import numpy as np
import progressbar
import tele
import tele.meter
import torch
import torchnet.meter
from torch import optim
from torch.autograd import Variable
from torch.optim import lr_scheduler
from torch.utils.data import DataLoader
from torchvision.transforms import ToPILImage
from torchviz import make_dot

from dsnt.data import MPIIDataset
from dsnt.evaluator import PCKhEvaluator
from dsnt.hyperparam_scheduler import make_1cycle
from dsnt.model import build_mpii_pose_model
from dsnt.util import draw_skeleton, timer, generator_timer, seed_random_number_generators


def parse_args():
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(description='DSNT human pose model trainer')
    parser.add_argument('--epochs', type=int, default=120, metavar='N',
                        help='number of epochs to train (default=120)')
    parser.add_argument('--batch-size', type=int, default=32, metavar='N',
                        help='input batch size (default=32)')
    parser.add_argument('--showoff', type=str, default='showoff:3000', metavar='HOST:PORT',
                        help='network location of the Showoff server (default="showoff:3000")')
    parser.add_argument('--no-aug', action='store_true', default=False,
                        help='disable training data augmentation')
    parser.add_argument('--train-samples', type=int,
                        help='maximum number of samples to use from the training set')
    parser.add_argument('--out-dir', type=str, default='out', metavar='PATH',
                        help='path to output directory (default="out")')
    parser.add_argument('--base-model', type=str, default='resnet34', metavar='BM',
                        help='base model type (default="resnet34")')
    parser.add_argument('--dilate', type=int, default=0, metavar='N',
                        help='number of ResNet layer groups to dilate (default=0)')
    parser.add_argument('--truncate', type=int, default=0, metavar='N',
                        help='number of ResNet layer groups to cut off (default=0)')
    parser.add_argument('--output-strat', type=str, default='dsnt', metavar='S',
                        choices=['dsnt', 'gauss', 'fc'],
                        help='strategy for outputting coordinates (default="dsnt")')
    parser.add_argument('--preact', type=str, default='softmax', metavar='S',
                        choices=['softmax', 'thresholded_softmax', 'abs', 'relu', 'sigmoid'],
                        help='heatmap preactivation function (default="softmax")')
    parser.add_argument('--reg', type=str, default='none',
                        choices=['none', 'var', 'js', 'kl', 'mse'],
                        help='set the regularizer (default="none")')
    parser.add_argument('--reg-coeff', type=float, default=1.0,
                        help='coefficient for controlling regularization strength')
    parser.add_argument('--hm-sigma', type=float, default=1.0,
                        help='target standard deviation for heatmap, in pixels')
    parser.add_argument('--lr', type=float, metavar='LR',
                        help='initial learning rate')
    parser.add_argument('--schedule-milestones', type=int, nargs='+',
                        help='list of epochs at which to drop the learning rate')
    parser.add_argument('--schedule-gamma', type=float, metavar='G',
                        help='factor to multiply the LR by at each drop')
    parser.add_argument('--optim', type=str, default='rmsprop', metavar='S',
                        choices=['sgd', 'rmsprop', '1cycle'],
                        help='optimizer to use (default=rmsprop)')
    parser.add_argument('--tags', type=str, nargs='+', default=[],
                        help='keywords to tag this experiment with')
    parser.add_argument('--seed', type=int, metavar='N',
                        help='seed for random number generators')

    args = parser.parse_args()

    if args.seed is None:
        args.seed = np.random.randint(0, 999999)

    if args.optim == 'sgd':
        args.lr = args.lr or 0.2
        args.schedule_gamma = args.schedule_gamma or 0.5
        args.schedule_milestones = args.schedule_milestones or [20, 40, 60, 80, 120, 140, 160, 180]
    elif args.optim == 'rmsprop':
        args.lr = args.lr or 2.5e-4
        args.schedule_gamma = args.schedule_gamma or 0.1
        args.schedule_milestones = args.schedule_milestones or [60, 90]
    elif args.optim == '1cycle':
        args.lr = args.lr or 1
        args.schedule_gamma = None
        args.schedule_milestones = None

    return args


class Reporting:
    """Helper class for setting up metric reporting outputs."""

    def __init__(self, train_eval, val_eval):
        self.telemetry = tele.Telemetry({
            'experiment_id': tele.meter.ValueMeter(skip_reset=True),
            'epoch': tele.meter.ValueMeter(),
            'train_loss': torchnet.meter.AverageValueMeter(),
            'val_loss': torchnet.meter.AverageValueMeter(),
            'epoch_time': torchnet.meter.TimeMeter(unit=False),
            'train_data_load_time': torchnet.meter.AverageValueMeter(),
            'train_data_transfer_time': torchnet.meter.AverageValueMeter(),
            'train_forward_time': torchnet.meter.AverageValueMeter(),
            'train_criterion_time': torchnet.meter.AverageValueMeter(),
            'train_backward_time': torchnet.meter.AverageValueMeter(),
            'train_optim_time': torchnet.meter.AverageValueMeter(),
            'train_eval_time': torchnet.meter.AverageValueMeter(),
            'train_sample': tele.meter.ValueMeter(),
            'val_sample': tele.meter.ValueMeter(),
            'train_heatmaps': tele.meter.ValueMeter(),
            'val_heatmaps': tele.meter.ValueMeter(),
            'args': tele.meter.ValueMeter(skip_reset=True),
            'train_pckh_total': train_eval.meters['total_mpii'],
            'val_pckh_total': val_eval.meters['total_mpii'],
            'train_pckh_all': train_eval.meters['all'],
            'val_pckh_all': val_eval.meters['all'],
            'val_preds': tele.meter.ValueMeter(),
            'best_val_preds': tele.meter.ValueMeter(skip_reset=True),
            'model_graph': tele.meter.ValueMeter(skip_reset=True),
        })

    def setup_console_output(self):
        """Setup stdout reporting output."""

        from tele.console import views
        meters_to_print = [
            'train_loss', 'val_loss', 'train_pckh_total', 'val_pckh_total', 'epoch_time'
        ]
        self.telemetry.sink(tele.console.Conf(), [
            views.KeyValue([mn]) for mn in meters_to_print
        ])

    def setup_folder_output(self, out_dir):
        """Setup file system reporting output."""

        from tele.folder import views

        self.telemetry.sink(tele.folder.Conf(out_dir), [
            views.GrowingJSON(['epoch', 'train_loss', 'val_loss', 'epoch_time',
                'train_pckh_total', 'val_pckh_total'], 'saved_metrics.json'),
            views.HDF5(['val_preds'], 'val_preds.h5', {'val_preds': 'preds'}),
            views.HDF5(['best_val_preds'], 'val_preds-best.h5', {'best_val_preds': 'preds'}),
        ])

    def setup_showoff_output(self, notebook):
        """Setup Showoff reporting output."""

        from tele.showoff import views

        self.telemetry.sink(tele.showoff.Conf(notebook), [
            views.LineGraph(['train_loss', 'val_loss'], 'Loss'),
            views.LineGraph(['train_pckh_all', 'val_pckh_all'], 'PCKh all'),
            views.LineGraph(['train_pckh_total', 'val_pckh_total'], 'PCKh total'),
            views.Inspect(['experiment_id', 'epoch', 'train_loss', 'val_loss',
                'train_pckh_total', 'val_pckh_total', 'train_pckh_all', 'val_pckh_all'], 'Inspect'),
            views.LineGraph(['epoch_time'], 'Time'),
            views.Inspect(['args'], 'Command-line arguments', flatten=True),
            views.Images(['train_sample'], 'Training samples', images_per_row=2),
            views.Images(['val_sample'], 'Validation samples', images_per_row=2),
            views.Graphviz(['model_graph'], 'Model graph'),
            views.LineGraph(['train_data_load_time', 'train_data_transfer_time',
                'train_forward_time', 'train_criterion_time',
                'train_backward_time', 'train_optim_time', 'train_eval_time'],
                'Training time breakdown'),
            views.Images(['train_heatmaps'], 'Training wrist heatmaps', images_per_row=2),
            views.Images(['val_heatmaps'], 'Validation wrist heatmaps', images_per_row=2),
        ])


def main():
    """Main training entrypoint function."""

    args = parse_args()
    seed_random_number_generators(args.seed)

    epochs = args.epochs
    batch_size = args.batch_size
    use_train_aug = not args.no_aug
    out_dir = args.out_dir
    base_model = args.base_model
    dilate = args.dilate
    truncate = args.truncate
    initial_lr = args.lr
    schedule_milestones = args.schedule_milestones
    schedule_gamma = args.schedule_gamma

    experiment_id = datetime.datetime.now().strftime('%Y%m%d-%H%M%S%f')
    exp_out_dir = os.path.join(out_dir, experiment_id) if out_dir else None

    print('Experiment ID: {}'.format(experiment_id))

    ####
    # Model
    ####

    model_desc = {
        'base': base_model,
        'dilate': dilate,
        'truncate': truncate,
        'output_strat': args.output_strat,
        'preact': args.preact,
        'reg': args.reg,
        'reg_coeff': args.reg_coeff,
        'hm_sigma': args.hm_sigma,
    }
    model = build_mpii_pose_model(**model_desc)
    model.cuda()

    input_size = model.image_specs.size

    ####
    # Data
    ####

    train_data = MPIIDataset('/datasets/mpii', 'train',
        use_aug=use_train_aug, image_specs=model.image_specs, max_length=args.train_samples)
    train_loader = DataLoader(train_data, batch_size, num_workers=4, pin_memory=True, shuffle=True)

    val_data = MPIIDataset('/datasets/mpii', 'val',
        use_aug=False, image_specs=model.image_specs)
    val_loader = DataLoader(val_data, batch_size, num_workers=4, pin_memory=True)

    ####
    # Metrics and visualisation
    ####

    train_eval = PCKhEvaluator()
    val_eval = PCKhEvaluator()

    def eval_metrics_for_batch(evaluator, batch, norm_out):
        """Evaluate and accumulate performance metrics for batch."""

        norm_out = norm_out.type(torch.DoubleTensor)

        # Coords in original MPII dataset space
        orig_out = torch.bmm(norm_out, batch['transform_m']).add_(
            batch['transform_b'].expand_as(norm_out))

        norm_target = batch['part_coords'].double()
        orig_target = torch.bmm(norm_target, batch['transform_m']).add_(
            batch['transform_b'].expand_as(norm_target))

        head_lengths = batch['normalize'].double()

        evaluator.add(orig_out, orig_target, batch['part_mask'], head_lengths)

    reporting = Reporting(train_eval, val_eval)
    tel = reporting.telemetry

    reporting.setup_console_output()

    if exp_out_dir:
        reporting.setup_folder_output(exp_out_dir)

        with open(os.path.join(exp_out_dir, 'cli_args.json'), 'w') as f:
            json.dump(vars(args), f, sort_keys=True, indent=2)

    if args.showoff:
        import pyshowoff

        with open('/etc/hostname', 'r') as f:
            hostname = f.read().strip()

        client = pyshowoff.Client('http://' + args.showoff)
        notebook = client.add_notebook(
            '[{}] Human pose ({}-d{}-t{}, {}, {}@{:.1e}, reg={})'.format(
                hostname, base_model, dilate, truncate, args.output_strat, args.optim, args.lr,
                args.reg)
        ).result()

        for tag_name in args.tags:
            notebook.add_tag(tag_name)

        reporting.setup_showoff_output(notebook)

        progress_frame = notebook.add_frame('Progress',
            bounds={'x': 0, 'y': 924, 'width': 1920, 'height': 64}
        ).result()
    else:
        progress_frame = None

    # Set constant values
    tel['experiment_id'].set_value(experiment_id)
    tel['args'].set_value(vars(args))

    # Generate a Graphviz graph to visualise the model
    dummy_data = torch.cuda.FloatTensor(1, 3, input_size, input_size).uniform_(0, 1)
    out_var = model(Variable(dummy_data, requires_grad=False))
    if isinstance(out_var, list):
        out_var = out_var[-1]
    tel['model_graph'].set_value(make_dot(out_var, dict(model.named_parameters())))
    del dummy_data

    best_val_acc_meter = tele.meter.MaxValueMeter(skip_reset=True)

    ####
    # Optimiser
    ####

    # Initialize optimiser and learning rate scheduler
    if args.optim == '1cycle':
        optimizer = optim.SGD(model.parameters(), lr=0)
        scheduler = make_1cycle(optimizer, epochs * len(train_loader), lr_max=initial_lr, momentum=0.9)
    else:
        if args.optim == 'sgd':
            optimizer = optim.SGD(model.parameters(), lr=initial_lr, momentum=0.9)
        elif args.optim == 'rmsprop':
            optimizer = optim.RMSprop(model.parameters(), lr=initial_lr)
        else:
            raise Exception('unrecognised optimizer: {}'.format(args.optim))

        scheduler = lr_scheduler.MultiStepLR(
            optimizer, milestones=schedule_milestones, gamma=schedule_gamma)

    # `vis` will hold a few samples for visualisation
    vis = {}

    ####
    # Training
    ####

    def train(epoch):
        """Do a full pass over the training set, updating model parameters."""

        if hasattr(scheduler, 'step'):
            scheduler.step(epoch)

        model.train()
        samples_processed = 0

        with progressbar.ProgressBar(max_value=len(train_data)) as bar:
            for i, batch in generator_timer(enumerate(train_loader), tel['train_data_load_time']):
                if hasattr(scheduler, 'batch_step'):
                    scheduler.batch_step()

                with timer(tel['train_data_transfer_time']):
                    in_var = Variable(batch['input'].cuda(), requires_grad=False)
                    target_var = Variable(batch['part_coords'].cuda(), requires_grad=False)
                    mask_var = Variable(batch['part_mask'].type(torch.cuda.FloatTensor), requires_grad=False)

                with timer(tel['train_forward_time']):
                    out_var = model(in_var)

                with timer(tel['train_criterion_time']):
                    loss = model.forward_loss(out_var, target_var, mask_var)

                    if np.isnan(loss.data[0]):
                        state = {
                            'state_dict': model.state_dict(),
                            'model_desc': model_desc,
                            'optimizer': optimizer.state_dict(),
                            'epoch': epoch,
                            'input': in_var.data,
                            'target': target_var.data,
                            'mask': mask_var.data,
                        }
                        torch.save(state, 'model_dump.pth')
                        raise Exception('training loss should not be nan')

                    tel['train_loss'].add(loss.data[0])

                with timer(tel['train_eval_time']):
                    coords = model.compute_coords(out_var)
                    eval_metrics_for_batch(train_eval, batch, coords)

                with timer(tel['train_backward_time']):
                    optimizer.zero_grad()
                    loss.backward()

                with timer(tel['train_optim_time']):
                    optimizer.step()

                samples_processed += batch['input'].size(0)
                bar.update(samples_processed)

                if i == 0:
                    vis['train_images'] = batch['input']
                    vis['train_preds'] = coords
                    vis['train_masks'] = batch['part_mask']
                    vis['train_coords'] = batch['part_coords']
                    vis['train_heatmaps'] = model.heatmaps.data.cpu()

                if progress_frame is not None:
                    so_far = epoch * len(train_data) + samples_processed
                    total = epochs * len(train_data)
                    notebook.set_progress(so_far / total)
                    progress_frame.progress(so_far, total)

    def validate(epoch):
        '''Do a full pass over the validation set, evaluating model performance.'''

        model.eval()
        val_preds = torch.DoubleTensor(len(val_data), 16, 2)
        samples_processed = 0

        with progressbar.ProgressBar(max_value=len(val_data)) as bar:
            for i, batch in enumerate(val_loader):
                in_var = Variable(batch['input'].cuda(), volatile=True)
                target_var = Variable(batch['part_coords'].cuda(), volatile=True)
                mask_var = Variable(batch['part_mask'].type(torch.cuda.FloatTensor), volatile=True)

                out_var = model(in_var)
                loss = model.forward_loss(out_var, target_var, mask_var)
                tel['val_loss'].add(loss.data[0])
                coords = model.compute_coords(out_var)
                eval_metrics_for_batch(val_eval, batch, coords)

                preds = coords.double()
                pos = i * batch_size
                orig_preds = val_preds[pos:(pos + preds.size(0))]
                torch.baddbmm(
                    batch['transform_b'],
                    preds,
                    batch['transform_m'],
                    out=orig_preds)

                samples_processed += batch['input'].size(0)
                bar.update(samples_processed)

                if i == 0:
                    vis['val_images'] = batch['input']
                    vis['val_preds'] = coords
                    vis['val_masks'] = batch['part_mask']
                    vis['val_coords'] = batch['part_coords']
                    vis['val_heatmaps'] = model.heatmaps.data.cpu()

            tel['val_preds'].set_value(val_preds.numpy())

    print('Entering the main training loop')

    for epoch in range(epochs):
        print('> Epoch {:3d}/{:3d}'.format(epoch + 1, epochs))

        tel['epoch'].set_value(epoch)
        tel['epoch_time'].reset()

        print('Training pass...')
        train(epoch)
        print('Validation pass...')
        validate(epoch)

        train_sample = []
        for i in range(min(16, vis['train_images'].size(0))):
            img = model.image_specs.unconvert(vis['train_images'][i], train_data)
            coords = (vis['train_preds'][i] + 1) * (input_size / 2)
            draw_skeleton(img, coords, vis['train_masks'][i])
            train_sample.append(img)
        tel['train_sample'].set_value(train_sample)

        val_sample = []
        for i in range(min(16, vis['val_images'].size(0))):
            img = model.image_specs.unconvert(vis['val_images'][i], val_data)
            coords = (vis['val_preds'][i] + 1) * (input_size / 2)
            draw_skeleton(img, coords, vis['val_masks'][i])
            val_sample.append(img)
        tel['val_sample'].set_value(val_sample)

        def visualise_heatmaps(key):
            heatmap_images = []
            for i in range(min(16, vis[key].size(0))):
                lwrist_hm = vis[key][i, PCKhEvaluator.JOINT_NAMES.index('lwrist')]
                rwrist_hm = vis[key][i, PCKhEvaluator.JOINT_NAMES.index('rwrist')]
                lwrist_hm = (lwrist_hm / lwrist_hm.max()).clamp_(0, 1)
                rwrist_hm = (rwrist_hm / rwrist_hm.max()).clamp_(0, 1)
                img = ToPILImage()(torch.stack([rwrist_hm, lwrist_hm.clone().zero_(), lwrist_hm], 0))
                heatmap_images.append(img)
            tel[key].set_value(heatmap_images)

        visualise_heatmaps('train_heatmaps')
        visualise_heatmaps('val_heatmaps')

        val_acc = val_eval.meters['total_mpii'].value()[0]
        is_best = best_val_acc_meter.add(val_acc)

        if exp_out_dir:
            state = {
                'state_dict': model.state_dict(),
                'model_desc': model_desc,
                'optimizer': optimizer.state_dict(),
                'epoch': epoch + 1,
                'val_acc': val_acc,
            }
            torch.save(state, os.path.join(exp_out_dir, 'model.pth'))

            if is_best:
                torch.save(state, os.path.join(exp_out_dir, 'model-best.pth'))
                tel['best_val_preds'].set_value(tel['val_preds'].value())

        tel.step()
        train_eval.reset()
        val_eval.reset()
        print()


if __name__ == '__main__':
    main()
