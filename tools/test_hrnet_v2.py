import _init_paths
import argparse
import os
import pprint

import logging
import timeit

import numpy as np

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.optim
from tensorboardX import SummaryWriter

from networks import hrnet_v2 as models

from config import config_hrnet_v2 as config
from config import update_config_hrnet_v2 as update_config
from core.criterion import CrossEntropy, OhemCrossEntropy
from core.function_2 import validate
from utils.hrnet_v2_utils.utils import create_logger, FullModel
from utils.hrnet_utils.normalization_utils import get_imagenet_mean_std
from semantic_dataloader import UWFSDataLoader
from semantic_dataloader_3 import UWFSDataLoader as UWFSDataLoader2
from utils.hrnet_utils import transform
from tqdm import tqdm
from scipy.io import loadmat
from sklearn.model_selection import StratifiedShuffleSplit


def parse_args():
    parser = argparse.ArgumentParser(description='Train segmentation network')

    parser.add_argument('--cfg',
                        help='experiment configure file name',
                        required=True,
                        type=str)
    parser.add_argument('--seed', type=int, default=304)
    parser.add_argument('opts',
                        help="Modify config options using the command-line",
                        default=None,
                        nargs=argparse.REMAINDER)

    args = parser.parse_args()
    update_config(config, args)

    return args


