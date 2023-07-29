import itertools
import math
import multiprocessing
import os.path
import shutil
import sys
import threading
import time
import tkinter
import tkinter as tk
import warnings
import webbrowser
from multiprocessing.pool import ThreadPool
from timeit import default_timer as timer

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
import torch_directml
from customtkinter import (CTk, 
                           CTkButton, 
                           CTkEntry, 
                           CTkFont, 
                           CTkImage,
                           CTkLabel, 
                           CTkOptionMenu, 
                           CTkScrollableFrame,
                           filedialog, 
                           set_appearance_mode,
                           set_default_color_theme)
from moviepy.editor import VideoFileClip
from moviepy.video.io import ImageSequenceClip
from PIL import Image


"""
NEW
Completely rewrote the tile management algorithm:
- cutting an image into tiles is ~60% faster
- tiles now also support transparent images
- tiles are no longer saved as files, to save disk space and time
- now the image/frame upscaled as a result of tiles is interpolated with the original image/frame: this reduces graphical defects while maintaining upscale quality

Added "Video output" widget that allows you to choose the extension of the upscaled video:
- .mp4, produces well compressed and good quality video
- .avi, produces very high quality video without compression
- .webm, produces very compressed and very light video

GUI
The app will now tell how many tiles the images are divided into during upscaling
Removed Mica effect (transparency) due to incompatibilities: often did not allow to select, zoom, and move the application window

IMPROVEMENTS
By default AI precision is set to "Half precision"
By default now "Input resolution %" is set to 50%
Partially rewrote and cleaned up more than 50% of the code
Updated all dependencies
"""


app_name = "RealScaler"
version  = "2.3"

githubme   = "https://github.com/Djdefrag/ReSRScaler"
itchme     = "https://jangystudio.itch.io/realesrscaler"
telegramme = "https://linktr.ee/j3ngystudio"

AI_models_list       = [ 
                        'RealESR_Gx4', 
                        'RealSRx4_Anime', 
                        'RealESRGANx4', 
                        'RealESRNetx4'
                        ]

image_extension_list  = [ '.png', '.jpg', '.bmp', '.tiff' ]
video_extension_list  = [ '.mp4', '.avi', '.webm' ]

device_list_names    = []
device_list          = []
vram_multiplier      = 0.9
gpus_found           = torch_directml.device_count()
resize_algorithm     = cv2.INTER_AREA

offset_y_options = 0.1125
row1_y           = 0.705
row2_y           = row1_y + offset_y_options
row3_y           = row2_y + offset_y_options

app_name_color = "#4169E1"
dark_color     = "#080808"

torch.autograd.set_detect_anomaly(False)
torch.autograd.profiler.profile(False)
torch.autograd.profiler.emit_nvtx(False)
if sys.stdout is None: sys.stdout = open(os.devnull, "w")
if sys.stderr is None: sys.stderr = open(os.devnull, "w")



# ------------------ AI ------------------

def default_init_weights(module_list, scale=1, bias_fill=0):
    if not isinstance(module_list, list):
        module_list = [module_list]
    for module in module_list:
        if isinstance(module, nn.Conv2d) or isinstance(module, nn.Linear):
            init.kaiming_normal_(module.weight)
            module.weight.data *= scale
            if module.bias is not None:
                module.bias.data.fill_(bias_fill)
        elif isinstance(module, nn.BatchNorm2d):
            init.constant_(module.weight, 1)
            if module.bias is not None:
                module.bias.data.fill_(bias_fill)

def make_layer(basic_block, num_basic_block, **kwarg):
    layers = []
    for _ in range(num_basic_block):
        layers.append(basic_block(**kwarg))
    return nn.Sequential(*layers)

class ResidualBlockNoBN(nn.Module):
    def __init__(self, num_feat=64, res_scale=1):
        super(ResidualBlockNoBN, self).__init__()
        self.res_scale = res_scale
        self.conv1 = nn.Conv2d(num_feat, num_feat, 3, 1, 1, bias=True)
        self.conv2 = nn.Conv2d(num_feat, num_feat, 3, 1, 1, bias=True)
        self.relu = nn.ReLU(inplace=True)

        default_init_weights([self.conv1, self.conv2], 0.1)

    def forward(self, x):
        identity = x
        out = self.conv2(self.relu(self.conv1(x)))
        return identity + out * self.res_scale

class Upsample(nn.Sequential):
    def __init__(self, scale, num_feat):
        m = []
        if (scale & (scale - 1)) == 0:  # scale = 2^n
            for _ in range(int(math.log(scale, 2))):
                m.extend([nn.Conv2d(num_feat, 4 * num_feat, 3, 1, 1), nn.PixelShuffle(2)])
        elif scale == 3:
            m.extend([nn.Conv2d(num_feat, 9 * num_feat, 3, 1, 1), nn.PixelShuffle(3)])
        else:
            raise ValueError(f'scale {scale} is not supported. Supported scales: 2^n and 3.')
        super(Upsample, self).__init__(*m)

def pixel_unshuffle(x, scale):
    b, c, hh, hw = x.size()
    out_channel = c * (scale**2)
    h = hh // scale
    w = hw // scale
    x_view = x.view(b, c, h, scale, w, scale)
    return x_view.permute(0, 1, 3, 5, 2, 4).reshape(b, out_channel, h, w)

def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn(
            'mean is more than 2 std from [a, b] in nn.init.trunc_normal_. '
            'The distribution of values may be incorrect.',
            stacklevel=2)

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        low = (1. + math.erf((a - mean) / (std * math.sqrt(2.)))) / 2.
        up = (1. + math.erf((b - mean) / (std * math.sqrt(2.)))) / 2.

        # Uniformly fill tensor with values from [low, up], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * low - 1, 2 * up - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.)).add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor

def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)

