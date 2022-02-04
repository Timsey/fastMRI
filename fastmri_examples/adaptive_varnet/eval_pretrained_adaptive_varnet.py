import pathlib
from argparse import ArgumentParser

import fastmri
import numpy as np
import pytorch_lightning as pl
import torch

from fastmri import evaluate
from fastmri.data.mri_data import fetch_dir
from fastmri.data.transforms import MiniCoilTransform
from fastmri.pl_modules import FastMriDataModule

from pl_modules import AdaptiveVarNetModule, VarNetModule
from subsample import create_mask_for_mask_type


def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif v.lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise ValueError("Boolean value expected.")


def entropy(prob_mask: torch.Tensor):
    ent = -(prob_mask * prob_mask.log() + (1 - prob_mask) * (1 - prob_mask).log())
    ent[prob_mask == 0] = 0
    ent[prob_mask == 1] = 0
    return ent


def load_model(
    module_class: pl.LightningModule,
    fname: pathlib.Path,
):
    print(f"loading model from {fname}")
    checkpoint = torch.load(fname, map_location=torch.device("cpu"))

    # Initialise model with stored params
    module = module_class(**checkpoint["hyper_parameters"])

    # Load stored weights: this will error if the keys don't match the model weights, which will happen
    #  when we are loading a VarNet instead of an AdaptiveVarNet or vice-versa.
    # TODO: Check that this works with models trained much earlier, where some extra parameters might exist even
    #  within class.
    module.load_state_dict(checkpoint["state_dict"])

    return module


def cli_main(args):
    pl.seed_everything(0)

    # ------------
    # data
    # ------------
    # this creates a k-space mask for transforming input data
    mask = create_mask_for_mask_type(
        args.mask_type,
        args.center_fractions,
        args.accelerations,
        args.skip_low_freqs,
    )

    # Assumes coil compression with 4 coils
    val_transform = MiniCoilTransform(
        mask_func=mask,
        num_compressed_coils=4,
        crop_size=args.crop_size,
    )

    data_module = FastMriDataModule(
        data_path=args.data_path,
        challenge=args.challenge,
        train_transform=val_transform,
        val_transform=val_transform,
        test_transform=val_transform,
        test_split=args.test_split,
        test_path=args.test_path,
        sample_rate=args.sample_rate,
        volume_sample_rate=args.volume_sample_rate,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
    )

    # ------------
    # model
    # ------------
    device = "cuda:0" if torch.cuda.is_available() else "cpu"

    try:
        # Try to load as AdaptiveVarNetModule, if this fails, then the model is probably a VarNetModule instead
        print("Trying to load as AdaptiveVarNetModule...")
        model = load_model(AdaptiveVarNetModule, args.load_checkpoint)
        print("... Success!")
    except RuntimeError:
        # If this still fails, then probably the state dict
        print(
            "Loading as AdaptiveVarNetModule failed, trying to load as VarNetModule..."
        )
        model = load_model(VarNetModule, args.load_checkpoint)
        print("... Success!")

    model.to(device)

    ssim_loss = fastmri.SSIMLoss()

    all_prob_masks = []
    all_ssims, all_psnrs, all_nmses = [], [], []
    data_loader = (
        data_module.val_dataloader()
        if args.data_mode == "val"
        else data_module.train_dataloader()
    )
    with torch.no_grad():
        for i, batch in enumerate(data_loader):
            if i == args.num_batches:
                break

            output, extra_outputs = model(
                batch.kspace.to(device),
                batch.masked_kspace.to(device),
                batch.mask.to(device),
            )

            # ----- SSIM calculation -----
            # Note that SSIMLoss computes average SSIM over the entire batch
            ssim = (
                1
                - ssim_loss(
                    output.unsqueeze(1).cpu(),
                    batch.target.unsqueeze(1),
                    data_range=batch.max_value,
                    reduced=False,
                ).mean(dim=(1, 2))
            )
            all_ssims.append(ssim.cpu())

            target = batch.target.numpy()
            output = output.cpu().numpy()

            for gt, rec, maxval in zip(target, output, batch.max_value.numpy()):
                psnr = evaluate.psnr(gt, rec, maxval=maxval)
                nmse = evaluate.nmse(gt, rec)
                all_psnrs.append(psnr)
                all_nmses.append(nmse)

            # Save prob_masks if they exist (not for VarNetModule)
            if "prob_masks" in extra_outputs:
                prob_masks_list = extra_outputs["prob_masks"]

                assert (
                    len(prob_masks_list) == 1
                ), "Found more than one prob mask... Multiple policies in this checkpoint? Not currently supported."
                batch_prob_masks = (
                    prob_masks_list[0][:, 0, 0, :, 0].detach().cpu()
                )  # b x 1 x 1 x 128 x 1 --> b x 128
                assert np.isclose(batch_prob_masks[0].sum(), model.budget), (
                    f"Sum of a prob mask should match budget {model.budget} "
                    f"but it was {batch_prob_masks[0].sum()}"
                )
                all_prob_masks.append(batch_prob_masks)

        # These are numpy arrays
        psnr_array = np.concatenate(np.array(all_psnrs)[:, None], axis=0)
        nmse_array = np.concatenate(np.array(all_nmses)[:, None], axis=0)
        ssim_tensor = torch.cat(all_ssims, dim=0)

        return_dict = {
            "ssim": ssim_tensor.mean().item(),
            "psnr": psnr_array.mean().item(),
            "nmse": nmse_array.mean().item(),
        }
        if all_prob_masks:
            # Each row sums to model.budget.
            prob_mask_tensor = torch.cat(all_prob_masks, dim=0).double()

            print(
                f"Computed {prob_mask_tensor.shape[0]} masks of size {prob_mask_tensor.shape[1]}"
            )

            marg_prob = prob_mask_tensor.mean(dim=0, keepdim=True)
            marg_entropy = entropy(marg_prob).sum(dim=1)
            avg_cond_entropy = entropy(prob_mask_tensor).sum(dim=1).mean()
            mut_inf = marg_entropy - avg_cond_entropy

            return_dict.update(
                {
                    "cond_ent_ind": avg_cond_entropy.item(),
                    "marg_ent_ind": marg_entropy.item(),
                    "mi_ind": mut_inf.item(),
                }
            )
        print(return_dict)