def main():
    args = parse_args()

    if args.seed > 0:
        import random
        print('Seeding with', args.seed)
        random.seed(args.seed)
        torch.manual_seed(args.seed)

    logger, final_output_dir, tb_log_dir = create_logger(
        config, args.cfg, 'train')

    logger.info(pprint.pformat(args))
    logger.info(config)

    writer_dict = {
        'writer': SummaryWriter(tb_log_dir),
        'train_global_steps': 0,
        'valid_global_steps': 0,
    }

    # cudnn related setting
    cudnn.benchmark = config.CUDNN.BENCHMARK
    cudnn.deterministic = config.CUDNN.DETERMINISTIC
    cudnn.enabled = config.CUDNN.ENABLED
    gpus = list(config.GPUS)

    model = eval('models.' + config.MODEL.NAME +
                 '.get_seg_model')(config)

    batch_size = config.TRAIN.BATCH_SIZE_PER_GPU

    # prepare data
    mean, std = get_imagenet_mean_std()
    
    if config.DATASET.DATASET == 'UWS':
        # transform.ResizeTest((config.TRAIN.TRAIN_H, config.TRAIN.TRAIN_W)),
        # transform.ResizeShort(config.TRAIN.SHORT_SIZE),
        # transform.RandScale([config.TRAIN.SCALE_MIN, config.TRAIN.SCALE_MAX]),
        # transform.RandRotate(
        #     [config.TRAIN.ROTATE_MIN, config.TRAIN.ROTATE_MAX],
        #     padding=mean,
        #     ignore_label=config.TRAIN.IGNORE_LABEL,
        # ),
        # transform.RandomGaussianBlur(),
        # transform.RandomHorizontalFlip(),

        train_transform_list = [
            transform.ResizeShort(config.TRAIN.IMAGE_SIZE[0]),
            transform.RandScale([0.5, 1.0]),
            transform.RandRotate(
                [-10, 10],
                padding=mean,
                ignore_label=config.TRAIN.IGNORE_LABEL,
            ),
            transform.RandomHorizontalFlip(),
            transform.Crop(
                [config.TRAIN.IMAGE_SIZE[0], config.TRAIN.IMAGE_SIZE[1]],
                crop_type="rand",
                padding=mean,
                ignore_label=config.TRAIN.IGNORE_LABEL,
            ),
            transform.ToTensor(),
            transform.Normalize(mean=mean, std=std),
        ]
        # transform.ResizeTest((config.TRAIN.IMAGE_SIZE[0], config.TRAIN.IMAGE_SIZE[1])),
        # transform.ResizeShort(config.TRAIN.IMAGE_SIZE[0]),
        val_transform_list = [
            transform.ResizeShort(config.TRAIN.IMAGE_SIZE[0]),
            transform.Crop(
                [config.TRAIN.IMAGE_SIZE[0], config.TRAIN.IMAGE_SIZE[1]],
                crop_type="center",
                padding=mean,
                ignore_label=config.TRAIN.IGNORE_LABEL,
            ),
            transform.ToTensor(),
            transform.Normalize(mean=mean, std=std),
        ]
        train_indices_file = config.DATASET.TRAIN_SET
        val_indices_file = config.DATASET.TEST_SET

        with open(train_indices_file, 'r') as f:
            train_indices = f.read().strip().split('\n')

        with open(val_indices_file, 'r') as f:
            val_indices = f.read().strip().split('\n')

        train_dataset = UWFSDataLoader(
            output_image_height=config.TRAIN.IMAGE_SIZE[0],
            dataset_root=config.DATASET.ROOT,
            image_format='mat',
            indices=train_indices,
            channel_values=None,
            normalizer=transform.Compose(train_transform_list)
        )
        val_dataset = UWFSDataLoader(
            output_image_height=config.TRAIN.IMAGE_SIZE[0],
            dataset_root=config.DATASET.ROOT,
            image_format='mat',
            indices=val_indices,
            channel_values=None,
            normalizer=transform.Compose(val_transform_list)
        )
    elif config.DATASET.DATASET == 'UWS2':
        train_transform_list = [
            transform.ResizeShort(config.TRAIN.IMAGE_SIZE[0]),
            transform.Crop(
                [config.TRAIN.IMAGE_SIZE[0], config.TRAIN.IMAGE_SIZE[1]],
                crop_type="rand",
                padding=mean,
                ignore_label=config.TRAIN.IGNORE_LABEL,
            ),
            transform.ToTensor(),
            transform.Normalize(mean=mean, std=std),
        ]
        # transform.ResizeTest((config.TRAIN.TRAIN_H, config.TRAIN.TRAIN_W)),
        # transform.ResizeShort(config.TRAIN.SHORT_SIZE),
        val_transform_list = [
            transform.ResizeShort(config.TRAIN.IMAGE_SIZE[0]),
            transform.Crop(
                [config.TRAIN.IMAGE_SIZE[0], config.TRAIN.IMAGE_SIZE[1]],
                crop_type="center",
                padding=mean,
                ignore_label=config.TRAIN.IGNORE_LABEL,
            ),
            transform.ToTensor(),
            transform.Normalize(mean=mean, std=std),
        ]
        images = []
        masks = []
        im_classes = []
        classes_fol = os.listdir(config.DATASET.ROOT)
        if '.DS_Store' in classes_fol:
            classes_fol.remove('.DS_Store')
        for cls_fol in tqdm(classes_fol):
            files = os.listdir(os.path.join(config.DATASET.ROOT, cls_fol))
            for fl in files:
                if not fl.endswith('.mat'):
                    continue
                mat = loadmat(os.path.join(config.DATASET.ROOT, cls_fol, fl))
                images.append(np.asarray(mat["image_array"]))
                im_classes.append(mat["class"])
                masks.append(np.asarray(mat["mask_array"]))

        dataset_len = len(images)
        logger.info(f'Total mat files: {dataset_len}')
        np.random.seed(0)
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=200))
        for i in rand_n:
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)
            # perform horizontal flip
            images.append(np.fliplr(im))
            masks.append(np.fliplr(target))

        # shift right
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=100))
        for i in rand_n:
            shift = 20
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)

            im[:, shift:] = im[:, :-shift]
            target[:, shift:] = target[:, :-shift]
            images.append(im)
            masks.append(target)

        # shift left
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=100))
        for i in rand_n:
            shift = 20
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)

            im[:, :-shift] = im[:, shift:]
            target[:, :-shift] = target[:, shift:]
            images.append(im)
            masks.append(target)

        # shift up
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=100))
        for i in rand_n:
            shift = 20
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)

            im[:-shift, :] = im[shift:, :]
            target[:-shift, :] = target[shift:, :]
            images.append(im)
            masks.append(target)

        # shift down
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=100))
        for i in rand_n:
            shift = 20
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)

            im[shift:, :] = im[:-shift, :]
            target[shift:, :] = target[:-shift, :]
            images.append(im)
            masks.append(target)

        split = StratifiedShuffleSplit(n_splits=1, test_size=0.1)
        images_train = []
        # classes_train = []
        masks_train = []
        images_test = []
        # classes_test = []
        masks_test = []
        for train_index, test_index in split.split(images, im_classes):
            images_train = [images[i] for i in train_index]
            # classes_train = [im_classes[i] for i in train_index]
            masks_train = [masks[i] for i in train_index]
            images_test = [images[i] for i in test_index]
            # classes_test = [im_classes[i] for i in test_index]
            masks_test = [masks[i] for i in test_index]

        train_dataset = UWFSDataLoader2(
            output_image_height=config.TRAIN.IMAGE_SIZE[0],
            images=images_train,
            masks=masks_train,
            normalizer=transform.Compose(train_transform_list),
            channel_values=None
        )
        val_dataset = UWFSDataLoader2(
            output_image_height=config.TRAIN.IMAGE_SIZE[0],
            images=images_test,
            masks=masks_test,
            normalizer=transform.Compose(val_transform_list),
            channel_values=None
        )
    elif config.DATASET.DATASET == 'UWS3':
        train_transform_list = [
            transform.ResizeShort(config.TRAIN.IMAGE_SIZE[0]),
            transform.Crop(
                [config.TRAIN.IMAGE_SIZE[0], config.TRAIN.IMAGE_SIZE[1]],
                crop_type="rand",
                padding=mean,
                ignore_label=config.TRAIN.IGNORE_LABEL,
            ),
            transform.ToTensor(),
            transform.Normalize(mean=mean, std=std),
        ]
        # transform.ResizeTest((config.TRAIN.TRAIN_H, config.TRAIN.TRAIN_W)),
        # transform.ResizeShort(config.TRAIN.SHORT_SIZE),
        val_transform_list = [
            transform.ResizeShort(config.TRAIN.IMAGE_SIZE[0]),
            transform.Crop(
                [config.TRAIN.IMAGE_SIZE[0], config.TRAIN.IMAGE_SIZE[1]],
                crop_type="center",
                padding=mean,
                ignore_label=config.TRAIN.IGNORE_LABEL,
            ),
            transform.ToTensor(),
            transform.Normalize(mean=mean, std=std),
        ]
        
        train_indices_file = config.DATASET.TRAIN_SET
        val_indices_file = config.DATASET.TEST_SET
        
        with open(train_indices_file, 'r') as f:
            train_indices = f.read().strip().split('\n')
        
        with open(val_indices_file, 'r') as f:
            val_indices = f.read().strip().split('\n')
        
        images = []
        masks = []
        im_classes = []
        
        for cls_fol in tqdm(train_indices):
            mat = loadmat(os.path.join(config.DATASET.ROOT, cls_fol))
            images.append(np.asarray(mat["image_array"]))
            im_classes.append(mat["class"])
            masks.append(np.asarray(mat["mask_array"]))
        
        dataset_len = len(images)
        logger.info(f'Total train mat files: {dataset_len}')
        np.random.seed(0)
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=dataset_len//2))
        for i in rand_n:
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)
            # perform horizontal flip
            images.append(np.fliplr(im))
            masks.append(np.fliplr(target))
        
        # shift right
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=dataset_len//4))
        for i in rand_n:
            shift = 20
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)
        
            im[:, shift:] = im[:, :-shift]
            target[:, shift:] = target[:, :-shift]
            images.append(im)
            masks.append(target)
        
        # shift left
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=dataset_len//4))
        for i in rand_n:
            shift = 20
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)
        
            im[:, :-shift] = im[:, shift:]
            target[:, :-shift] = target[:, shift:]
            images.append(im)
            masks.append(target)
        
        # shift up
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=dataset_len//4))
        for i in rand_n:
            shift = 20
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)
        
            im[:-shift, :] = im[shift:, :]
            target[:-shift, :] = target[shift:, :]
            images.append(im)
            masks.append(target)
        
        # shift down
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=dataset_len//4))
        for i in rand_n:
            shift = 20
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)
        
            im[shift:, :] = im[:-shift, :]
            target[shift:, :] = target[:-shift, :]
            images.append(im)
            masks.append(target)
        
        images_train = images
        masks_train = masks
        
        images = []
        masks = []
        im_classes = []
        
        for cls_fol in tqdm(val_indices):
            mat = loadmat(os.path.join(config.DATASET.ROOT, cls_fol))
            images.append(np.asarray(mat["image_array"]))
            im_classes.append(mat["class"])
            masks.append(np.asarray(mat["mask_array"]))
        
        dataset_len = len(images)
        logger.info(f'Total val mat files: {dataset_len}')
        np.random.seed(0)
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=dataset_len//2))
        for i in rand_n:
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)
            # perform horizontal flip
            images.append(np.fliplr(im))
            masks.append(np.fliplr(target))
        
        # shift right
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=dataset_len//4))
        for i in rand_n:
            shift = 20
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)
        
            im[:, shift:] = im[:, :-shift]
            target[:, shift:] = target[:, :-shift]
            images.append(im)
            masks.append(target)
        
        # shift left
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=dataset_len//4))
        for i in rand_n:
            shift = 20
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)
        
            im[:, :-shift] = im[:, shift:]
            target[:, :-shift] = target[:, shift:]
            images.append(im)
            masks.append(target)
        
        # shift up
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=dataset_len//4))
        for i in rand_n:
            shift = 20
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)
        
            im[:-shift, :] = im[shift:, :]
            target[:-shift, :] = target[shift:, :]
            images.append(im)
            masks.append(target)
        
        # shift down
        rand_n = list(np.random.randint(low=0, high=dataset_len, size=dataset_len//4))
        for i in rand_n:
            shift = 20
            im = images[i]
            target = masks[i]
            class_ = im_classes[i]
            im_classes.append(class_)
        
            im[shift:, :] = im[:-shift, :]
            target[shift:, :] = target[:-shift, :]
            images.append(im)
            masks.append(target)
        
        images_test = images
        masks_test = masks
        
        train_dataset = UWFSDataLoader2(
            output_image_height=config.TRAIN.IMAGE_SIZE[0],
            images=images_train,
            masks=masks_train,
            normalizer=transform.Compose(train_transform_list),
            channel_values=None
        )
        val_dataset = UWFSDataLoader2(
            output_image_height=config.TRAIN.IMAGE_SIZE[0],
            images=images_test,
            masks=masks_test,
            normalizer=transform.Compose(val_transform_list),
            channel_values=None
        )
    else:
        train_dataset = None
        val_dataset = None
        logger.info("=> no dataset found. " 'Exiting...')
        exit()

    train_sampler = None
    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=(train_sampler is None),
        num_workers=config.WORKERS,
        pin_memory=True,
        sampler=train_sampler,
        drop_last=True
    )
    logger.info(f'Train loader has len: {len(train_loader)}')

    val_sampler = None
    val_loader = torch.utils.data.DataLoader(
        val_dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=config.WORKERS,
        pin_memory=True,
        sampler=val_sampler,
        drop_last=True
    )
    logger.info(f'Validation loader has len: {len(val_loader)}')

    # criterion
    if config.LOSS.USE_OHEM:
        criterion = OhemCrossEntropy(ignore_label=config.TRAIN.IGNORE_LABEL,
                                     thres=config.LOSS.OHEMTHRES,
                                     min_kept=config.LOSS.OHEMKEEP)  # ,weight=train_dataset.class_weights)
    else:
        criterion = CrossEntropy(ignore_label=config.TRAIN.IGNORE_LABEL)  # ,weight=train_dataset.class_weights)

    model = FullModel(model, criterion)

    model = nn.DataParallel(model, device_ids=gpus).cuda()
    logger.info(f'Using DataParallel')

    # optimizer
    if config.TRAIN.OPTIMIZER == 'sgd':

        params_dict = dict(model.named_parameters())
        if config.TRAIN.NONBACKBONE_KEYWORDS:
            bb_lr = []
            nbb_lr = []
            nbb_keys = set()
            for k, param in params_dict.items():
                if any(part in k for part in config.TRAIN.NONBACKBONE_KEYWORDS):
                    nbb_lr.append(param)
                    nbb_keys.add(k)
                else:
                    bb_lr.append(param)
            print(nbb_keys)
            params = [{'params': bb_lr, 'lr': config.TRAIN.LR},
                      {'params': nbb_lr, 'lr': config.TRAIN.LR * config.TRAIN.NONBACKBONE_MULT}]
        else:
            params = [{'params': list(params_dict.values()), 'lr': config.TRAIN.LR}]

        optimizer = torch.optim.SGD(params,
                                    lr=config.TRAIN.LR,
                                    momentum=config.TRAIN.MOMENTUM,
                                    weight_decay=config.TRAIN.WD,
                                    nesterov=config.TRAIN.NESTEROV,
                                    )
    else:
        raise ValueError('Only Support SGD optimizer')

    epoch_iters = int(train_dataset.__len__() / config.TRAIN.BATCH_SIZE_PER_GPU / len(gpus))

    best_mIoU = 0
    last_epoch = 0
    if config.TRAIN.RESUME:
        model_state_file = os.path.join(final_output_dir,
                                        'checkpoint.pth.tar')
        if os.path.isfile(model_state_file):
            checkpoint = torch.load(model_state_file, map_location={'cuda:0': 'cpu'})
            best_mIoU = checkpoint['best_mIoU']
            last_epoch = checkpoint['epoch']
            dct = checkpoint['state_dict']

            model.module.model.load_state_dict(
                {k.replace('model.', ''): v for k, v in checkpoint['state_dict'].items() if k.startswith('model.')})
            optimizer.load_state_dict(checkpoint['optimizer'])
            logger.info("=> loaded checkpoint (epoch {})"
                        .format(checkpoint['epoch']))

    if config.MODEL.PRETRAINED:
        model_state_file = config.MODEL.PRETRAINED
        if os.path.isfile(model_state_file):
            checkpoint = torch.load(model_state_file, map_location={'cuda:0': 'cpu'})
            model.module.load_state_dict(checkpoint)
            logger.info("=> loaded pretrained model {}"
                        .format(config.MODEL.PRETRAINED))

    extra_epoch_iters = 0
    start = timeit.default_timer()
    end_epoch = config.TRAIN.END_EPOCH + config.TRAIN.EXTRA_EPOCH
    num_iters = config.TRAIN.END_EPOCH * epoch_iters
    extra_iters = config.TRAIN.EXTRA_EPOCH * extra_epoch_iters

    valid_loss, mean_IoU, IoU_array = validate(config,
                                               train_loader, model, writer_dict)

    msg = 'Loss: {:.3f}, MeanIU: {: 4.4f}'.format(
        valid_loss, mean_IoU)
    logging.info(msg)
    logging.info(IoU_array)

    writer_dict['writer'].close()
    end = timeit.default_timer()
    logger.info('Hours: %d' % np.int((end - start) / 3600))
    logger.info('Done')


if __name__ == '__main__':
    main()