class ResidualDenseBlock(nn.Module):
    def __init__(self, num_feat=64, num_grow_ch=32):
        super(ResidualDenseBlock, self).__init__()
        self.conv1 = nn.Conv2d(num_feat, num_grow_ch, 3, 1, 1)
        self.conv2 = nn.Conv2d(num_feat + num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv3 = nn.Conv2d(num_feat + 2 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv4 = nn.Conv2d(num_feat + 3 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv5 = nn.Conv2d(num_feat + 4 * num_grow_ch, num_feat, 3, 1, 1)

        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

        # initialization
        default_init_weights([self.conv1, self.conv2, self.conv3, self.conv4, self.conv5], 0.1)

    def forward(self, x):
        x1 = self.lrelu(self.conv1(x))
        x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
        x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
        x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
        x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
        # Empirically, we use 0.2 to scale the residual for better performance
        return x5 * 0.2 + x

class RRDB(nn.Module):
    def __init__(self, num_feat, num_grow_ch=32):
        super(RRDB, self).__init__()
        self.rdb1 = ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb2 = ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb3 = ResidualDenseBlock(num_feat, num_grow_ch)

    def forward(self, x):
        out = self.rdb1(x)
        out = self.rdb2(out)
        out = self.rdb3(out)
        # Empirically, we use 0.2 to scale the residual for better performance
        return out * 0.2 + x

class RRDBNet(nn.Module):

    def __init__(self, num_in_ch, num_out_ch, scale=4, num_feat=64, num_block=23, num_grow_ch=32):
        super(RRDBNet, self).__init__()
        self.scale = scale
        if scale == 2:
            num_in_ch = num_in_ch * 4
        elif scale == 1:
            num_in_ch = num_in_ch * 16
        self.conv_first = nn.Conv2d(num_in_ch, num_feat, 3, 1, 1)
        self.body = make_layer(RRDB, num_block, num_feat=num_feat, num_grow_ch=num_grow_ch)
        self.conv_body = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        # upsample
        self.conv_up1 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_up2 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_hr = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_last = nn.Conv2d(num_feat, num_out_ch, 3, 1, 1)

        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        if self.scale == 2:
            feat = pixel_unshuffle(x, scale=2)
        elif self.scale == 1:
            feat = pixel_unshuffle(x, scale=4)
        else:
            feat = x
        feat = self.conv_first(feat)
        body_feat = self.conv_body(self.body(feat))
        feat = feat + body_feat
        # upsample
        feat = self.lrelu(self.conv_up1(F.interpolate(feat, scale_factor=2, mode='nearest')))
        feat = self.lrelu(self.conv_up2(F.interpolate(feat, scale_factor=2, mode='nearest')))
        out = self.conv_last(self.lrelu(self.conv_hr(feat)))
        return out

class SRVGGNetCompact(nn.Module):

    def __init__(self, num_in_ch=3, num_out_ch=3, num_feat=64, num_conv=16, upscale=4, act_type='prelu'):
        super(SRVGGNetCompact, self).__init__()
        self.num_in_ch = num_in_ch
        self.num_out_ch = num_out_ch
        self.num_feat = num_feat
        self.num_conv = num_conv
        self.upscale = upscale
        self.act_type = act_type

        self.body = nn.ModuleList()
        # the first conv
        self.body.append(nn.Conv2d(num_in_ch, num_feat, 3, 1, 1))
        # the first activation
        if act_type == 'relu':
            activation = nn.ReLU(inplace=True)
        elif act_type == 'prelu':
            activation = nn.PReLU(num_parameters=num_feat)
        elif act_type == 'leakyrelu':
            activation = nn.LeakyReLU(negative_slope=0.1, inplace=True)
        self.body.append(activation)

        # the body structure
        for _ in range(num_conv):
            self.body.append(nn.Conv2d(num_feat, num_feat, 3, 1, 1))
            # activation
            if act_type == 'relu':
                activation = nn.ReLU(inplace=True)
            elif act_type == 'prelu':
                activation = nn.PReLU(num_parameters=num_feat)
            elif act_type == 'leakyrelu':
                activation = nn.LeakyReLU(negative_slope=0.1, inplace=True)
            self.body.append(activation)

        # the last conv
        self.body.append(nn.Conv2d(num_feat, num_out_ch * upscale * upscale, 3, 1, 1))
        # upsample
        self.upsampler = nn.PixelShuffle(upscale)

    def forward(self, x):
        out = x
        for i in range(0, len(self.body)):
            out = self.body[i](out)

        out = self.upsampler(out)
        # add the nearest upsampled image, so that the network learns the residual
        base = F.interpolate(x, scale_factor=self.upscale, mode='nearest')
        out += base
        return out

def prepare_model(selected_AI_model, backend, half_precision):
    model_path = find_by_relative_path("AI" + os.sep + selected_AI_model + ".pth")

    if 'RealESR_Gx4' in selected_AI_model: 
        model = SRVGGNetCompact(num_in_ch  = 3, 
                                num_out_ch = 3, 
                                num_feat   = 64, 
                                num_conv   = 32, 
                                upscale    = 4, 
                                act_type   = 'prelu')
    elif 'RealSRx4_Anime' in selected_AI_model:
        model = SRVGGNetCompact(num_in_ch  = 3, 
                                num_out_ch = 3, 
                                num_feat   = 64, 
                                num_conv   = 16, 
                                upscale    = 4, 
                                act_type   = 'prelu')
    elif 'RealESRGANx4' in selected_AI_model:
        model = RRDBNet(num_in_ch  = 3, 
                        num_out_ch = 3, 
                        num_feat   = 64, 
                        num_block  = 23, 
                        num_grow_ch = 32, 
                        scale = 4)
    elif 'RealESRNetx4' in selected_AI_model:
        model = RRDBNet(num_in_ch  = 3, 
                        num_out_ch = 3, 
                        num_feat   = 64, 
                        num_block  = 23, 
                        num_grow_ch = 32, 
                        scale = 4)

    with torch.no_grad():
        pretrained_model = torch.load(model_path, map_location = torch.device('cpu'))
        if 'params_ema' in pretrained_model: keyname = 'params_ema'
        else: keyname = 'params'
        model.load_state_dict(pretrained_model[keyname], strict = True)
    model.eval()

    if half_precision: model = model.half()
    model = model.to(backend, non_blocking = True)
        
    return model

def AI_enhance(model, image, backend, half_precision):
    image = image.astype(np.float32)

    max_range = 65535 if np.max(image) > 256 else 255
    image /= max_range

    img_mode = 'RGB'
    if len(image.shape) == 2:  # gray image
        img_mode = 'L'
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    elif image.shape[2] == 4:  # RGBA image with alpha channel
        img_mode = 'RGBA'
        alpha = image[:, :, 3]
        image = image[:, :, :3]
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        alpha = cv2.cvtColor(alpha, cv2.COLOR_GRAY2RGB)

    image = torch.from_numpy(np.transpose(image, (2, 0, 1))).float()
    if half_precision:
        image = image.unsqueeze(0).half().to(backend, non_blocking=True)
    else:
        image = image.unsqueeze(0).to(backend, non_blocking=True)

    output = model(image)

    output_img = output.squeeze().float().clamp(0, 1).cpu().numpy()
    output_img = np.transpose(output_img, (1, 2, 0))

    if img_mode == 'L':
        output_img = cv2.cvtColor(output_img, cv2.COLOR_RGB2GRAY)

    if img_mode == 'RGBA':
        alpha = torch.from_numpy(np.transpose(alpha, (2, 0, 1))).float()
        if half_precision:
            alpha = alpha.unsqueeze(0).half().to(backend, non_blocking=True)
        else:
            alpha = alpha.unsqueeze(0).to(backend, non_blocking=True)

        output_alpha = model(alpha)

        output_alpha = output_alpha.squeeze().float().clamp(0, 1).cpu().numpy()
        output_alpha = np.transpose(output_alpha, (1, 2, 0))
        output_alpha = cv2.cvtColor(output_alpha, cv2.COLOR_RGB2GRAY)

        output_img = cv2.cvtColor(output_img, cv2.COLOR_RGB2BGRA)
        output_img[:, :, 3] = output_alpha

    output = (output_img * max_range).round().astype(np.uint16 if max_range == 65535 else np.uint8)

    return output



# Classes and utils -------------------

class Gpu:
    def __init__(self, index, name):
        self.name   = name
        self.index  = index

class ScrollableImagesTextFrame(CTkScrollableFrame):
    def __init__(self, master, command=None, **kwargs):
        super().__init__(master, **kwargs)
        self.grid_columnconfigure(0, weight=1)
        self.label_list  = []
        self.button_list = []
        self.file_list   = []

    def get_selected_file_list(self): 
        return self.file_list

    def add_clean_button(self):
        label = CTkLabel(self, text = "")
        button = CTkButton(self, 
                            font  = bold11,
                            text  = "CLEAN", 
                            fg_color   = "#282828",
                            text_color = "#E0E0E0",
                            image    = clear_icon,
                            compound = "left",
                            width    = 85, 
                            height   = 27,
                            corner_radius = 25)
        button.configure(command=lambda: self.clean_all_items())
        button.grid(row = len(self.button_list), column=1, pady=(0, 10), padx = 5)
        self.label_list.append(label)
        self.button_list.append(button)

    def add_item(self, text_to_show, file_element, image = None):
        label = CTkLabel(self, 
                        text  = text_to_show,
                        font  = bold11,
                        image = image, 
                        #fg_color   = "#282828",
                        text_color = "#E0E0E0",
                        compound = "left", 
                        padx     = 10,
                        pady     = 5,
                        corner_radius = 25,
                        anchor   = "center")
                        
        label.grid(row  = len(self.label_list), column = 0, 
                   pady = (3, 3), padx = (3, 3), sticky = "w")
        self.label_list.append(label)
        self.file_list.append(file_element)    

    def clean_all_items(self):
        self.label_list  = []
        self.button_list = []
        self.file_list   = []
        place_up_background()
        place_loadFile_section()

for index in range(gpus_found): 
    gpu = Gpu(index = index, name = torch_directml.device_name(index))
    device_list.append(gpu)
    device_list_names.append(gpu.name)

supported_file_extensions = [
                            '.jpg', '.jpeg', '.JPG', '.JPEG',
                            '.png', '.PNG',
                            '.webp', '.WEBP',
                            '.bmp', '.BMP',
                            '.tif', '.tiff', '.TIF', '.TIFF',
                            '.mp4', '.MP4',
                            '.webm', '.WEBM',
                            '.mkv', '.MKV',
                            '.flv', '.FLV',
                            '.gif', '.GIF',
                            '.m4v', ',M4V',
                            '.avi', '.AVI',
                            '.mov', '.MOV',
                            '.qt', '.3gp', 
                            '.mpg', '.mpeg'
                            ]

supported_video_extensions  = [
                                '.mp4', '.MP4',
                                '.webm', '.WEBM',
                                '.mkv', '.MKV',
                                '.flv', '.FLV',
                                '.gif', '.GIF',
                                '.m4v', ',M4V',
                                '.avi', '.AVI',
                                '.mov', '.MOV',
                                '.qt', '.3gp', 
                                '.mpg', '.mpeg'
                            ]



#  Slice functions -------------------

def add_alpha_channel(tile):
    if tile.shape[2] == 3:  # Check if the tile does not have an alpha channel
        alpha_channel = np.full((tile.shape[0], tile.shape[1], 1), 255, dtype=np.uint8)
        tile = np.concatenate((tile, alpha_channel), axis=2)
    return tile

def split_image_into_tiles(image, num_tiles_x, num_tiles_y):
    img_height, img_width, _ = image.shape

    tile_width = img_width // num_tiles_x
    tile_height = img_height // num_tiles_y

    tiles = []

    for y in range(num_tiles_y):
        y_start = y * tile_height
        y_end = (y + 1) * tile_height

        for x in range(num_tiles_x):
            x_start = x * tile_width
            x_end = (x + 1) * tile_width

            tile = image[y_start:y_end, x_start:x_end]

            tiles.append(tile)

    return tiles

def combine_tiles_into_image(tiles, 
                             image_for_dimensions, 
                             starting_image, 
                             num_tiles_x, 
                             num_tiles_y, 
                             output_path):
    
    # Utilizzo l immagine downscalata per calcolare le giuste dimensioni 
    original_height, original_width, _ = image_for_dimensions.shape
    output_width = int(original_width * 4)
    output_height = int(original_height * 4)

    # Ridimensiono e aggiungo l'alpha channel all' immagine iniziale
    # affinché le dimensioni durante l interpolazione siano identiche
    starting_image = add_alpha_channel(cv2.resize(starting_image, 
                                        (output_width, output_height), 
                                        interpolation = cv2.INTER_CUBIC))

    tiled_image = np.zeros((output_height, output_width, 4), dtype = np.uint8)

    for i, tile in enumerate(tiles):
        tile_height, tile_width, _ = tile.shape
        row = i // num_tiles_x
        col = i % num_tiles_x
        y_start = row * tile_height
        y_end = y_start + tile_height
        x_start = col * tile_width
        x_end = x_start + tile_width

        tiled_image[y_start:y_end, x_start:x_end] = add_alpha_channel(tile)

    tiled_image = cv2.addWeighted(tiled_image, 0.5, starting_image, 0.5, 0)

    image_write(output_path, tiled_image)

def file_need_tiles(image, tiles_resolution):
    height, width, _ = image.shape

    tile_size = tiles_resolution

    num_tiles_horizontal = (width + tile_size - 1) // tile_size
    num_tiles_vertical = (height + tile_size - 1) // tile_size

    total_tiles = num_tiles_horizontal * num_tiles_vertical

    if total_tiles <= 1:
        return False, 0, 0
    else:
        return True, num_tiles_horizontal, num_tiles_vertical



# Utils functions ------------------------

def opengithub(): webbrowser.open(githubme, new=1)

def openitch(): webbrowser.open(itchme, new=1)

def opentelegram(): webbrowser.open(telegramme, new=1)

def image_write(path, image_data):
    _, file_extension = os.path.splitext(path)
    cv2.imwrite(path, image_data)

def image_read(image_to_prepare, flags=cv2.IMREAD_UNCHANGED):
    return cv2.imread(image_to_prepare, flags)

def create_temp_dir(name_dir):
    if os.path.exists(name_dir): shutil.rmtree(name_dir)
    if not os.path.exists(name_dir): os.makedirs(name_dir, mode=0o777)

def remove_dir(name_dir):
    if os.path.exists(name_dir): shutil.rmtree(name_dir)

def write_in_log_file(text_to_insert):
    log_file_name = app_name + ".log"
    with open(log_file_name,'w') as log_file: 
        os.chmod(log_file_name, 0o777)
        log_file.write(text_to_insert) 
    log_file.close()

def read_log_file():
    log_file_name = app_name + ".log"
    with open(log_file_name,'r') as log_file: 
        os.chmod(log_file_name, 0o777)
        step = log_file.readline()
    log_file.close()
    return step

def find_by_relative_path(relative_path):
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

def prepare_output_image_filename(image_path, 
                                  selected_AI_model, 
                                  resize_factor, 
                                  selected_image_extension):
    
    # Remove extension
    result_path, _ = os.path.splitext(image_path)

    resize_percentage = str(int(resize_factor * 100)) + "%"
    to_append = f"_{selected_AI_model}_{resize_percentage}{selected_image_extension}"

    if "_resized" in result_path:
        result_path = result_path.replace("_resized", to_append)
    else:
        result_path += to_append

    return result_path

def prepare_output_video_filename(video_path, 
                                  selected_AI_model, 
                                  resize_factor, 
                                  selected_video_extension):
    
    # Remove original video file extension
    original_video_path, _ = os.path.splitext(video_path)

    to_append = f"_{selected_AI_model}_{int(resize_factor * 100)}%{selected_video_extension}"

    return original_video_path + to_append

def delete_list_of_files(list_to_delete):
    if len(list_to_delete) > 0:
        for to_delete in list_to_delete:
            if os.path.exists(to_delete):
                os.remove(to_delete)

def resize_image(image_path, resize_factor):
    image = image_read(image_path)
    old_height, old_width, _ = image.shape
    new_width = int(old_width * resize_factor)
    new_height = int(old_height * resize_factor)

    resized_image = cv2.resize(image, 
                               (new_width, new_height), 
                               interpolation = resize_algorithm)
    return resized_image       

def resize_frame(image_path, new_width, new_height, target_file_extension):
    new_image_path = image_path.replace('.jpg', "" + target_file_extension)
    
    old_image = image_read(image_path.strip(), cv2.IMREAD_UNCHANGED)

    resized_image = cv2.resize(old_image, 
                               (new_width, new_height), 
                                interpolation = resize_algorithm)    
    image_write(new_image_path, resized_image)

def resize_frame_list(image_list, resize_factor, target_file_extension, cpu_number):
    downscaled_images = []

    old_image = Image.open(image_list[1])
    new_width, new_height = old_image.size
    new_width = int(new_width * resize_factor)
    new_height = int(new_height * resize_factor)
    
    with ThreadPool(cpu_number) as pool:
        pool.starmap(resize_frame, zip(image_list, 
                                    itertools.repeat(new_width), 
                                    itertools.repeat(new_height), 
                                    itertools.repeat(target_file_extension)))

    for image in image_list:
        resized_image_path = image.replace('.jpg', "" + target_file_extension)
        downscaled_images.append(resized_image_path)

    return downscaled_images

def remove_file(name_file):
    if os.path.exists(name_file): os.remove(name_file)

def show_error(exception):
    import tkinter as tk
    tk.messagebox.showerror(title   = 'Error', 
                            message = 'Upscale failed caused by:\n\n' +
                                        str(exception) + '\n\n' +
                                        'Please report the error on Github.com or Itch.io.' +
                                        '\n\nThank you :)')

def extract_frames_from_video(video_path):
    video_frames_list = []
    cap          = cv2.VideoCapture(video_path)
    frame_rate   = cap.get(cv2.CAP_PROP_FPS)
    cap.release()

    # extract frames
    video = VideoFileClip(video_path)
    img_sequence = app_name + "_temp" + os.sep + "frame_%01d" + '.jpg'
    video_frames_list = video.write_images_sequence(img_sequence, 
                                                    verbose = False,
                                                    logger  = None, 
                                                    fps     = frame_rate)
    
    # extract audio
    try: video.audio.write_audiofile(app_name + "_temp" + os.sep + "audio.mp3",
                                    verbose = False,
                                    logger  = None)
    except: pass

    return video_frames_list

def video_reconstruction_by_frames(input_video_path, 
                                   frames_upscaled_list, 
                                   selected_AI_model, 
                                   resize_factor, 
                                   cpu_number,
                                   selected_video_extension):
    
    # Find original video FPS
    cap          = cv2.VideoCapture(input_video_path)
    frame_rate   = cap.get(cv2.CAP_PROP_FPS)
    cap.release()

    # Choose the appropriate codec
    if selected_video_extension == '.mp4':
        extension = '.mp4'
        codec = 'libx264'
    elif selected_video_extension == '.avi':
        extension = '.avi'
        codec = 'png'
    elif selected_video_extension == '.webm':
        extension = '.webm'
        codec = 'libvpx'

    upscaled_video_path = prepare_output_video_filename(input_video_path, 
                                                        selected_AI_model, 
                                                        resize_factor, 
                                                        extension)
    audio_file = app_name + "_temp" + os.sep + "audio.mp3"

    clip = ImageSequenceClip.ImageSequenceClip(frames_upscaled_list, fps = frame_rate)
    if os.path.exists(audio_file) and extension != '.webm':
        clip.write_videofile(upscaled_video_path,
                            fps     = frame_rate,
                            audio   = audio_file,
                            codec   = codec,
                            verbose = False,
                            logger  = None,
                            threads = cpu_number)
    else:
        clip.write_videofile(upscaled_video_path,
                             fps     = frame_rate,
                             codec   = codec,
                             verbose = False,
                             logger  = None,
                             threads = cpu_number)  



# Core functions ------------------------

def remove_temp_files():
    remove_dir(app_name + "_temp")
    remove_file(app_name + ".log")

def stop_thread():
    # to stop a thread execution
    stop = 1 + "x"

def stop_upscale_process():
    global process_upscale_orchestrator
    process_upscale_orchestrator.terminate()
    process_upscale_orchestrator.join()

def check_upscale_steps():
    time.sleep(3)
    try:
        while True:
            step = read_log_file()
            if "All files completed" in step:
                info_message.set(step)
                stop_upscale_process()
                remove_temp_files()
                stop_thread()
            elif "Error while upscaling" in step:
                info_message.set("Error while upscaling :(")
                remove_temp_files()
                stop_thread()
            elif "Stopped upscaling" in step:
                info_message.set("Stopped upscaling")
                stop_upscale_process()
                remove_temp_files()
                stop_thread()
            else:
                info_message.set(step)
            time.sleep(2)
    except:
        place_upscale_button()

def update_process_status(actual_process_phase):
    print("> " + actual_process_phase)
    write_in_log_file(actual_process_phase) 

def stop_button_command():
    stop_upscale_process()
    write_in_log_file("Stopped upscaling") 

def upscale_button_command(): 
    global selected_file_list
    global selected_AI_model
    global half_precision
    global selected_AI_device 
    global selected_image_extension
    global selected_video_extension
    global tiles_resolution
    global resize_factor
    global cpu_number

    global process_upscale_orchestrator

    remove_file(app_name + ".log")
    
    if user_input_checks():
        info_message.set("Loading")
        write_in_log_file("Loading")

        print("=================================================")
        print("> Starting upscale:")
        print("  Files to upscale: "   + str(len(selected_file_list)))
        print("  Selected AI model: "  + str(selected_AI_model))
        print("  AI half precision: "  + str(half_precision))
        print("  Selected GPU: "       + str(torch_directml.device_name(selected_AI_device)))
        print("  Selected image output extension: "          + str(selected_image_extension))
        print("  Selected video output extension: "          + str(selected_video_extension))
        print("  Tiles resolution for selected GPU VRAM: "   + str(tiles_resolution) + "x" + str(tiles_resolution) + "px")
        print("  Resize factor: "      + str(int(resize_factor*100)) + "%")
        print("  Cpu number: "         + str(cpu_number))
        print("=================================================")

        backend = torch.device(torch_directml.device(selected_AI_device))

        place_stop_button()

        process_upscale_orchestrator = multiprocessing.Process(
                                            target = upscale_orchestrator,
                                            args   = (selected_file_list,
                                                     selected_AI_model,
                                                     backend, 
                                                     selected_image_extension,
                                                     tiles_resolution,
                                                     resize_factor,
                                                     cpu_number,
                                                     half_precision,
                                                     selected_video_extension))
        process_upscale_orchestrator.start()

        thread_wait = threading.Thread(target = check_upscale_steps, daemon = True)
        thread_wait.start()

def upscale_image(image_path, 
                  AI_model, 
                  selected_AI_model, 
                  backend, 
                  selected_image_extension, 
                  tiles_resolution, 
                  resize_factor, 
                  half_precision):
    
    starting_image = image_read(image_path)
    
    if resize_factor != 1:
        image_to_upscale = resize_image(image_path, resize_factor) 
    else:
        image_to_upscale = image_read(image_path)

    result_image_path = prepare_output_image_filename(image_path, 
                                                        selected_AI_model, 
                                                        resize_factor, 
                                                        selected_image_extension)  

    need_tiles, num_tiles_x, num_tiles_y = file_need_tiles(image_to_upscale, tiles_resolution)

    if need_tiles:
        update_process_status(f"Tiling image in {num_tiles_x * num_tiles_y}")
        tiles_list = split_image_into_tiles(image_to_upscale, num_tiles_x, num_tiles_y)

        with torch.no_grad():
            for i, tile in enumerate(tiles_list, 0):
                update_process_status(f"Upscaling tiles {i}/{len(tiles_list)}")                
                tile_upscaled = AI_enhance(AI_model, tile, backend, half_precision)

                if tile_upscaled.shape[:2] != (tile.shape[1] * 4, tile.shape[0] * 4):
                    tile_upscaled = cv2.resize(tile_upscaled, (tile.shape[1] * 4, tile.shape[0] * 4), interpolation = cv2.INTER_CUBIC)

                tiles_list[i] = tile_upscaled

            update_process_status("Reconstructing image by tiles")
            combine_tiles_into_image(tiles_list, image_to_upscale, starting_image, num_tiles_x, num_tiles_y, result_image_path)
    else:
        with torch.no_grad():
            update_process_status("Upscaling image")
            image_upscaled = AI_enhance(AI_model, image_to_upscale, backend, half_precision)
            image_write(result_image_path, image_upscaled)

def upscale_video(video_path, 
                  AI_model, 
                  selected_AI_model, 
                  backend, 
                  selected_image_extension, 
                  tiles_resolution,
                  resize_factor, 
                  cpu_number, 
                  half_precision, 
                  selected_video_extension):
    
    create_temp_dir(app_name + "_temp")

    update_process_status("Extracting video frames")
    frame_list_paths = extract_frames_from_video(video_path)
    starting_frame_list_paths = frame_list_paths

    if resize_factor != 1:
        update_process_status("Resizing video frames")
        frame_list_paths = resize_frame_list(frame_list_paths, resize_factor, selected_image_extension, cpu_number)

    update_process_status("Upscaling video")
    first_frame = image_read(frame_list_paths[0])
    frames_upscaled_paths_list = []   
    need_tiles, num_tiles_x, num_tiles_y = file_need_tiles(first_frame, tiles_resolution)

    if need_tiles:
        for index_frame, frame_path in enumerate(frame_list_paths, 0):
            if (index_frame % 8 == 0): update_process_status(f"Upscaling frame {index_frame}/{len(frame_list_paths)}")
            
            frame = image_read(frame_path)

            result_path = prepare_output_image_filename(frame_path, selected_AI_model, resize_factor, selected_image_extension)
            
            tiles_list = split_image_into_tiles(frame, num_tiles_x, num_tiles_y)

            with torch.no_grad():
                for i, tile in enumerate(tiles_list, 0):
                    tile_upscaled = AI_enhance(AI_model, tile, backend,  half_precision)

                    if tile_upscaled.shape[:2] != (tile.shape[1] * 4, tile.shape[0] * 4):
                        tile_upscaled = cv2.resize(tile_upscaled, 
                                                    (tile.shape[1] * 4, tile.shape[0] * 4), 
                                                    interpolation = cv2.INTER_CUBIC)
                        
                    tiles_list[i] = tile_upscaled

            starting_frame = image_read(starting_frame_list_paths[index_frame])
            combine_tiles_into_image(tiles_list, frame, starting_frame, num_tiles_x, num_tiles_y, result_path)
            frames_upscaled_paths_list.append(result_path)

    else:
        for index_frame, frame_path in enumerate(frame_list_paths, 0):
            if (index_frame % 8 == 0): update_process_status(f"Upscaling frames {index_frame}/{len(frame_list_paths)}")
            
            with torch.no_grad():
                frame = image_read(frame_path, cv2.IMREAD_UNCHANGED)
                result_path = prepare_output_image_filename(frame_path, selected_AI_model, resize_factor, selected_image_extension)
                frame_upscaled = AI_enhance(AI_model, frame, backend, half_precision)
                image_write(result_path, frame_upscaled)
                frames_upscaled_paths_list.append(result_path)

    update_process_status("Processing upscaled video")
    video_reconstruction_by_frames(video_path, 
                                   frames_upscaled_paths_list, 
                                   selected_AI_model, 
                                   resize_factor, 
                                   cpu_number, 
                                   selected_video_extension)

def upscale_orchestrator(selected_file_list,
                         selected_AI_model,
                         backend, 
                         selected_image_extension,
                         tiles_resolution,
                         resize_factor,
                         cpu_number,
                         half_precision,
                         selected_video_extension):
    
    start = timer()
    torch.set_num_threads(cpu_number)

    try:
        update_process_status("Preparing AI model")
        AI_model = prepare_model(selected_AI_model, backend, half_precision)

        for index, file_path in enumerate(selected_file_list, 1):
            update_process_status(f"Upscaling {index}/{len(selected_file_list)}")

            if check_if_file_is_video(file_path):
                upscale_video(file_path, AI_model, selected_AI_model, backend, selected_image_extension, tiles_resolution, resize_factor, cpu_number, half_precision, selected_video_extension)
            else:
                upscale_image(file_path, AI_model, selected_AI_model, backend, selected_image_extension, tiles_resolution, resize_factor, half_precision)

        update_process_status(f"All files completed ({round(timer() - start)} sec.)")

    except Exception as exception:
        update_process_status('Error while upscaling\n\n' + str(exception))
        show_error(exception)



# GUI utils function ---------------------------

def user_input_checks():
    global selected_file_list
    global selected_AI_model
    global half_precision
    global selected_AI_device 
    global selected_image_extension
    global tiles_resolution
    global resize_factor
    global cpu_number

    is_ready = True

    # Selected files -------------------------------------------------
    try: selected_file_list = scrollable_frame_file_list.get_selected_file_list()
    except:
        info_message.set("No file selected. Please select a file")
        is_ready = False

    if len(selected_file_list) <= 0:
        info_message.set("No file selected. Please select a file")
        is_ready = False



    # File resize factor -------------------------------------------------
    try: resize_factor = int(float(str(selected_resize_factor.get())))
    except:
        info_message.set("Resize % must be a numeric value")
        is_ready = False

    if resize_factor > 0: resize_factor = resize_factor/100
    else:
        info_message.set("Resize % must be a value > 0")
        is_ready = False

    

    # Tiles resolution -------------------------------------------------
    try: tiles_resolution = 100 * int(float(str(selected_VRAM_limiter.get())))
    except:
        info_message.set("VRAM/RAM value must be a numeric value")
        is_ready = False 

    if tiles_resolution > 0: 
        selected_vram = (vram_multiplier * int(float(str(selected_VRAM_limiter.get()))))

        if half_precision == True:
            tiles_resolution = int(selected_vram * 100)
        elif half_precision == False:
            tiles_resolution = int(selected_vram * 100 * 0.60)

        if selected_AI_model == 'RealESR_Gx4' or selected_AI_model == 'RealSRx4_Anime':
            tiles_resolution = tiles_resolution * 2

    else:
        info_message.set("VRAM/RAM value must be > 0")
        is_ready = False



    # Cpu number -------------------------------------------------
    try: cpu_number = int(float(str(selected_cpu_number.get())))
    except:
        info_message.set("Cpu number must be a numeric value")
        is_ready = False 

    if cpu_number <= 0:         
        info_message.set("Cpu number value must be > 0")
        is_ready = False
    else: cpu_number = int(cpu_number)



    return is_ready

def extract_image_info(image_file):
    image_name = str(image_file.split("/")[-1])

    image  = image_read(image_file, cv2.IMREAD_UNCHANGED)
    width  = int(image.shape[1])
    height = int(image.shape[0])

    image_label = ( "IMAGE" + " | " + image_name + " | " + str(width) + "x" + str(height) )

    ctkimage = CTkImage(Image.open(image_file), size = (25, 25))

    return image_label, ctkimage

def extract_video_info(video_file):
    cap          = cv2.VideoCapture(video_file)
    width        = round(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height       = round(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    num_frames   = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_rate   = cap.get(cv2.CAP_PROP_FPS)
    duration     = num_frames/frame_rate
    minutes      = int(duration/60)
    seconds      = duration % 60
    video_name   = str(video_file.split("/")[-1])
    
    while(cap.isOpened()):
        ret, frame = cap.read()
        if ret == False: break
        image_write("temp.jpg", frame)
        break
    cap.release()

    video_label = ( "VIDEO" + " | " + video_name + " | " + str(width) + "x" 
                   + str(height) + " | " + str(minutes) + 'm:' 
                   + str(round(seconds)) + "s | " + str(num_frames) 
                   + "frames | " + str(round(frame_rate)) + "fps" )

    ctkimage = CTkImage(Image.open("temp.jpg"), size = (25, 25))
    
    return video_label, ctkimage

def check_if_file_is_video(file):
    for video_extension in supported_video_extensions:
        if video_extension in file:
            return True

def check_supported_selected_files(uploaded_file_list):
    supported_files_list = []

    for file in uploaded_file_list:
        for supported_extension in supported_file_extensions:
            if supported_extension in file:
                supported_files_list.append(file)

    return supported_files_list

def open_files_action():
    info_message.set("Selecting files...")

    uploaded_files_list = list(filedialog.askopenfilenames())
    uploaded_files_counter = len(uploaded_files_list)

    supported_files_list = check_supported_selected_files(uploaded_files_list)
    supported_files_counter = len(supported_files_list)
    
    print("> Uploaded files: " + str(uploaded_files_counter) + " => Supported files: " + str(supported_files_counter))

    if supported_files_counter > 0:
        place_up_background()

        global scrollable_frame_file_list
        scrollable_frame_file_list = ScrollableImagesTextFrame(master = window, 
                                                               fg_color = dark_color, 
                                                               bg_color = dark_color)
        scrollable_frame_file_list.place(relx = 0.5, 
                                         rely = 0.25, 
                                         relwidth = 1.0, 
                                         relheight = 0.475, 
                                         anchor = tkinter.CENTER)
        
        scrollable_frame_file_list.add_clean_button()

        for index in range(supported_files_counter):
            actual_file = supported_files_list[index]
            if check_if_file_is_video(actual_file):
                # video
                video_label, ctkimage = extract_video_info(actual_file)
                scrollable_frame_file_list.add_item(text_to_show = video_label, 
                                                    image = ctkimage,
                                                    file_element = actual_file)
                remove_file("temp.jpg")
            else:
                # image
                image_label, ctkimage = extract_image_info(actual_file)
                scrollable_frame_file_list.add_item(text_to_show = image_label, 
                                                    image = ctkimage,
                                                    file_element = actual_file)
    
        info_message.set("Ready")
    else: 
        info_message.set("Not supported files :(")



# GUI select from menus functions ---------------------------

def select_AI_from_menu(new_value: str):
    global selected_AI_model    
    selected_AI_model = new_value

def select_AI_mode_from_menu(new_value: str):
    global half_precision

    if new_value == "Full precision": half_precision = False
    elif new_value == "Half precision": half_precision = True

def select_AI_device_from_menu(new_value: str):
    global selected_AI_device    

    for device in device_list:
        if device.name == new_value:
            selected_AI_device = device.index

def select_image_extension_from_menu(new_value: str):
    global selected_image_extension    
    selected_image_extension = new_value

def select_video_extension_from_menu(new_value: str):
    global selected_video_extension   
    selected_video_extension = new_value



# GUI info functions ---------------------------

def open_info_AI_model():
    info = """This widget allows to choose between different AI: \n
- RealESR_Gx4 | good upscale quality | fast | enlarge by 4
- RealSRx4_Anime | | good upscale quality | fast | enlarge by 4
- RealESRGANx4 | high upscale quality | slow | enlarge by 4
- RealESRNetx4 | high upscale quality | slow | enlarge by 4 \n
Try them all and find the one that meets your needs :)""" 

    tk.messagebox.showinfo(title = 'GPU', message = info)
    
def open_info_device():
    info = """This widget allows to choose the gpu to run AI with. \n 
Keep in mind that the more powerful your gpu is, 
the faster the upscale will be \n
For best results, it is necessary to update the gpu drivers constantly"""

    tk.messagebox.showinfo(title = 'GPU', message = info)

def open_info_file_extension():
    info = """This widget allows to choose the extension of upscaled image/frame:\n
- png | very good quality | supports transparent images
- jpg | good quality | very fast
- bmp | highest quality | slow
- tiff | highest quality | very slow"""

    tk.messagebox.showinfo(title = 'Image output', message = info)

def open_info_resize():
    info = """This widget allows to choose the resolution input to the AI:\n
For example for a 100x100px image:
- Input resolution 50% => input to AI 50x50px
- Input resolution 100% => input to AI 100x100px
- Input resolution 200% => input to AI 200x200px """

    tk.messagebox.showinfo(title = 'Input resolution %', message = info)

def open_info_vram_limiter():
    info = """This widget allows to set a limit on the gpu's VRAM memory usage: \n
- For a gpu with 4 GB of Vram you must select 4
- For a gpu with 6 GB of Vram you must select 6
- For a gpu with 8 GB of Vram you must select 8
- For integrated gpus (Intel-HD series | Vega 3,5,7) 
  that do not have dedicated memory, you must select 2 \n
Selecting a value greater than the actual amount of gpu VRAM may result in upscale failure """

    tk.messagebox.showinfo(title = 'GPU Vram (GB)', message = info)
    
def open_info_cpu():
    info = """This widget allows you to choose how many cpus to devote to the app.\n
Where possible the app will use the number of processors you select, for example:
- Extracting frames from videos
- Resizing frames from videos
- Recostructing final video
- AI processing"""

    tk.messagebox.showinfo(title = 'Cpu number', message = info)

def open_info_AI_precision():
    info = """This widget allows you to choose the AI upscaling mode:

- Full precision (>=8GB Vram recommended)
  > compatible with all GPUs 
  > uses 50% more GPU memory than Half precision mode
  > is 30-70% faster than Half precision mode
  > may result in lower upscale quality
  
- Half precision
  > some old GPUs are not compatible with this mode
  > uses 50% less GPU memory than Full precision mode
  > is 30-70% slower than Full precision mode"""

    tk.messagebox.showinfo(title = 'AI mode', message = info)

def open_info_video_extension():
    info = """This widget allows you to choose the video output:

- .mp4  | produces good quality and well compressed video
- .avi  | produces the highest quality video
- .webm | produces low quality but light video (no audio)"""

    tk.messagebox.showinfo(title = 'Video output', message = info)    



# GUI place functions ---------------------------
        
def place_up_background():
    up_background = CTkLabel(master  = window, 
                            text    = "",
                            fg_color = dark_color,
                            font     = bold12,
                            anchor   = "w")
    
    up_background.place(relx = 0.5, 
                        rely = 0.0, 
                        relwidth = 1.0,  
                        relheight = 1.0,  
                        anchor = tkinter.CENTER)

def place_app_name():
    app_name_label = CTkLabel(master     = window, 
                              text       = app_name + " " + version,
                              text_color = app_name_color,
                              font       = bold20,
                              anchor     = "w")
    
    app_name_label.place(relx = 0.5, rely = 0.56, anchor = tkinter.CENTER)

def place_itch_button(): 
    itch_button = CTkButton(master     = window, 
                            width      = 30,
                            height     = 30,
                            fg_color   = "black",
                            text       = "", 
                            font       = bold11,
                            image      = logo_itch,
                            command    = openitch)
    itch_button.place(relx = 0.045, rely = 0.55, anchor = tkinter.CENTER)

def place_github_button():
    git_button = CTkButton(master      = window, 
                            width      = 30,
                            height     = 30,
                            fg_color   = "black",
                            text       = "", 
                            font       = bold11,
                            image      = logo_git,
                            command    = opengithub)
    git_button.place(relx = 0.045, rely = 0.61, anchor = tkinter.CENTER)

def place_telegram_button():
    telegram_button = CTkButton(master = window, 
                                width      = 30,
                                height     = 30,
                                fg_color   = "black",
                                text       = "", 
                                font       = bold11,
                                image      = logo_telegram,
                                command    = opentelegram)
    telegram_button.place(relx = 0.045, rely = 0.67, anchor = tkinter.CENTER)

def place_upscale_button(): 
    upscale_button = CTkButton(master    = window, 
                                width      = 140,
                                height     = 30,
                                fg_color   = "#282828",
                                text_color = "#E0E0E0",
                                text       = "UPSCALE", 
                                font       = bold11,
                                image      = play_icon,
                                command    = upscale_button_command)
    upscale_button.place(relx = 0.8, rely = row3_y, anchor = tkinter.CENTER)
    
def place_stop_button(): 
    stop_button = CTkButton(master   = window, 
                            width      = 140,
                            height     = 30,
                            fg_color   = "#282828",
                            text_color = "#E0E0E0",
                            text       = "STOP", 
                            font       = bold11,
                            image      = stop_icon,
                            command    = stop_button_command)
    stop_button.place(relx = 0.8, rely = row3_y, anchor = tkinter.CENTER)

def place_AI_menu():
    AI_menu_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "AI model",
                              height   = 23,
                              width    = 130,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_AI_model)

    AI_menu = CTkOptionMenu(master  = window, 
                            values  = AI_models_list,
                            width      = 140,
                            font       = bold11,
                            height     = 30,
                            fg_color   = "#000000",
                            anchor     = "center",
                            command    = select_AI_from_menu,
                            dropdown_font = bold11,
                            dropdown_fg_color = "#000000")

    AI_menu_button.place(relx = 0.20, rely = row1_y - 0.05, anchor = tkinter.CENTER)
    AI_menu.place(relx = 0.20, rely = row1_y, anchor = tkinter.CENTER)

def place_AI_mode_menu():
    AI_modes = ["Half precision", "Full precision"]

    AI_mode_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "AI mode",
                              height   = 23,
                              width    = 130,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_AI_precision)

    AI_mode_menu = CTkOptionMenu(master  = window, 
                                values   = AI_modes,
                                width      = 140,
                                font       = bold11,
                                height     = 30,
                                fg_color   = "#000000",
                                anchor     = "center",
                                dynamic_resizing = False,
                                command    = select_AI_mode_from_menu,
                                dropdown_font = bold11,
                                dropdown_fg_color = "#000000")
    
    AI_mode_button.place(relx = 0.20, rely = row2_y - 0.05, anchor = tkinter.CENTER)
    AI_mode_menu.place(relx = 0.20, rely = row2_y, anchor = tkinter.CENTER)

def place_image_extension_menu():
    file_extension_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "Image output",
                              height   = 23,
                              width    = 130,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_file_extension)

    file_extension_menu = CTkOptionMenu(master  = window, 
                                        values     = image_extension_list,
                                        width      = 140,
                                        font       = bold11,
                                        height     = 30,
                                        fg_color   = "#000000",
                                        anchor     = "center",
                                        command    = select_image_extension_from_menu,
                                        dropdown_font = bold11,
                                        dropdown_fg_color = "#000000")
    
    file_extension_button.place(relx = 0.20, rely = row3_y - 0.05, anchor = tkinter.CENTER)
    file_extension_menu.place(relx = 0.20, rely = row3_y, anchor = tkinter.CENTER)

def place_video_extension_menu():
    video_extension_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "Video output",
                              height   = 23,
                              width    = 130,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_video_extension)

    video_extension_menu = CTkOptionMenu(master  = window, 
                                    values     = video_extension_list,
                                    width      = 140,
                                    font       = bold11,
                                    height     = 30,
                                    fg_color   = "#000000",
                                    anchor     = "center",
                                    dynamic_resizing = False,
                                    command    = select_video_extension_from_menu,
                                    dropdown_font = bold11,
                                    dropdown_fg_color = "#000000")
    
    video_extension_button.place(relx = 0.5, rely = row1_y - 0.05, anchor = tkinter.CENTER)
    video_extension_menu.place(relx = 0.5, rely = row1_y, anchor = tkinter.CENTER)

def place_gpu_menu():
    AI_device_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "GPU",
                              height   = 23,
                              width    = 130,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_device)

    AI_device_menu = CTkOptionMenu(master  = window, 
                                    values   = device_list_names,
                                    width      = 140,
                                    font       = bold9,
                                    height     = 30,
                                    fg_color   = "#000000",
                                    anchor     = "center",
                                    dynamic_resizing = False,
                                    command    = select_AI_device_from_menu,
                                    dropdown_font = bold11,
                                    dropdown_fg_color = "#000000")
    
    AI_device_button.place(relx = 0.5, rely = row2_y - 0.05, anchor = tkinter.CENTER)
    AI_device_menu.place(relx = 0.5, rely  = row2_y, anchor = tkinter.CENTER)

