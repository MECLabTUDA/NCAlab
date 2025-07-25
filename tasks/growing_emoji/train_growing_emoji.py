#!/usr/bin/env python3
import os
import sys

import click

from torch.utils.data import DataLoader

from torch.utils.tensorboard import SummaryWriter

import numpy as np

root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.append(root_dir)

from ncalab import (
    GrowingNCADataset,
    GrowingNCAModel,
    BasicNCATrainer,
    WEIGHTS_PATH,
    get_compute_device,
    print_NCALab_banner,
    fix_random_seed,
    Pool,
)

from growing_utils import get_emoji_image


def train_growing_emoji(
    batch_size: int, hidden_channels: int, gpu: bool, gpu_index: int, max_epochs: int
):
    """
    Main function to run the "growing emoji" example task.

    :param batch_size [int]: Minibatch size.
    :param hidden_channels [int]: Hidden channels the NCA will use.
    """
    print_NCALab_banner()
    fix_random_seed()

    # Create tensorboard summary writer
    writer = SummaryWriter(comment=" Growing NCA (Lizard)")

    # Select device, try to use GPU or fall back to CPU
    device = get_compute_device(f"cuda:{gpu_index}" if gpu else "cpu")

    pool = Pool(6, damage=True)

    # Create NCA model definition
    nca = GrowingNCAModel(
        device,
        num_image_channels=4,
        num_hidden_channels=hidden_channels,
        use_alive_mask=True,
    )

    # Create dataset containing a single growing emoji
    image = np.asarray(get_emoji_image())
    dataset = GrowingNCADataset(image, nca.num_channels, batch_size=batch_size)
    dataloader_train = DataLoader(dataset, batch_size=batch_size, shuffle=False)

    # Create Trainer and run training
    trainer = BasicNCATrainer(
        nca,
        WEIGHTS_PATH / "ncalab_growing_emoji.pth",
        max_epochs=max_epochs,
        pool=pool,
    )
    trainer.train(dataloader_train, summary_writer=writer, save_every=100)
    writer.close()


@click.command()
@click.option("--batch-size", "-b", default=8, type=int)
@click.option("--hidden-channels", "-H", default=12, type=int)
@click.option(
    "--gpu/--no-gpu", is_flag=True, default=True, help="Try using the GPU if available."
)
@click.option(
    "--gpu-index", type=int, default=0, help="Index of GPU to use, if --gpu in use."
)
@click.option("--epochs", "-e", type=int, default=5000)
def main(batch_size, hidden_channels, gpu, gpu_index, epochs):
    train_growing_emoji(
        batch_size=batch_size,
        hidden_channels=hidden_channels,
        gpu=gpu,
        gpu_index=gpu_index,
        max_epochs=epochs,
    )


if __name__ == "__main__":
    main()
