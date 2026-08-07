"""DPM-Solver++ scheduler for the Stable Diffusion 2.1 denoising loop.

This model's unet.onnx takes the raw integer `timestep` directly as an input
(see metadata.json) -- the time-embedding MLP is baked into the QNN context
binary, so no external sinusoidal time-embedding module is needed.
"""

import numpy as np
import torch
from diffusers import DPMSolverMultistepScheduler

_scheduler = DPMSolverMultistepScheduler(
    num_train_timesteps=1000,
    beta_start=0.00085,
    beta_end=0.012,
    beta_schedule="scaled_linear",
    prediction_type="v_prediction",
)


def set_num_steps(num_steps):
    _scheduler.set_timesteps(num_steps)


def get_timestep(step):
    return float(_scheduler.timesteps.numpy()[step])


def get_init_noise_sigma():
    return float(_scheduler.init_noise_sigma)


def run_scheduler(noise_pred_uncond, noise_pred_text, latent_in, timestep, guidance_scale):
    # NHWC -> NCHW
    noise_pred_uncond = torch.from_numpy(np.transpose(noise_pred_uncond, (0, 3, 1, 2)).copy())
    noise_pred_text = torch.from_numpy(np.transpose(noise_pred_text, (0, 3, 1, 2)).copy())
    latent_in = torch.from_numpy(np.transpose(latent_in, (0, 3, 1, 2)).copy())

    noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

    latent_out = _scheduler.step(noise_pred, int(round(timestep)), latent_in).prev_sample.numpy()

    # NCHW -> NHWC
    return np.transpose(latent_out, (0, 2, 3, 1)).copy()
