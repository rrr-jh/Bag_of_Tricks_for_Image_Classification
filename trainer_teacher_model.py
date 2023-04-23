import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
from torch.optim.lr_scheduler import _LRScheduler
import torch.utils.data as data
from torch.utils.tensorboard import SummaryWriter

import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torchvision.models as models

from sklearn import decomposition
from sklearn import manifold
from sklearn.metrics import confusion_matrix
from sklearn.metrics import ConfusionMatrixDisplay
import matplotlib.pyplot as plt
import numpy as np

import copy
from collections import namedtuple
import os
import random
import shutil
import time
import argparse

from model_archive.ResNet import ResNet, Config


SEED = 42

random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed(SEED)
torch.backends.cudnn.deterministic = True

def normalize_image(image):
    image_min = image.min()
    image_max = image.max()
    image.clamp_(min = image_min, max = image_max)
    image.add_(-image_min).div_(image_max - image_min + 1e-5)
    return image    

def calculate_topk_accuracy(y_pred, y, k = 5):
    with torch.no_grad():
        batch_size = y.shape[0]
        _, top_pred = y_pred.topk(k, 1)
        top_pred = top_pred.t()
        correct = top_pred.eq(y.view(1, -1).expand_as(top_pred))
        correct_1 = correct[:1].reshape(-1).float().sum(0, keepdim = True)
        correct_k = correct[:k].reshape(-1).float().sum(0, keepdim = True)
        acc_1 = correct_1 / batch_size
        acc_k = correct_k / batch_size
    return acc_1, acc_k

def train(model, iterator, optimizer, criterion, device, args, scheduler=None):
    
    epoch_loss = 0
    epoch_acc_1 = 0
    epoch_acc_5 = 0
    
    model.train()
    
    for (x, y) in iterator:
        
        x = x.to(device)
        y = y.to(device)
        
        optimizer.zero_grad()
        
        if args.pretrained=='yes':
            y_pred= model(x)
        else:
            y_pred, _ = model(x)
        
        loss = criterion(y_pred, y)
        
        acc_1, acc_5 = calculate_topk_accuracy(y_pred, y)
        
        loss.backward()
        optimizer.step()

        if scheduler=='yes':
            scheduler.step()
        
        epoch_loss += loss.item()
        epoch_acc_1 += acc_1.item()
        epoch_acc_5 += acc_5.item()
        
    epoch_loss /= len(iterator)
    epoch_acc_1 /= len(iterator)
    epoch_acc_5 /= len(iterator)
        
    return epoch_loss, epoch_acc_1, epoch_acc_5

def evaluate(model, iterator, criterion, device, args):
    
    epoch_loss = 0
    epoch_acc_1 = 0
    epoch_acc_5 = 0
    
    model.eval()
    
    with torch.no_grad():
        
        for (x, y) in iterator:

            x = x.to(device)
            y = y.to(device)

            if args.pretrained!='yes':
                y_pred, _ = model(x)
            else:
                y_pred = model(x)

            loss = criterion(y_pred, y)

            acc_1, acc_5 = calculate_topk_accuracy(y_pred, y)

            epoch_loss += loss.item()
            epoch_acc_1 += acc_1.item()
            epoch_acc_5 += acc_5.item()
        
    epoch_loss /= len(iterator)
    epoch_acc_1 /= len(iterator)
    epoch_acc_5 /= len(iterator)
    
    return epoch_loss, epoch_acc_1, epoch_acc_5

def epoch_time(start_time, end_time):
    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
    return elapsed_mins, elapsed_secs

def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)

