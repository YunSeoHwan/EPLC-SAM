import warnings
warnings.filterwarnings('ignore')

from segment_anything import sam_model_registry
from segment_anything.utils import *
from segment_anything.utils import trainer_dice_iou_point_softmax

import albumentations as A

import torch
import torch.nn as nn 

import torch.distributed as dist
from torch.utils.data import DataLoader, BatchSampler
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel

import os
import argparse
import wandb
from datetime import datetime
from torchinfo import summary

from model.backbone import resnet50
from model import adl_fft, adl_fft_internel_point_softmax

CHECKPOINT_DIR = 'checkpoints'
os.makedirs(CHECKPOINT_DIR, exist_ok=True)

def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

def get_args_parser():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--batch_size', type=int, default=1, help='batch size allocated to each GPU')
    parser.add_argument('--port', type=int, default=1234, help='port number for distributed learning')
    parser.add_argument('--dist', type=str2bool, default=True, help='if True, use multi-gpu(distributed) training')
    parser.add_argument('--seed', type=int, default=21, help='random seed')
    parser.add_argument('--model_type', type=str, default='vit_b', help='SAM model type')
    parser.add_argument('--checkpoint', type=str, default='sam_vit_b.pth', help='SAM model checkpoint')
    parser.add_argument('--checkpoint_cls', type=str, default='sam_vit_b.pth', help='Classifier model checkpoint')
    parser.add_argument('--epoch', type=int, default=10, help='total epoch')
    parser.add_argument('--lr', type=float, default=2e-4, help='initial learning rate')
    parser.add_argument('--dataset_type', type=str, default=0.75, help='pseudo mask dataset option')
    parser.add_argument('--project_name', type=str, default='Fine-tuning-SAM', help='WandB project name')
    parser.add_argument('--checkpoint_decoder', type=str, default='decoder.pth', help='decoder weight file')
    parser.add_argument('--local_rank', type=int)
    parser.add_argument('--pth_name', type=str)
    parser.add_argument('--alpha', type=float)
    parser.add_argument('--gamma', type=int)
    parser.add_argument('--cuda', type=int)
    parser.add_argument('--weight', type=int)
    
    return parser

