import argparse, os, sys, glob
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import torch
import numpy as np
from omegaconf import OmegaConf
from PIL import Image
from tqdm import tqdm, trange
from itertools import islice
from einops import rearrange
from torchvision.utils import make_grid, save_image
import time
from pytorch_lightning import seed_everything
from torch import autocast
from contextlib import contextmanager, nullcontext
from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler
from scripts.helpers import chunk, load_model_from_config
from scripts.helpers import sample as advanced_sample

def main(opt):

    print(f'\n step 1. check device')
    assert torch.cuda.is_available()
    device = "cuda"

    print(f'\n step 2. batch size')
    batch_size = opt.n_samples
    shape = [opt.C, opt.H // opt.f, opt.W // opt.f]

    print(f'\n step 3. laion400m checking')
    if opt.laion400m:
        print("Falling back to LAION 400M model...")
        opt.config = "configs/latent-diffusion/txt2img-1p4B-eval.yaml"
        opt.ckpt = "models/ldm/text2img-large/model.ckpt"
        opt.outdir = "outputs/txt2img-samples-laion400m"

    print(f'\n step 4. seed')
    seed_everything(opt.seed)

    print(f'\n step 5. load model')
    print(f' (5.1) base config')
    config = OmegaConf.load(f"{opt.config}")
    print(f' (5.2) personalized config (teddy bear)')
    personalized_ckpts = opt.personalized_ckpt.split(',')
    print(f' (5.3) number of personalized concept')
    n_concepts = len(personalized_ckpts)
    if n_concepts > 1:
        config.model.target = 'perfusion.perfusion.MultiConceptsPerfusion'
        config.model.params.n_concepts = n_concepts
    else:
        personalized_ckpts = personalized_ckpts[0]

    print(f'\n step 6. load model')
    # what is mean of beta and tau
    beta = [float(b) for b in opt.beta.split(',')]  # [0.7]
    tau = [float(t) for t in opt.tau.split(',')]  # [0.15]
    config.model.params.beta = beta if len(beta) > 1 else beta[0]  # beta = 0.7
    config.model.params.tau = tau if len(tau) > 1 else tau[0]  # tau = 0.15
    print(f' (6.1) loading model with personalized ckpt')
    model = load_model_from_config(config,
                                   opt.ckpt,
                                   personalized_ckpts)
    model = model.to(device)

    print(f'\n step 7. using new sampler?')
    if opt.advanced_sampler:
        sampler_config = OmegaConf.load(f"{opt.sampler_config}")
        sampler_config.params.num_steps = opt.steps
        sampler_config.params.guider_config.params.scale = opt.scale
        sampler = instantiate_from_config(sampler_config)
        denoiser_config = OmegaConf.load(f"{opt.denoiser_config}")
        denoiser = instantiate_from_config(denoiser_config).to(device)
        sample = lambda c, uc: (advanced_sample(model.apply_model, denoiser, sampler, c, uc, batch_size, shape, device))
    else:
        if opt.plms:
            sampler = PLMSSampler(model)
        else:
            sampler = DDIMSampler(model)
        sample = lambda c, uc: (sampler.sample(S=opt.steps,conditioning=c,batch_size=batch_size,
                                               shape=shape,verbose=False,
                                               unconditional_guidance_scale=opt.scale,
                                               unconditional_conditioning=uc,
                                               eta=opt.ddim_eta,)[0])

    print(f'\n step 8. where to save')
    os.makedirs(opt.outdir, exist_ok=True)
    outpath = opt.outdir

    print(f'\n step 9. display printed image')
    n_rows = opt.n_rows if opt.n_rows > 0 else batch_size

    print(f'\n step 10. check prompt')
    if not opt.from_file:
        prompt = opt.prompt
        assert prompt is not None
        data = [batch_size * [prompt]]
    else:
        print(f"reading prompts from {opt.from_file}")
        with open(opt.from_file, "r") as f:
            data = f.read().splitlines()
            data = list(chunk(data, batch_size))
    print(f' (10.1) base prompt data : {data}')

    print(f' (10.2) prompts with placeholder word')
    placeholders = list(model.embedding_manager.string_to_token_dict.keys())
    print(f' - presaved placeholders : {placeholders}')
    superclasses = model.embedding_manager.initializer_words
    print(f' - presaved superclass : {superclasses}')


    data_concept = list()
    data_superclass = list()

    for i in range(len(data)):
        data_concept.append(list())
        data_superclass.append(list())
        for j in range(len(data[i])):
            prompt_concept, prompt_superclass = data[i][j], data[i][j]
            for concept_i in range(n_concepts):
                target = f'{{{concept_i + 1}}}' if n_concepts > 1 else '{}'
                prompt_concept = prompt_concept.replace(target, placeholders[concept_i])
                prompt_superclass = prompt_superclass.replace(target, superclasses[concept_i])
                # prompt_concept : photo of a *
                # prompt_superclass : photo of a teddy
            data_concept[i].append(prompt_concept)
            data_superclass[i].append(prompt_superclass)

    sample_path = os.path.join(outpath, "samples")
    os.makedirs(sample_path, exist_ok=True)
    base_count = len(os.listdir(sample_path))
    grid_count = len(os.listdir(outpath)) - 1

    start_code = None
    if opt.fixed_code:
        start_code = torch.randn([opt.n_samples, opt.C, opt.H // opt.f, opt.W // opt.f], device=device)

    precision_scope = autocast if opt.precision == "autocast" else nullcontext

    with torch.no_grad():
        with precision_scope(device):
            with model.ema_scope():
                tic = time.time()
                all_samples = list()
                for n in trange(opt.n_iter, desc="Sampling"):
                    for data_i in tqdm(range(len(data_concept)), desc="data"):
                        prompts = data_concept[data_i]
                        prompts_superclass = data_superclass[data_i] if opt.global_locking else None
                        # prompts : photo of a *
                        # prompts_superclass : photo of a teddy


                        # ------------------------------------------------------------------------------------------------
                        # make unconditional condition
                        uc = None
                        if opt.scale != 1.0:
                            encoding_uc = model.get_learned_conditioning(batch_size * [""])
                            uc = dict(c_crossattn=encoding_uc,
                                      c_super=encoding_uc if opt.global_locking else None)

                        # ------------------------------------------------------------------------------------------------
                        # check prompt
                        if isinstance(prompts, tuple):
                            prompts = list(prompts)

                        print(f' prompts right before sampling, prompts : {prompts}')

                        # ------------------------------------------------------------------------------------------------
                        # get text embedding
                        # (1) prompt embedding
                        encoding = model.cond_stage_model.encode(prompts,
                                                                 embedding_manager=model.embedding_manager)
                        # (2) superclass embedding
                        encoding_superclass = model.get_learned_conditioning(prompts_superclass) if opt.global_locking else None
                        c = dict(c_crossattn=encoding,
                                 c_super=encoding_superclass)

                        # ------------------------------------------------------------------------------------------------
                        # (3) sampling (ddpm)
                        print(f'sample : {sample.__class__.__name__}')
                        z_samples = sample(c,
                                           uc)

                        # ------------------------------------------------------------------------------------------------
                        # decoding after all timestep repeating loop
                        x_samples = model.decode_first_stage(z_samples)
                        x_samples = torch.clamp((x_samples + 1.0) / 2.0, min=0.0, max=1.0)

                        if not opt.skip_save:
                            for x_sample in x_samples:
                                x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                                Image.fromarray(x_sample.astype(np.uint8)).save(
                                    os.path.join(sample_path, f"{base_count:05}.jpg"))
                                base_count += 1
                        if not opt.skip_grid:
                            all_samples.append(x_samples)

                if not opt.skip_grid:
                    # additionally, save as grid
                    grid = torch.stack(all_samples, 0)
                    grid = rearrange(grid, 'n b c h w -> (n b) c h w')

                    for i in range(grid.size(0)):
                        save_image(grid[i, :, :, :], os.path.join(outpath, opt.prompt + '_{}.png'.format(i)))
                    grid = make_grid(grid, nrow=n_rows)

                    # to image
                    grid = 255. * rearrange(grid, 'c h w -> h w c').cpu().numpy()
                    Image.fromarray(grid.astype(np.uint8)).save(
                        os.path.join(outpath, f'{prompt.replace(" ", "-")}-{grid_count:04}.jpg'))
                    grid_count += 1

                toc = time.time()

    print(f"Your samples are ready and waiting for you here: \n{outpath} \n"
          f" \nEnjoy.")
    

if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    # step 2. batch size
    parser.add_argument("--n_samples", type=int, default=4,
                        help="how many samples to produce for each given prompt. A.k.a. batch size", )
    parser.add_argument("--C", type=int, default=4, help="latent channels", )
    parser.add_argument("--H", type=int, default=512, help="image height, in pixel space", )
    parser.add_argument("--W", type=int, default=512, help="image width, in pixel space", )
    parser.add_argument("--f", type=int, default=8, help="downsampling factor", )

    # step 3.
    parser.add_argument("--laion400m", action='store_true', help="uses the LAION400M model", )

    # step 4.
    parser.add_argument("--seed", type=int, default=42, help="the seed (for reproducible sampling)", )

    # step 5. load model
    parser.add_argument("--config", type=str, default="configs/perfusion_inference.yaml",
                        help="path to config which constructs model", )
    parser.add_argument("--ckpt", default="", help="path to checkpoint of model", )
    parser.add_argument("--personalized_ckpt", type=str, default='./ckpt/teddy.ckpt',
                        help="Paths to a pre-trained personalized checkpoint. With the form 'ckpt1,ckpt2,...'")

    # step 6.
    parser.add_argument("--beta", type=str, default="0.7", help="bias used in gated rank-1 editing", )
    parser.add_argument("--tau", type=str, default="0.15", help="temperature used in gated rank-1 editing", )

    # step 7. using new sampler
    parser.add_argument("--advanced_sampler", action="store_true",
                        help="use other advanced sampler through the sampler and denoiser configs.")
    parser.add_argument("--sampler_config", type=str,
                        default="configs/sampler/sampler.yaml", help="path to config which constructs sampler", )
    parser.add_argument("--steps", type=int, default=50, help="number of sampling steps", )
    parser.add_argument("--scale", type=float, default=7.5,
                        help="unconditional guidance scale: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))", )
    parser.add_argument("--denoiser_config", type=str, default="configs/denoiser/denoiser.yaml",
                        help="path to config which constructs sampler", )
    parser.add_argument("--plms", action='store_true', help="use plms sampling", )

    # step 8.
    parser.add_argument("--outdir", type=str, nargs="?", help="dir to write results to",
                        default="outputs/txt2img-samples")

    # step 9. display printed image
    parser.add_argument("--n_rows", type=int, default=0, help="rows in the grid (default: n_samples)", )

    # step 10.
    parser.add_argument("--from-file", type=str, help="if specified, load prompts from this file", )
    parser.add_argument("--prompt", type=str, nargs="?", default="photo of a {}",
                        help="the prompt to render. Use {n} to distinguish different concepts.")

    # step 11.
    parser.add_argument("--skip_grid",action='store_true',
                        help="do not save a grid, only individual samples. Helpful when evaluating lots of samples",)
    parser.add_argument("--skip_save",action='store_true',
                        help="do not save individual samples. For speed measurements.",)
    parser.add_argument("--fixed_code",action='store_true',help="if enabled, uses the same starting code across samples ",)
    parser.add_argument("--ddim_eta",type=float,default=0.0,help="ddim eta (eta=0.0 corresponds to deterministic sampling",)
    parser.add_argument("--n_iter",type=int,default=2,help="sample this often",)
    parser.add_argument("--precision",type=str,help="evaluate at this precision",
                        choices=["full", "autocast"],default="autocast")
    parser.add_argument("--global_locking", action="store_true",
                        help="the superclass word for global locking. None for disable.")
    opt = parser.parse_args()
    main(opt)


