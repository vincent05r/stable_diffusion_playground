"""Script for generating images via stable diffusion.

The script supports 3 modes of execution:
* GENERATE_DIVERSE - Generate `num_imgs` diverse images given a `prompt`.
* REPRODUCE - Reproduce an image given its `src_latent_path` and `metadata_path`.
* INTERPOLATE - Choose 2 images (via `(src|trg)_latent_path`) and interpolate betweeen them.

Note:
    * You'll have to run "huggingface-cli login" the first time so that you can access the model weights.
    * The script currently works only with jpg images. Exif package doesn't play nicely with png, etc.
"""

import enum
import functools
import json
import os

from diffusers import StableDiffusionPipeline
from diffusers.schedulers import DDIMScheduler, LMSDiscreteScheduler, PNDMScheduler
import exif
import fire
import matplotlib.pyplot as plt
import numpy as np
import cv2 as cv
import torch
from torch import autocast


class ExecutionMode(enum.Enum):
    GENERATE_DIVERSE = 0,  # Generate a set of diverse images given a prompt.
    REPRODUCE = 1,  # Reproduce an image given its latent (`src_latent_path`) and metadata (`metadata_path`)
    INTERPOLATE = 2  # Pick 2 images (via (src|trg)_latent_path) and interpolate betweeen them.


EXIF_KEY = 'user_comment'  # This is where we store metadata if `save_metadata_to_img` set to True.


def interpolate(t, v0, v1, DOT_THRESHOLD=0.9995):
    """Helper function to (spherically) interpolate two arrays v1 v2.
    
    Taken from: https://gist.github.com/karpathy/00103b0037c5aaea32fe1da1af553355
    """

    if not isinstance(v0, np.ndarray):
        inputs_are_torch = True
        input_device = v0.device
        v0 = v0.cpu().numpy()
        v1 = v1.cpu().numpy()

    dot = np.sum(v0 * v1 / (np.linalg.norm(v0) * np.linalg.norm(v1)))
    if np.abs(dot) > DOT_THRESHOLD:
        v2 = (1 - t) * v0 + t * v1
    else:
        theta_0 = np.arccos(dot)
        sin_theta_0 = np.sin(theta_0)
        theta_t = theta_0 * t
        sin_theta_t = np.sin(theta_t)
        s0 = np.sin(theta_0 - theta_t) / sin_theta_0
        s1 = sin_theta_t / sin_theta_0
        v2 = s0 * v0 + s1 * v1

    if inputs_are_torch:
        v2 = torch.from_numpy(v2).to(input_device)

    return v2


def generate_name(output_dir_path, suffix='jpg'):
    """Counts the number of files in `output_dir_path` and creates a filename based on that."""
    prefix = str(len(os.listdir(output_dir_path))).zfill(6)
    return f'{prefix}.{suffix}'


def extract_metadata(metadata_path):
    if metadata_path.endswith('.jpg'):
        with open(metadata_path, 'rb') as img_file:
            metadata = json.loads(exif.Image(img_file).get(EXIF_KEY))
    else:
        with open(metadata_path) as metadata_file:
            metadata = json.load(metadata_file)
    return metadata


def save_img_metadata(save_metadata_to_img, meta_dir, imgs_dir, image, prompt, num_inference_steps, guidance_scale):
    """Saves crucial metadata information to a json file or directly to an image."""
    metadata = {  # Feel free to add anything else you might need.
        'prompt': prompt,
        'num_steps': num_inference_steps,
        'scale': guidance_scale
    }

    if save_metadata_to_img:  # Store metadata directly inside of the image. 🧠 :)
        metadata_str = json.dumps(metadata)
        exif_img = exif.Image(cv.imencode('.jpg', np.asarray(image)[...,::-1])[1].tobytes())  # Check out https://exif.readthedocs.io/en/latest/usage.html
        exif_img.user_comment = metadata_str  # Saving the generation metadata into the user_comment field. :)
        with open(os.path.join(imgs_dir, generate_name(imgs_dir, suffix='jpg')), 'wb') as img_file:
            img_file.write(exif_img.get_file())

    else:  # Separately save image and metadata.
        image.save(os.path.join(imgs_dir, generate_name(imgs_dir, suffix='jpg')))
        with open(os.path.join(meta_dir, generate_name(meta_dir, suffix='json')), 'w') as metadata_file:
            json.dump(metadata, metadata_file)