### Fine-tuning SAM ###
def main(rank, opts) -> str:
    """
    Model fine-tuning
    
    Returns:
        str: Save path of model checkpoint 
    """
    seed.seed_everything(opts.seed)
    
    # set_dist.init_distributed_training(rank, opts)
    opts.rank = 0
    opts.gpu = 0
    local_gpu_id = opts.gpu + opts.cuda


    ### checkpoint & WandB set ### 
    run_time = datetime.now()
    run_time = run_time.strftime("%b%d_%H%M%S")
    file_name = opts.pth_name + '.pth'
    save_path = os.path.join(CHECKPOINT_DIR, file_name)
    
    if opts.rank == 0:
        wandb.init(project=opts.project_name)
        wandb.run.name = f'lr{opts.lr}_dice_iou'

    ### dataset & dataloader ### 
    # data augmentation for train image & mask 
    transform = A.Compose([
        A.OneOf([
            A.HorizontalFlip(p=1),
            A.VerticalFlip(p=1),
            A.RandomRotate90(p=1),
            A.ShiftScaleRotate(p=1)
        ], p=0.5)
    ])
    
    # opts.dataset_type 
    train_set = dataset.make_dataset(
        image_dir=f'/home/team1/ddrive/team1/sam_dataset/internal_dataset/train/image',
        mask_dir=f'{opts.dataset_type}',
        transform=transform
    )
    #   /home/team1/ddrive/team1/SAM_WSSS_CAM/WSSS_latest/code/pseudo_mask_generation/outputs/pseudo_masks_f1_{opts.dataset_type}  
    val_set = dataset.make_dataset(
        image_dir=f'/home/team1/ddrive/team1/sam_dataset/internal_dataset/val/image',
        mask_dir=f'/home/team1/ddrive/team1/sam_dataset/internal_dataset/val/mask'
    )
    
    if opts.dist:
        train_sampler = DistributedSampler(dataset=train_set, shuffle=True, seed=opts.seed)
        batch_sampler_train = BatchSampler(train_sampler, opts.batch_size, drop_last=True)
        train_loader = DataLoader(train_set, batch_sampler=batch_sampler_train, num_workers=opts.num_workers)
    
    if not opts.dist:
        train_loader = DataLoader(
            train_set, 
            batch_size=opts.batch_size, 
            shuffle=True, 
            num_workers=opts.num_workers
        )

    val_loader = DataLoader(
        val_set, 
        batch_size=opts.batch_size, 
        shuffle=False
    )
    
    ### SAM config ### 
    sam_checkpoint = opts.checkpoint
    model_type = opts.model_type

    sam = sam_model_registry[model_type](checkpoint=sam_checkpoint)
    sam.cuda(local_gpu_id)

    # sam = save_weight.load_partial_weight(
    #     model=sam,
    #     load_path=opts.checkpoint_decoder,
    #     dist=False
    # )    

    # set trainable parameters
    for _, p in sam.image_encoder.named_parameters():
        p.requires_grad = False
        
    for _, p in sam.prompt_encoder.named_parameters():
        p.requires_grad = False

    # fine-tuning mask decoder         
    for _, p in sam.mask_decoder.named_parameters():
        p.requires_grad = True
    
    # print model info 
    print()
    print('=== MODEL INFO ===')
    summary(sam)
    print()

    if not opts.dist:
        model = sam
    
    if opts.dist:
        sam = DistributedDataParallel(module=sam, device_ids=[local_gpu_id])    
        model = sam.module
        
    ### training config ###  
   
    iouloss = iou_loss_torch.IoULoss()
    diceloss = dice_loss_torch.DiceLoss()
    # focalloss = focal_loss_torch.FocalLoss(alpha=opts.alpha, gamma=opts.gamma, device=local_gpu_id)

    EPOCHS = opts.epoch
    lr = opts.lr
    # EarlyStopping : Determined based on the validation IOU. Higher is better(mode='max').
    es = trainer_dice_iou_point_softmax.EarlyStopping(patience=10, delta=0, mode='min', verbose=True)
    es_signal = torch.tensor([0]).to(local_gpu_id)
    optimizer = torch.optim.AdamW(
        model.parameters(), 
        lr=lr
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, 
        T_max=len(train_loader), 
        eta_min=0,
        last_epoch=-1
    )

    if opts.rank == 0:
        wandb.watch(
            models=model,
            # criterion=(bceloss, iouloss, diceloss, focalloss),
            criterion=(diceloss, iouloss),
            log='all',
            log_freq=10
        )
        
        wandb.run.summary['optimizer'] = type(optimizer).__name__
        wandb.run.summary['scheduler'] = type(scheduler).__name__
        wandb.run.summary['initial lr'] = lr
        wandb.run.summary['total epoch'] = EPOCHS
    
    min_loss = np.Inf
    
    for epoch in range(EPOCHS):
        
        # EarlyStopping
        if opts.dist:
            dist.barrier()  
            dist.all_reduce(es_signal, op=dist.ReduceOp.SUM) 
            
        if es_signal.item() == 1:
            break
        
        if opts.dist:
            train_sampler.set_epoch(epoch)
        
        ### model train / validation ###
        # train_iou_loss, train_iou_loss, train_dice_loss, train_focal_loss, train_dice, train_iou = trainer_dice_focal_point.model_train(
        train_dice_loss, train_iou_loss, train_dice, train_iou = trainer_dice_iou_point_softmax.model_train(
            model=model,
            data_loader=train_loader,
            # criterion=[bceloss, iouloss, diceloss, focalloss],
            criterion=[diceloss, iouloss],
            optimizer=optimizer,
            device=local_gpu_id,
            weight=opts.weight,
            scheduler=scheduler
        )
        
        if opts.dist:
            dist.barrier()
            
            dist.all_reduce(train_iou_loss, op=dist.ReduceOp.SUM)            
            train_iou_loss = train_iou_loss.item() / dist.get_world_size()
            
            # dist.all_reduce(train_iou_loss, op=dist.ReduceOp.SUM)            
            # train_iou_loss = train_iou_loss.item() / dist.get_world_size()
            
            dist.all_reduce(train_dice, op=dist.ReduceOp.SUM)
            train_dice_loss = train_dice_loss.item() / dist.get_world_size()
            
            # dist.all_reduce(train_dice, op=dist.ReduceOp.SUM)
            # train_focal_loss = train_focal_loss.item() / dist.get_world_size()
            
            dist.all_reduce(train_dice, op=dist.ReduceOp.SUM)
            train_dice = train_dice.item() / dist.get_world_size()
            
            dist.all_reduce(train_iou, op=dist.ReduceOp.SUM)
            train_iou = train_iou.item() / dist.get_world_size()
            
        if not opts.dist:
            train_iou_loss = train_iou_loss.item()
            # train_iou_loss = train_iou_loss.item()
            train_dice_loss = train_dice_loss.item()
            # train_focal_loss = train_focal_loss.item()
            train_dice = train_dice.item()
            train_iou = train_iou.item()
            
        
        if opts.rank == 0:
            # resnet = resnet50.ResNet50(pretrain=False)
            # Classifier
            cls = adl_fft_internel_point_softmax.resnet50_adl(
                architecture_type='adl', 
                pretrained=False, 
                adl_drop_rate=0.75, 
                adl_drop_threshold=0.8
            ).to(device=f"cuda:{local_gpu_id}")
            cls.load_state_dict(torch.load(f'{opts.checkpoint_cls}', map_location=f"cuda:{local_gpu_id}"))
            cls.eval()
            
            # val_iou_loss, val_iou_loss, val_dice_loss, val_focal_loss, val_dice, val_iou = trainer_dice_focal_point.model_evaluate(
            val_dice_loss, val_iou_loss, val_dice, val_iou = trainer_dice_iou_point_softmax.model_evaluate(
                model=model,
                cls=cls,
                data_loader=val_loader,
                # criterion=[bceloss, iouloss, diceloss, focalloss],
                criterion=[diceloss, iouloss],
                weight=opts.weight,
                device=f"cuda:{local_gpu_id}"
            )
            
            val_loss = val_dice_loss + val_iou_loss
            
            wandb.log(
                {
                    'Train IoU Loss': train_iou_loss,
                    # 'Train IoU Loss': train_iou_loss,
                    'Train DICE Loss': train_dice_loss,
                    # 'Train Focal Loss': train_focal_loss,
                    'Train Dice Metric': train_dice,
                    'Train IoU Metric': train_iou
                }, step=epoch+1
            )
        
            wandb.log(
                {
                    'Validation IoU Loss': val_iou_loss,
                    # 'Validation IoU Loss': val_iou_loss,
                    'Validation DICE Loss': val_dice_loss,
                    # 'Validation Focal Loss': val_focal_loss,
                    'Validation Dice Metric': val_dice,
                    'Validation IoU Metric': val_iou
                }, step=epoch+1
            )
            
            # Check EarlyStopping
            es(val_loss)
            if es.early_stop:
                es_signal = torch.tensor([1]).to(local_gpu_id)
                continue
        
            ### Save best model ###
            if val_loss < min_loss:
                print(f'[INFO] val_loss has been improved from {min_loss:.5f} to {val_loss:.5f}. Save model.')
                min_loss = val_loss
                _ = save_weight.save_partial_weight(model=sam, save_path=save_path)
            
            
            
            ### print current loss / metric ###
            # print(f'epoch {epoch+1:02d}, bce_loss: {train_iou_loss:.5f}, iou_loss: {train_iou_loss:.5f}, dice_loss: {train_dice_loss:.5f}, focal_loss: {train_focal_loss:.5f}, dice: {train_dice:.5f}, iou: {train_iou:.5f},', end=' ')
            print(f'epoch {epoch+1:02d}, dice_loss: {train_dice_loss:.5f}, iou_loss: {train_iou_loss:.5f}, dice: {train_dice:.5f}, iou: {train_iou:.5f} \n')
            # print(f'val_iou_loss: {val_iou_loss:.5f}, val_iou_loss: {val_iou_loss:.5f}, val_dice_loss: {val_dice_loss:.5f}, val_focal_loss: {val_focal_loss:.5f}, val_dice: {val_dice:.5f}, val_iou: {val_iou:.5f} \n')
            print(f'val_dice_loss: {val_dice_loss:.5f}, val_iou_loss: {val_iou_loss:.5f}, val_dice: {val_dice:.5f}, val_iou: {val_iou:.5f} \n')
    
    print(f'Model checkpoint saved at: {save_path} \n') 
    
    return

if __name__ == '__main__': 

    wandb.login()
    
    parser = argparse.ArgumentParser('Training Segmentor', parents=[get_args_parser()])
    opts = parser.parse_args() 
    
    if opts.dist:
        opts.ngpus_per_node = torch.cuda.device_count()
        opts.gpu_ids = list(range(opts.ngpus_per_node))
        opts.num_workers = opts.ngpus_per_node * 4
        
        torch.multiprocessing.spawn(
            main,
            args=(opts,),
            nprocs=opts.ngpus_per_node,
            join=True
        )
        
    if not opts.dist:
        opts.ngpus_per_node = 1
        opts.gpu_ids = [0]
        opts.num_workers = 0
    
        main(rank=0, opts=opts)
    
    print('=== DONE === \n')    