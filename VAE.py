# Copyright 2019 Stanislav Pidhorskyi
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#  http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import print_function
import torch.utils.data
from scipy import misc
from torch import optim
from torchvision.utils import save_image
from net import *
import numpy as np
import pickle
import time
import random
import os
from dlutils import batch_provider
from dlutils.pytorch.cuda_helper import *
from dlutils.pytorch import count_parameters

im_size = 128


def save_model(x, name):
    if isinstance(x, nn.DataParallel):
        torch.save(x.module.state_dict(), name)
    else:
        torch.save(x.state_dict(), name)


def loss_function(recon_x, x):
    return torch.mean((recon_x - x)**2)


def process_batch(batch):
    data = [x.transpose((2, 0, 1)) for x in batch]
    x = torch.tensor(np.asarray(data, dtype=np.float32), requires_grad=True).cuda() / 127.5 - 1.
    return x

if torch.cuda.device_count() == 4:
    #              4x4  8x8 16x16  32x32  64x64  128x128
    lod_2_batch = [512, 256, 128,   128,   128,    128]
elif torch.cuda.device_count() == 2:
    #              4x4  8x8 16x16  32x32  64x64  128x128
    lod_2_batch = [512, 256, 128,   128,   128,     64]
elif torch.cuda.device_count() == 1:
    #              4x4  8x8 16x16  32x32  64x64  128x128
    lod_2_batch = [512, 256, 128,   128,    64,     32]


def D_logistic_simplegp(d_result_fake, d_result_real, reals, r1_gamma=10.0):
    loss = (F.softplus(d_result_fake) + F.softplus(-d_result_real)).mean()

    if r1_gamma != 0.0:
        real_loss = d_result_real.sum()
        real_grads = torch.autograd.grad(real_loss, reals, create_graph=True, retain_graph=True)[0]
        r1_penalty = torch.sum(real_grads.pow(2.0), dim=[1,2,3])
        loss = loss + r1_penalty.mean() * (r1_gamma * 0.5)
    return loss

    
def G_logistic_nonsaturating(d_result_fake):
    return F.softplus(-d_result_fake).mean()

    