def place_vram_textbox():
    vram_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "GPU Vram (GB)",
                              height   = 23,
                              width    = 130,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_vram_limiter)

    vram_textbox = CTkEntry(master      = window, 
                            width      = 140,
                            font       = bold11,
                            height     = 30,
                            fg_color   = "#000000",
                            textvariable = selected_VRAM_limiter)
    
    vram_button.place(relx = 0.5, rely = row3_y - 0.05, anchor = tkinter.CENTER)
    vram_textbox.place(relx = 0.5, rely  = row3_y, anchor = tkinter.CENTER)

def place_message_label():
    message_label = CTkLabel(master  = window, 
                            textvariable = info_message,
                            height       = 25,
                            font         = bold10,
                            fg_color     = "#ffbf00",
                            text_color   = "#000000",
                            anchor       = "center",
                            corner_radius = 25)
    message_label.place(relx = 0.8, rely = 0.56, anchor = tkinter.CENTER)

def place_cpu_textbox():
    cpu_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "CPU number",
                              height   = 23,
                              width    = 130,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_cpu)

    cpu_textbox = CTkEntry(master    = window, 
                            width      = 140,
                            font       = bold11,
                            height     = 30,
                            fg_color   = "#000000",
                            textvariable = selected_cpu_number)

    cpu_button.place(relx = 0.8, rely = row1_y - 0.05, anchor = tkinter.CENTER)
    cpu_textbox.place(relx = 0.8, rely  = row1_y, anchor = tkinter.CENTER)

