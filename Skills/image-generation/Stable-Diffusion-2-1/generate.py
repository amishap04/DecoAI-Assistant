"""CLI entrypoint: run the full Stable Diffusion 2.1 QNN pipeline end to end
via ONNX Runtime's QNN execution provider and save the generated image.

Requires session_server.py to already be running (it holds the pre-loaded
ORT QNN sessions so this process never pays the context-binary load cost).
"""

import argparse

import numpy as np
import torch
from PIL import Image

from qnn_runner import run_text_encoder, run_unet, run_vae
from scheduler import get_init_noise_sigma, get_timestep, run_scheduler, set_num_steps
from tokenizer import run_tokenizer

def _stats(name, arr):
    arr = np.asarray(arr)
    print(f"    {name}: shape={arr.shape} min={arr.min():.4f} max={arr.max():.4f} mean={arr.mean():.4f} std={arr.std():.4f}")


def generate(prompt, negative_prompt, seed, steps, guidance_scale, output_path):
    set_num_steps(steps)
    uncond_tokens = run_tokenizer(negative_prompt)
    cond_tokens = run_tokenizer(prompt)

    uncond_text_embedding = run_text_encoder(uncond_tokens)
    cond_text_embedding = run_text_encoder(cond_tokens)
    _stats("uncond_text_embedding", uncond_text_embedding)
    _stats("cond_text_embedding", cond_text_embedding)

    random_init_latent = torch.randn((1, 4, 64, 64), generator=torch.manual_seed(seed)).numpy()
    latent_in = (random_init_latent * get_init_noise_sigma()).transpose((0, 2, 3, 1)).copy()
    _stats("initial latent", latent_in)

    for step in range(steps):
        print(f"Step {step + 1}/{steps}")
        timestep = get_timestep(step)
        print(f"    timestep={timestep}")

        unconditional_noise_pred = run_unet(latent_in, timestep, uncond_text_embedding)
        conditional_noise_pred = run_unet(latent_in, timestep, cond_text_embedding)
        _stats("unconditional_noise_pred", unconditional_noise_pred)
        _stats("conditional_noise_pred", conditional_noise_pred)

        latent_in = run_scheduler(
            unconditional_noise_pred, conditional_noise_pred, latent_in, timestep, guidance_scale
        )
        _stats("latent_in (post-step)", latent_in)

    output_image = run_vae(latent_in)
    _stats("output_image", output_image)
    Image.fromarray(output_image, mode="RGB").save(output_path)
    print(f"Saved image to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Stable Diffusion 2.1 image generation via ORT QNN")
    parser.add_argument("--prompt", required=True, help="Text prompt")
    parser.add_argument("--negative-prompt", default="", help="Negative/unconditional prompt")
    parser.add_argument("--seed", type=int, default=4, help="Random seed for the initial latent")
    parser.add_argument("--steps", type=int, default=20, choices=[20, 50], help="Number of denoising steps")
    parser.add_argument("--guidance-scale", type=float, default=7.5, help="Classifier-free guidance scale, in [5.0, 15.0]")
    parser.add_argument("--output", default="output.png", help="Path to save the generated PNG")
    args = parser.parse_args()

    assert 5.0 <= args.guidance_scale <= 15.0, "guidance-scale must be in [5.0, 15.0]"

    generate(
        prompt=args.prompt,
        negative_prompt=args.negative_prompt,
        seed=np.int64(args.seed),
        steps=args.steps,
        guidance_scale=args.guidance_scale,
        output_path=args.output,
    )


if __name__ == "__main__":
    main()