def build_args():
    parser = ArgumentParser()

    parser.add_argument(
        "--load_checkpoint",
        type=pathlib.Path,
        help="Model checkpoint to load.",
    )
    parser.add_argument(
        "--center_fractions",
        nargs="+",
        default=[0.08],
        type=float,
        help="Number of center lines to use in mask. "
        "0.08 for acceleration 4, 0.04 for acceleration 8 models.",
    )
    parser.add_argument(
        "--accelerations",
        nargs="+",
        default=[4],
        type=int,
        help="Acceleration rates to use.",
    )
    parser.add_argument(
        "--crop_size",
        default=(128, 128),
        type=int,
        nargs="+",
        help="Crop size used by checkpoint.",
    )
    parser.add_argument(
        "--num_batches",
        default=None,
        type=int,
    )
    parser.add_argument(
        "--data_mode",
        default="val",
        type=str,
        choices=["train", "val"],
    )
    parser.add_argument(
        "--skip_low_freqs",
        default=True,
        type=str2bool,
        help="Whether skip low-frequency lines when computing equispaced mask.",
    )

    parser = AdaptiveVarNetModule.add_model_specific_args(parser)

    # basic args
    path_config = pathlib.Path("../../fastmri_dirs.yaml")

    # set defaults based on optional directory config
    data_path = fetch_dir("knee_path", path_config)

    # data config
    parser = FastMriDataModule.add_data_specific_args(parser)
    parser.set_defaults(
        data_path=data_path,  # path to fastMRI data
        mask_type="adaptive_equispaced_fraction",  # VarNet uses equispaced mask
        challenge="multicoil",  # only multicoil implemented for VarNet
        batch_size=1,  # number of samples per batch
        test_path=None,  # path for test split, overwrites data_path
        num_workers=20,
    )

    args = parser.parse_args()
    assert (
        len(args.crop_size) == 2
    ), f"Crop size must be of length 2, not {len(args.crop_size)}."

    return args


def run_cli():
    args = build_args()

    # ---------------------
    # RUN TRAINING
    # ---------------------
    cli_main(args)


if __name__ == "__main__":
    run_cli()