def place_input_resolution_textbox():
    resize_factor_button = CTkButton(master  = window, 
                              fg_color   = "black",
                              text_color = "#ffbf00",
                              text     = "Input resolution (%)",
                              height   = 23,
                              width    = 130,
                              font     = bold11,
                              corner_radius = 25,
                              anchor  = "center",
                              command = open_info_resize)

    resize_factor_textbox = CTkEntry(master    = window, 
                                    width      = 140,
                                    font       = bold11,
                                    height     = 30,
                                    fg_color   = "#000000",
                                    textvariable = selected_resize_factor)
    
    resize_factor_button.place(relx = 0.80, rely = row2_y - 0.05, anchor = tkinter.CENTER)
    resize_factor_textbox.place(relx = 0.80, rely = row2_y, anchor = tkinter.CENTER)

def place_loadFile_section():

    text_drop = """ - SUPPORTED FILES -

IMAGES - jpg png tif bmp webp
VIDEOS - mp4 webm mkv flv gif avi mov mpg qt 3gp"""

    input_file_text = CTkLabel(master    = window, 
                                text     = text_drop,
                                fg_color = dark_color,
                                bg_color = dark_color,
                                width   = 300,
                                height  = 150,
                                font    = bold12,
                                anchor  = "center")
    
    input_file_button = CTkButton(master = window, 
                                width    = 140,
                                height   = 30,
                                text     = "SELECT FILES", 
                                font     = bold11,
                                border_spacing = 0,
                                command        = open_files_action)

    input_file_text.place(relx = 0.5, rely = 0.22,  anchor = tkinter.CENTER)
    input_file_button.place(relx = 0.5, rely = 0.385, anchor = tkinter.CENTER)



