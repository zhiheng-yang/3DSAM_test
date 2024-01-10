from dataset.datasets import load_data_volume
from segment_anything import SamAutomaticMaskGenerator, sam_model_registry
import argparse
from torch.optim import AdamW
import numpy as np
import logging
from utils.script_util import save_checkpoint
import sys
from monai.losses import DiceCELoss, DiceLoss
from modeling.Med_SAM.image_encoder import ImageEncoderViT_3d_v2 as ImageEncoderViT_3d
import torch.nn.functional as F
from modeling.Med_SAM.mask_decoder import VIT_MLAHead_h as VIT_MLAHead
import torch
from modeling.Med_SAM.prompt_encoder import PromptEncoder, TwoWayTransformer
import torch.nn as nn
from functools import partial
import os
from utils.util import setup_logger

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default='kits', type=str, choices=["kits", "pancreas", "lits", "colon"])
    parser.add_argument("--snapshot_path", default="./snapshots", type=str)
    parser.add_argument("--data_prefix", default="/home/jie/newdisk/Medical/Datasets/tumor/kits/data", type=str)
    parser.add_argument("--rand_crop_size", default=0, nargs='+', type=int, help="random crop size")
    parser.add_argument("--device", default="cuda:0", type=str)
    parser.add_argument("-bs", "--batch_size", default=1, type=int)
    parser.add_argument("--num_classes", default=2, type=int)
    parser.add_argument("--lr", default=4e-4, type=float)
    parser.add_argument("--max_epoch", default=500, type=int)
    parser.add_argument("--eval_interval", default=4, type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--num_worker", default=6, type=int)
    parser.add_argument("-tolerance", default=5, type=int)

    args = parser.parse_args()
    device = args.device

    # specify random crop size
    if args.rand_crop_size == 0:
        if args.data in ["pancreas", "lits", "colon", "kits"]:
            args.rand_crop_size = (128, 128, 128)
    else:
        if len(args.rand_crop_size) == 1:
            args.rand_crop_size = tuple(args.rand_crop_size * 3)
        else:
            args.rand_crop_size = tuple(args.rand_crop_size)

    # specify snapshot path
    args.snapshot_path = os.path.join(args.snapshot_path, args.data)
    if not os.path.exists(args.snapshot_path):
        os.makedirs(args.snapshot_path)

    # setup logger
    setup_logger(logger_name="train", root=args.snapshot_path, screen=True, tofile=True)
    logger = logging.getLogger(f"train")
    logger.info(str(args))

    # load data
    train_data = load_data_volume(
        data=args.data,
        path_prefix=args.data_prefix,
        batch_size=1,
        augmentation=True,
        split="train",
        rand_crop_spatial_size=args.rand_crop_size,
        num_worker=args.num_worker
    )
    val_data = load_data_volume(
        data=args.data,
        path_prefix=args.data_prefix,
        batch_size=1,
        augmentation=False,
        split="val",
        deterministic=True,
        rand_crop_spatial_size=args.rand_crop_size,
        num_worker = args.num_worker
    )

    # load pretrained sam model
    sam = sam_model_registry["vit_b"](checkpoint="ckpt/sam_vit_b_01ec64.pth")

    mask_generator = SamAutomaticMaskGenerator(sam)

    # adapted image encoder
    img_encoder = ImageEncoderViT_3d(
        depth=12,
        embed_dim=768,
        img_size=1024,
        mlp_ratio=4,
        norm_layer=partial(torch.nn.LayerNorm, eps=1e-6),
        num_heads=12,
        patch_size=16,
        qkv_bias=True,
        use_rel_pos=True,
        global_attn_indexes=[2, 5, 8, 11],
        window_size=14,
        cubic_window_size=8,
        out_chans=256,
        num_slice=16)

    # load frozen parameters from sam
    img_encoder.load_state_dict(mask_generator.predictor.model.image_encoder.state_dict(), strict=False)
    del sam
    img_encoder.to(device)

    # set trainable parameters in image encoder
    for p in img_encoder.parameters():
        p.requires_grad = False
    img_encoder.depth_embed.requires_grad = True
    for p in img_encoder.slice_embed.parameters():
        p.requires_grad = True
    for i in img_encoder.blocks:
        for p in i.norm1.parameters():
            p.requires_grad = True
        for p in i.adapter.parameters():
            p.requires_grad = True
        for p in i.norm2.parameters():
            p.requires_grad = True
        i.attn.rel_pos_d = nn.parameter.Parameter(0.5 * (i.attn.rel_pos_h + i.attn.rel_pos_w), requires_grad=True)
    for i in img_encoder.neck_3d:
        for p in i.parameters():
            p.requires_grad = True

    # prompt encoder (trainable)
    prompt_encoder_list = []
    parameter_list = []
    for i in range(4):
        prompt_encoder = PromptEncoder(transformer=TwoWayTransformer(depth=2,
                                                                 embedding_dim=256,
                                                                 mlp_dim=2048,
                                                                 num_heads=8))
        prompt_encoder.to(device)
        prompt_encoder_list.append(prompt_encoder)
        parameter_list.extend([i for i in prompt_encoder.parameters() if i.requires_grad == True])

    # mask decoder (trainable)
    mask_decoder = VIT_MLAHead(img_size=96, num_classes=2)
    mask_decoder.to(device)

    # set optimizer and scheduler
    # encoder
    encoder_opt = AdamW([i for i in img_encoder.parameters() if i.requires_grad==True], lr=args.lr, weight_decay=0)
    encoder_scheduler = torch.optim.lr_scheduler.LinearLR(encoder_opt, start_factor=1.0, end_factor=0.01, total_iters=500)
    # prompt encoder
    feature_opt = AdamW(parameter_list, lr=args.lr, weight_decay=0)
    feature_scheduler = torch.optim.lr_scheduler.LinearLR(feature_opt, start_factor=1.0, end_factor=0.01, total_iters=500)
    # mask decoder
    decoder_opt = AdamW([i for i in mask_decoder.parameters() if i.requires_grad == True], lr=args.lr, weight_decay=0)
    decoder_scheduler = torch.optim.lr_scheduler.LinearLR(decoder_opt, start_factor=1.0, end_factor=0.01, total_iters=500)

    # set loss function
    dice_loss = DiceLoss(include_background=False, softmax=True, to_onehot_y=True, reduction="none")
    loss_cal = DiceCELoss(include_background=False, softmax=True, to_onehot_y=True, lambda_dice=0.5, lambda_ce=0.5)
    best_loss = np.inf
    patch_size = args.rand_crop_size[0]

    for epoch_num in range(args.max_epoch):
        """start training"""
        loss_summary = []
        img_encoder.train()
        for module in prompt_encoder_list:
            module.train()
        mask_decoder.train()
        for idx, (img, seg, spacing) in enumerate(train_data):
            print('seg: ', seg.sum())
            out = F.interpolate(img.float(), scale_factor=512 / patch_size, mode='trilinear')
            # input_batch = (out.cuda() - pixel_mean) / pixel_std
            input_batch = out.to(device)
            input_batch = input_batch[0].transpose(0, 1)
            batch_features, feature_list = img_encoder(input_batch)
            feature_list.append(batch_features)
            #feature_list = feature_list[::-1]

            # generate points for interaction
            l = len(torch.where(seg == 1)[0])
            points_torch = None
            if l > 0:
                sample = np.random.choice(np.arange(l), 10, replace=True)
                x = torch.where(seg == 1)[1][sample].unsqueeze(1)
                y = torch.where(seg == 1)[3][sample].unsqueeze(1)
                z = torch.where(seg == 1)[2][sample].unsqueeze(1)
                points = torch.cat([x, y, z], dim=1).unsqueeze(1).float()
                points_torch = points.to(device)
                points_torch = points_torch.transpose(0,1)
            l = len(torch.where(seg < 10)[0])
            sample = np.random.choice(np.arange(l), 20, replace=True)
            x = torch.where(seg < 10)[1][sample].unsqueeze(1)
            y = torch.where(seg < 10)[3][sample].unsqueeze(1)
            z = torch.where(seg < 10)[2][sample].unsqueeze(1)
            points = torch.cat([x, y, z], dim=1).unsqueeze(1).float()
            points_torch_negative = points.to(device)
            points_torch_negative = points_torch_negative.transpose(0, 1)
            if points_torch is not None:
                points_torch = torch.cat([points_torch, points_torch_negative], dim=1)
            else:
                points_torch = points_torch_negative

            # encode points prompts
            new_feature = []
            for i, (feature, prompt_encoder) in enumerate(zip(feature_list, prompt_encoder_list)):
                if i == 3:  # 只在最后一层加入prompt
                    new_feature.append(
                        prompt_encoder(feature, points_torch.clone(), [patch_size, patch_size, patch_size])
                    )
                else:
                    new_feature.append(feature)

            # include original image to mask decoder
            img_resize = F.interpolate(img[:, 0].permute(0, 2, 3, 1).unsqueeze(1).to(device), scale_factor=64/patch_size,
                mode='trilinear')
            new_feature.append(img_resize)

            # predict mask
            masks = mask_decoder(new_feature, 2, patch_size//64)
            masks = masks.permute(0, 1, 4, 2, 3)

            # ground truth
            seg = seg.to(device)
            seg = seg.unsqueeze(1)

            # calculate loss
            loss = loss_cal(masks, seg)
            loss_summary.append(loss.detach().cpu().numpy())

            # optimize
            encoder_opt.zero_grad()
            decoder_opt.zero_grad()
            feature_opt.zero_grad()
            loss.backward()

            logger.info('epoch: {}/{}, iter: {}/{}'.format(epoch_num, args.max_epoch, idx, len(train_data)) + ": loss:" + str(loss_summary[-1].flatten()[0]))

            # clip gradient
            torch.nn.utils.clip_grad_norm_(img_encoder.parameters(), 1.0)
            torch.nn.utils.clip_grad_norm_(mask_decoder.parameters(), 1.0)
            torch.nn.utils.clip_grad_norm_(prompt_encoder_list[-1].parameters(), 1.0)

            # update parameters
            encoder_opt.step()
            feature_opt.step()
            decoder_opt.step()

        # update learning rate
        encoder_scheduler.step()
        feature_scheduler.step()
        decoder_scheduler.step()

        logger.info("- Train metrics: " + str(np.mean(loss_summary)))

        """start validation"""
        img_encoder.eval()
        for module in prompt_encoder_list:
            module.eval()
        mask_decoder.eval()
        with torch.no_grad():
            loss_summary = []
            for idx, (img, seg, spacing) in enumerate(val_data):
                print('seg: ', seg.sum())
                out = F.interpolate(img.float(), scale_factor=512 / patch_size, mode='trilinear')
                input_batch = out.to(device)
                input_batch = input_batch[0].transpose(0, 1)
                batch_features, feature_list = img_encoder(input_batch)
                feature_list.append(batch_features)
                #feature_list = feature_list[::-1]

                # generate points for interaction
                l = len(torch.where(seg == 1)[0])
                points_torch = None
                if l > 0:
                    sample = np.random.choice(np.arange(l), 10, replace=True)
                    x = torch.where(seg == 1)[1][sample].unsqueeze(1)
                    y = torch.where(seg == 1)[3][sample].unsqueeze(1)
                    z = torch.where(seg == 1)[2][sample].unsqueeze(1)
                    points = torch.cat([x, y, z], dim=1).unsqueeze(1).float()
                    points_torch = points.to(device)
                    points_torch = points_torch.transpose(0, 1)
                l = len(torch.where(seg < 10)[0])
                sample = np.random.choice(np.arange(l), 10, replace=True)
                x = torch.where(seg < 10)[1][sample].unsqueeze(1)
                y = torch.where(seg < 10)[3][sample].unsqueeze(1)
                z = torch.where(seg < 10)[2][sample].unsqueeze(1)
                points = torch.cat([x, y, z], dim=1).unsqueeze(1).float()
                points_torch_negative = points.to(device)
                points_torch_negative = points_torch_negative.transpose(0, 1)
                if points_torch is not None:
                    points_torch = points_torch
                else:
                    points_torch = points_torch_negative

                # encode points prompts
                new_feature = []
                for i, (feature, prompt_encoder) in enumerate(zip(feature_list, prompt_encoder_list)):
                    if i == 3:
                        new_feature.append(
                            prompt_encoder(feature, points_torch.clone(), [patch_size, patch_size, patch_size])
                        )
                    else:
                        new_feature.append(feature)

                # include original image to mask decoder
                img_resize = F.interpolate(img[:, 0].permute(0, 2, 3, 1).unsqueeze(1).to(device), scale_factor=64/patch_size,
                                           mode='trilinear')
                new_feature.append(img_resize)

                # predict mask
                masks = mask_decoder(new_feature, 2, patch_size//64)
                masks = masks.permute(0, 1, 4, 2, 3)

                # ground truth
                seg = seg.to(device)
                seg = seg.unsqueeze(1)

                # calculate loss
                loss = dice_loss(masks, seg)
                loss_summary.append(loss.detach().cpu().numpy())
                logger.info('epoch: {}/{}, iter: {}/{}'.format(epoch_num, args.max_epoch, idx, len(val_data)) + ": loss:" + str(loss_summary[-1].flatten()[0]))
        logger.info("- Val metrics: " + str(np.mean(loss_summary)))

        # save best model
        is_best = False
        if np.mean(loss_summary) < best_loss:
            best_loss = np.mean(loss_summary)
            is_best = True
        save_checkpoint({"epoch": epoch_num + 1,
                        "best_val_loss": best_loss,
                         "encoder_dict": img_encoder.state_dict(),
                         "decoder_dict": mask_decoder.state_dict(),
                         "feature_dict": [i.state_dict() for i in prompt_encoder_list],
                         "encoder_opt": encoder_opt.state_dict(),
                         "feature_opt": feature_opt.state_dict(),
                         "decoder_opt": decoder_opt.state_dict()
                         },
                        is_best=is_best,
                        checkpoint=args.snapshot_path)
        logger.info("- Val metrics best: " + str(best_loss))


if __name__ == "__main__":
    main()