def generate_images(
        # all the meta data
        output_dir_name='test_fp',  # Name of the output directory.
        execution_mode=ExecutionMode.GENERATE_DIVERSE,  # Choose between diverse generation and interpolation.
        num_imgs=2,  # How many images you want to generate in this run.
        
        ##### main args for controlling the generation #####
        prompt="cyberpunk city",  # Unleash your inner neural network whisperer.
        num_inference_steps=200,  # More (e.g. 100, 200 etc) can create slightly better images.
        guidance_scale=7.5,  # Complete black magic. Usually somewhere between 3-10 is good - but experiment!
        seed=42,  # 23 ,42 feels good

        width=512,  # Make sure it's a multiple of 8.
        height=512,
        src_latent_path=None,  # Set the latent of the 2 images you like (useful for INTERPOLATE mode).
        trg_latent_path=None,
        metadata_path=None,  # Used only in the REPRODUCE mode.

        ##### you'll set this one once and never touch it again depending on your HW #####
        fp16=False,  # Set to True unless you have ~16 GBs of VRAM.
        save_metadata_to_img=True,  # If False we'll save metadata in a separate file otherwise we store it inside of the image.
):
    assert torch.cuda.is_available(), "You need a GPU to run this script."
    assert height % 8 == 0 and width % 8 == 0, f"Width and height need to be a multiple of 8, got (w,h)=({width},{height})."
    device = "cuda"
    if seed:  # If you want to have consistent runs, otherwise set to None.
        torch.manual_seed(seed)

    # Initialize the output file structure.
    root_dir = os.path.join(os.getcwd(), 'output', output_dir_name)
    imgs_dir = os.path.join(root_dir, "imgs")
    latents_dir = os.path.join(root_dir, "latents")
    meta_dir = os.path.join(root_dir, "metadata")
    os.makedirs(imgs_dir, exist_ok=True)
    os.makedirs(latents_dir, exist_ok=True)
    if not save_metadata_to_img:  # Only create metadata dir if we need it.
        os.makedirs(meta_dir, exist_ok=True)

    # So that we don't have to pass these each time - they don't change...just a syntactic sugar.
    save_img_metadata_short = functools.partial(save_img_metadata, save_metadata_to_img, meta_dir, imgs_dir)

    # Hardcoded the recommended scheduler - feel free to play with it.
    lms = LMSDiscreteScheduler(beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear")

    #my_access_token = "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"

    # Create diffusion pipeline object.
    pipe = StableDiffusionPipeline.from_pretrained(
        "CompVis/stable-diffusion-v1-4",  # Recommended as the best model weights (more here: https://huggingface.co/CompVis).
        torch_dtype=torch.float16 if fp16 else None,
        revision="fp16" if fp16 else "main",
        scheduler=lms,
        use_auth_token=True  # You'll have to login the 1st time you run this script, run "huggingface-cli login".
    ).to(device)

    if execution_mode == execution_mode.GENERATE_DIVERSE:
        for i in range(num_imgs):
            print(f'Generating {i+1}. image.')
            init_latent = torch.randn((1, pipe.unet.in_channels, height // 8, width // 8), device=device)

            with autocast(device):
                image = pipe(  # Diffuse magic.
                    prompt,
                    num_inference_steps=num_inference_steps,
                    latents=init_latent,
                    guidance_scale=guidance_scale
                )["sample"][0]

            # Make sure generation is reproducible by saving the latent and metadata.
            # TODO: is there some clever python mechanism that can enable me to automatically fetch all input arg names & passed values?
            # Couldn't find anything in inspect...
            save_img_metadata_short(image, prompt, num_inference_steps, guidance_scale)
            np.save(os.path.join(latents_dir, generate_name(latents_dir, suffix='npy')), init_latent.cpu().numpy())

    elif execution_mode == execution_mode.INTERPOLATE:
        if src_latent_path and trg_latent_path:
            print('Loading existing source and target latents and interpolating between them!')
            src_init = torch.from_numpy(np.load(src_latent_path)).to(device)
            trg_init = torch.from_numpy(np.load(trg_latent_path)).to(device)
        else:
            print('Generating random source and target latents and interpolating between them!')
            src_init = torch.randn((1, pipe.unet.in_channels, height // 8, width // 8), device=device)
            trg_init = torch.randn((1, pipe.unet.in_channels, height // 8, width // 8), device=device)

            np.save(os.path.join(latents_dir, generate_name(latents_dir, suffix='npy')), src_init.cpu().numpy())
            np.save(os.path.join(latents_dir, generate_name(latents_dir, suffix='npy')), trg_init.cpu().numpy())
        
        for i, t in enumerate(np.concatenate([[0], np.linspace(0, 1, num_imgs)])):
            if i == 0:
                init_latent = trg_init  # Make sure you're happy with the target image before you waste too much time.
            else:
                init_latent = interpolate(float(t), src_init, trg_init)

            print(f'Generating {i+1}. image.')
            with autocast(device):
                image = pipe(
                    prompt,
                    num_inference_steps=num_inference_steps,
                    latents=init_latent,
                    guidance_scale=guidance_scale
                )["sample"][0]

            # Make sure generation is reproducible.
            save_img_metadata_short(image, prompt, num_inference_steps, guidance_scale)

    elif execution_mode == execution_mode.REPRODUCE:        
        assert src_latent_path, 'You need to provide the latent path if you wish to reproduce an image.'
        assert metadata_path, 'You need to provide the metadata file/image with metadata if you wish to reproduce an image.'

        metadata = extract_metadata(metadata_path)
        print(f'Found metadata info:\n\n{metadata}')
        init_latent = torch.from_numpy(np.load(src_latent_path)).to(device)

        with autocast(device):
            image = pipe(
                **metadata,
                latents=init_latent,
                output_type='npy', # As long as it's not pil it'll return numpy with the current imp (0.2.4) of StableDiffusionPipeline.
            )["sample"][0]

        plt.imshow((image * 255).astype(np.uint8))
        plt.show()
    else:
        print(f'Execution mode {execution_mode} not supported.')


if __name__ == '__main__':
    # Fire makes things much more concise than using argparse! :))
    # E.g. if there is an argument in generate_images func with name <arg_name> then you can call:
    # python generate_images.py --<arg_name> <arg_value>
    fire.Fire(generate_images)