class App():
    def __init__(self, window):
        window.title('')
        width        = 650
        height       = 600
        window.geometry("650x600")
        window.minsize(width, height)
        window.iconbitmap(find_by_relative_path("Assets" + os.sep + "logo.ico"))

        place_up_background()

        place_app_name()
        place_itch_button()
        place_github_button()
        place_telegram_button()

        place_AI_menu()
        place_AI_mode_menu()
        place_image_extension_menu()

        place_video_extension_menu()
        place_gpu_menu()
        place_vram_textbox()
        
        place_message_label()
        place_input_resolution_textbox()
        place_cpu_textbox()
        place_upscale_button()

        place_loadFile_section()

if __name__ == "__main__":
    multiprocessing.freeze_support()

    set_appearance_mode("Dark")
    set_default_color_theme("dark-blue")

    window = CTk() 

    global selected_file_list
    global selected_AI_model
    global half_precision
    global selected_AI_device 
    global selected_image_extension
    global selected_video_extension
    global tiles_resolution
    global resize_factor
    global cpu_number

    selected_file_list = []
    selected_AI_model  = AI_models_list[0]
    half_precision     = True
    selected_AI_device = 0

    selected_image_extension = image_extension_list[0]
    selected_video_extension = video_extension_list[0]

    info_message = tk.StringVar()
    selected_resize_factor  = tk.StringVar()
    selected_VRAM_limiter   = tk.StringVar()
    selected_cpu_number     = tk.StringVar()

    info_message.set("Hi :)")

    cpu_count = str(int(os.cpu_count()/2))

    selected_resize_factor.set("50")
    selected_VRAM_limiter.set("8")
    selected_cpu_number.set(cpu_count)

    bold8  = CTkFont(family = "Segoe UI", size = 8, weight = "bold")
    bold9  = CTkFont(family = "Segoe UI", size = 9, weight = "bold")
    bold10 = CTkFont(family = "Segoe UI", size = 10, weight = "bold")
    bold11 = CTkFont(family = "Segoe UI", size = 11, weight = "bold")
    bold12 = CTkFont(family = "Segoe UI", size = 12, weight = "bold")
    bold20 = CTkFont(family = "Segoe UI", size = 20, weight = "bold")
    bold21 = CTkFont(family = "Segoe UI", size = 21, weight = "bold")

    global stop_icon
    global clear_icon
    global play_icon
    global logo_itch
    global logo_git
    logo_git   = CTkImage(Image.open(find_by_relative_path("Assets" + os.sep + "github_logo.png")), size=(15, 15))
    logo_itch  = CTkImage(Image.open(find_by_relative_path("Assets" + os.sep + "itch_logo.png")),  size=(13, 13))
    logo_telegram = CTkImage(Image.open(find_by_relative_path("Assets" + os.sep + "telegram_logo.png")),  size=(15, 15))
    stop_icon  = CTkImage(Image.open(find_by_relative_path("Assets" + os.sep + "stop_icon.png")), size=(15, 15))
    play_icon  = CTkImage(Image.open(find_by_relative_path("Assets" + os.sep + "upscale_icon.png")), size=(15, 15))
    clear_icon = CTkImage(Image.open(find_by_relative_path("Assets" + os.sep + "clear_icon.png")), size=(15, 15))

    app = App(window)
    window.update()
    window.mainloop()