# Not yet for transfer learning
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch_size", type=int, default=64)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--epochs", type=int, default=150)
    parser.add_argument("--root_path", type=str, default='/root/datasets/archive/CUB_200_2011/')
    parser.add_argument("--scheduler", type=str, default='no')      # yes / no
    parser.add_argument("--lin_scheduler", type=str, default='no')      # yes / no
    parser.add_argument("--pretrained", type=str, default='yes')     # yes / no
    args = parser.parse_args()


    print('Training ResNet152 Teacher Model for Knowledge Distillation.')
    print()
    print('<< Configurations >>')
    print(f'[*] Model       - ResNet152')
    print(f'[*] Batch_size  - {args.batch_size}')
    print(f'[*] LR          - {args.lr}')
    print(f'[*] Epochs      - {args.epochs}')


    # Dataset
    pretrained_size = 224
    pretrained_means = [0.485, 0.456, 0.406]
    pretrained_stds= [0.229, 0.224, 0.225]
    train_transforms = transforms.Compose([
                            transforms.Resize(pretrained_size),
                            transforms.RandomRotation(5),
                            transforms.RandomHorizontalFlip(0.5),
                            transforms.RandomCrop(pretrained_size, padding = 10),
                            transforms.ToTensor(),
                            transforms.Normalize(mean = pretrained_means, 
                                                    std = pretrained_stds)
                        ])
    test_transforms = transforms.Compose([
                            transforms.Resize(pretrained_size),
                            transforms.CenterCrop(pretrained_size),
                            transforms.ToTensor(),
                            transforms.Normalize(mean = pretrained_means, 
                                                    std = pretrained_stds)
                        ])
    ROOT = args.root_path
    data_dir = os.path.join(ROOT, 'CUB_200_2011')
    images_dir = os.path.join(data_dir, 'images')
    train_dir = os.path.join(data_dir, 'train')
    test_dir = os.path.join(data_dir, 'test')

    train_data = datasets.ImageFolder(root = train_dir,
                                  transform = train_transforms)
    test_data = datasets.ImageFolder(root = test_dir,
                                    transform = test_transforms)
    
    VALID_RATIO = 0.8
    n_train_examples = int(len(train_data)*VALID_RATIO)
    n_valid_examples = len(train_data) - n_train_examples

    train_data, valid_data = data.random_split(train_data,
                                            [n_train_examples, n_valid_examples])
    valid_data = copy.deepcopy(valid_data)
    valid_data.dataset.transform = test_transforms

    BATCH_SIZE = args.batch_size
    train_iterator = data.DataLoader(train_data, 
                                    shuffle = True, 
                                    batch_size = BATCH_SIZE)
    valid_iterator = data.DataLoader(valid_data, 
                                    batch_size = BATCH_SIZE)
    test_iterator = data.DataLoader(test_data, 
                                    batch_size = BATCH_SIZE)


    # Model Zoo
    if args.pretrained=='yes':
        print('[*] pre-trained model being used!')
        model = models.resnet152(pretrained = True)
        IN_FEATURES = model.fc.in_features 
        OUTPUT_DIM = len(test_data.classes)
        model.fc = nn.Linear(IN_FEATURES, OUTPUT_DIM)
    else:
        print('[*] train newly initialized model!')
        config = Config()
        resnet_config = config.get_resnet_config(model_name = 'resnet152')
        OUTPUT_DIM = len(test_data.classes)
        model = ResNet(resnet_config, OUTPUT_DIM)

    print(f'[*] Parameters  - {count_parameters(model):,}')

    START_LR = args.lr
    optimizer = optim.Adam(model.parameters(), lr=START_LR)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    criterion = nn.CrossEntropyLoss()

    # * Use StepLR Scheduler
    if args.lin_scheduler=='yes':
        print('[*] Using StepLR Scheduler - step_size=30, gamma=0.5')
        scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=30, gamma=0.5)
    else:
        scheduler=None

    model = model.to(device)
    criterion = criterion.to(device)

    writer = SummaryWriter()

    # Model training.
    best_valid_loss = float('inf')
    best_valid_epoch = 0

    print('[*] Start Training !', end='\n\n')
    for epoch in range(args.epochs):
        start_time = time.monotonic()
        
        train_loss, train_acc_1, train_acc_5 = train(model, train_iterator, optimizer, criterion, device, args, scheduler)
        valid_loss, valid_acc_1, valid_acc_5 = evaluate(model, valid_iterator, criterion, device, args)

        writer.add_scalar("loss/train", train_loss, epoch)  # tensorboard
        writer.add_scalar("loss/val", valid_loss, epoch)    # tensorboard

        if valid_loss < best_valid_loss:
            best_valid_loss = valid_loss
            best_valid_epoch = epoch
            torch.save(model.state_dict(), f'./teacher_model/ResNet152_bs{args.batch_size}_lr{args.lr}_epochs{args.epochs}_pretrained-{args.pretrained}.pt')
        
        end_time = time.monotonic()

        epoch_mins, epoch_secs = epoch_time(start_time, end_time)
        
        print(f'Epoch: {epoch+1:02} | Epoch Time: {epoch_mins}m {epoch_secs}s')
        print(f'\tTrain Loss: {train_loss:.3f} | Train Acc @1: {train_acc_1*100:6.2f}% | ' \
            f'Train Acc @5: {train_acc_5*100:6.2f}%')
        print(f'\tValid Loss: {valid_loss:.3f} | Valid Acc @1: {valid_acc_1*100:6.2f}% | ' \
            f'Valid Acc @5: {valid_acc_5*100:6.2f}%')

    print()
    print(f"Best valid epoch : {best_valid_epoch}/{args.epochs} epochs")

    writer.flush()