def main(parallel=False):
    layer_count = 6
    epochs_per_lod = 6
    latent_size = 128

    autoencoder = Autoencoder(layer_count=layer_count, maxf=128, latent_size=latent_size, channels=3)
    autoencoder.cuda()
    autoencoder.train()
    #vae.weight_init(mean=0, std=0.02)

    discriminator = Discriminator(layer_count=layer_count, maxf=128, channels=3)
    discriminator.cuda()
    discriminator.train()
    #discriminator.weight_init(mean=0, std=0.02)

    mapping = Mapping(num_layers=2 * layer_count, latent_size=latent_size, dlatent_size=latent_size, mapping_fmaps=latent_size)
    mapping.cuda()
    mapping.train()
    #mapping.weight_init(mean=0, std=0.02)


    #autoencoder.load_state_dict(torch.load("VAEmodel.pkl"))

    print("Trainable parameters autoencoder:")
    count_parameters(autoencoder)

    print("Trainable parameters mapping:")
    count_parameters(mapping)

    print("Trainable parameters discriminator:")
    count_parameters(discriminator)

    if parallel:
        autoencoder = nn.DataParallel(autoencoder)
        discriminator = nn.DataParallel(discriminator)
        autoencoder.layer_to_resolution = autoencoder.module.layer_to_resolution

    lr = 0.001
    lr2 = 0.001

    autoencoder_optimizer = optim.Adam([
        {'params': autoencoder.parameters()},
        {'params': mapping.parameters(), 'lr': lr * 0.01}
    ], lr=lr, betas=(0.0, 0.99), weight_decay=0)

    discriminator_optimizer = optim.Adam(discriminator.parameters(), lr=lr2, betas=(0.0, 0.99), weight_decay=0)
 
    train_epoch = 45

    #sample = torch.randn(32, latent_size).view(-1, latent_size)

    with open('data_selected.pkl', 'rb') as pkl:
        data_train = pickle.load(pkl)
        sample = process_batch(data_train[:32])
        del data_train

    lod = -1
    in_transition = False

    for epoch in range(train_epoch):
        autoencoder.train()
        discriminator.train()

        new_lod = min(layer_count - 1, epoch // epochs_per_lod)
        if new_lod != lod:
            lod = new_lod
            print("#" * 80, "\n# Switching LOD to %d" % lod, "\n" + "#" * 80)
            print("Start transition")
            in_transition = True

        new_in_transition = (epoch % epochs_per_lod) < (epochs_per_lod // 2) and lod > 0 and epoch // epochs_per_lod == lod
        if new_in_transition != in_transition:
            in_transition = new_in_transition
            print("#" * 80, "\n# Transition ended", "\n" + "#" * 80)

        with open('../VAE/data_fold_%d_lod_%d.pkl' % (epoch % 5, lod), 'rb') as pkl:
            data_train = pickle.load(pkl)

        print("Train set size:", len(data_train))
        data_train = data_train[:4 * (len(data_train) // 4)]

        random.shuffle(data_train)

        batches = batch_provider(data_train, lod_2_batch[lod], process_batch, report_progress=True)

        rec_loss = []
        d_loss = []
        g_loss = []

        epoch_start_time = time.time()

        if (epoch + 1) == 35:
            autoencoder_optimizer.param_groups[0]['lr'] = lr / 4
            #discriminator_optimizer.param_groups[0]['lr'] = lr2 / 4
            print("learning rate change!")
        if (epoch + 1) == 40:
            autoencoder_optimizer.param_groups[0]['lr'] = lr / 4 / 4
            #discriminator_optimizer.param_groups[0]['lr'] = lr2 / 4 / 4
            print("learning rate change!")

        i = 0
        for x_orig in batches:
            if x_orig.shape[0] != lod_2_batch[lod]:
                continue
            autoencoder.train()
            discriminator.train()
            autoencoder.zero_grad()
            discriminator.zero_grad()

            blend_factor = float((epoch % epochs_per_lod) * len(data_train) + i) / float(epochs_per_lod // 2 * len(data_train))
            if not in_transition:
                blend_factor = 1

            needed_resolution = autoencoder.layer_to_resolution[lod]
            x = x_orig

            if in_transition:
                needed_resolution_prev = autoencoder.layer_to_resolution[lod - 1]
                x_prev = F.interpolate(x_orig, needed_resolution_prev)
                x_prev_2x = F.interpolate(x_prev, needed_resolution)
                x = x * blend_factor + x_prev_2x * (1.0 - blend_factor)
            #
            # z = torch.randn(lod_2_batch[lod], latent_size).view(-1, latent_size)
            # w = mapping(z)
            #
            # rec = autoencoder(w, lod, blend_factor)
            #
            # d_result_real = discriminator(x, lod, blend_factor).squeeze()
            # d_result_fake = discriminator(rec.detach(), lod, blend_factor).squeeze()
            #
            # loss_d = D_logistic_simplegp(d_result_fake, d_result_real, x)
            # discriminator.zero_grad()
            # loss_d.backward()
            # d_loss += [loss_d.item()]
            #
            # discriminator_optimizer.step()
            
            ############################################################
            # autoencoder.zero_grad()
            #
            # z = torch.randn(lod_2_batch[lod], latent_size).view(-1, latent_size)
            # w = mapping(z)
            #
            # rec = autoencoder.forward(w, lod, blend_factor)
            #
            # #loss_re = loss_function(rec, x)
            # #rec_loss += [loss_re.item()]
            #
            # d_result_fake = discriminator(rec, lod, blend_factor).squeeze()
            # loss_g = G_logistic_nonsaturating(d_result_fake)
            # loss_g.backward()
            # g_loss += [loss_g.item()]
            #
            # vae_optimizer.step()
            
            #kl_loss += loss_kl.item()

            #############################################

            autoencoder.zero_grad()
            rec = autoencoder(x, lod, blend_factor)
            loss_re = loss_function(rec, x)
            rec_loss += [loss_re.item()]
            loss_re.backward()
            autoencoder_optimizer.step()

            epoch_end_time = time.time()
            per_epoch_ptime = epoch_end_time - epoch_start_time
            
            def avg(lst): 
                if len(lst) == 0:
                    return 0
                return sum(lst) / len(lst) 
                
            # report losses and save samples each 60 iterations
            m = 7680 * 2
            i += lod_2_batch[lod]
            if i % m == 0:
                os.makedirs('results', exist_ok=True)
                rec_loss = avg(rec_loss)
                #kl_loss = avg(kl_loss)
                g_loss = avg(g_loss)
                d_loss = avg(d_loss)
                print('\n[%d/%d] - ptime: %.2f, rec loss: %.9f, g loss: %.9f, d loss: %.9f' % (
                    (epoch + 1), train_epoch, per_epoch_ptime, rec_loss, g_loss, d_loss))
                g_loss = []
                d_loss = []
                rec_loss = []
                kl_loss = []
                with torch.no_grad():
                    autoencoder.eval()
                    sample_in = F.interpolate(sample, needed_resolution)
                    rec = autoencoder(sample_in, lod, blend_factor)
                    resultsample = torch.cat([sample_in, rec], dim=0) * 0.5 + 0.5
                    resultsample = resultsample.cpu()
                    save_image(resultsample.view(-1, 3, needed_resolution, needed_resolution),
                               'results/sample_' + str(epoch) + "_" + str(i // lod_2_batch[lod]) + '.png', nrow=8)
                    # w = list(mapping(sample))
                    # x_rec = autoencoder(w, lod, blend_factor)
                    # resultsample = x_rec * 0.5 + 0.5
                    # resultsample = resultsample.cpu()
                    # save_image(resultsample.view(-1, 3, needed_resolution, needed_resolution),
                    #            'results_rec/sample_' + str(epoch) + "_" + str(i // lod_2_batch[lod]) + '.png', nrow=8)
                    #x_rec = vae.decode(sample1)
                    #resultsample = x_rec * 0.5 + 0.5
                    #resultsample = resultsample.cpu()
                    #save_image(resultsample.view(-1, 3, im_size, im_size),
                    #           'results_gen/sample_' + str(epoch) + "_" + str(i) + '.png')

        del batches
        del data_train
        save_model(autoencoder, "autoencoder_tmp.pkl")
    print("Training finish!... save training results")
    save_model(autoencoder, "autoencoder.pkl")

if __name__ == '__main__':
    main(True)