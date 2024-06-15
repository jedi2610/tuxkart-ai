import argparse
import os
import random
import time
from pathlib import Path
from typing import Tuple

import numpy as np
import numpy.typing as npt
import pystk
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from PIL import Image
from pystk_gym.common.race import RaceConfig
from torch.utils.data import DataLoader, Dataset
from torch.utils.tensorboard import SummaryWriter
from torchvision import transforms
from tqdm import tqdm

SAMPLE_RATE = 0.9
CLASS_COLOR = (
    (
        # https://pystk.readthedocs.io/en/latest/data.html#pystk.ObjectType
        np.array(
            [
                0x000000,  # None
                0x4E9A06,  # Kart
                0x2E3436,  # Track
                0xEEEEEC,  # Background
                0x204A87,  # Pickup
                0x204A87,  # Nitro
                0xA40000,  # Bomb
                0xCE5C00,  # Object
                0x5C3566,  # Projectile
                0x000000,  # Unknown
                0x000000,  # N
            ],
            dtype=">u4",
        )
        .view(np.uint8)
        .reshape((-1, 4))[:, 1:]
    )
    .flatten()
    .tolist()
)


class ResidualBlock(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(ResidualBlock, self).__init__()
        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1)
        self.bn1 = nn.BatchNorm2d(out_channels)
        self.conv2 = nn.Conv2d(
            out_channels, out_channels, kernel_size=3, padding=1, bias=False
        )
        self.bn2 = nn.BatchNorm2d(out_channels)

    def forward(self, x):
        residual = x
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        out += residual
        out = F.relu(out)
        return out


class VQVAE(nn.Module):
    def __init__(self, num_embeddings=512, embedding_dim=2048):
        super(VQVAE, self).__init__()
        self.embedding_dim = embedding_dim
        self.num_embeddings = num_embeddings

        self.encoder = nn.Sequential(
            nn.Conv2d(3, 64, kernel_size=5, stride=2, padding=1),
            nn.ReLU(),
            nn.Conv2d(64, 128, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            nn.Conv2d(128, embedding_dim, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            ResidualBlock(embedding_dim, embedding_dim),
            # ResidualBlock(embedding_dim, embedding_dim),
        )

        self.decoder = nn.Sequential(
            # ResidualBlock(embedding_dim, embedding_dim),
            ResidualBlock(embedding_dim, embedding_dim),
            nn.ConvTranspose2d(embedding_dim, 128, kernel_size=5, stride=2, padding=1),
            nn.ReLU(),
            nn.ConvTranspose2d(128, 64, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            nn.ConvTranspose2d(64, 1, kernel_size=3, stride=2, padding=1),
        )

        self.embeddings = nn.Embedding(num_embeddings, embedding_dim)
        self.embeddings.weight.data.uniform_(-1 / num_embeddings, 1 / num_embeddings)

    def forward(self, x):
        z_e = self.encoder(x)
        z_e_flattened = (
            z_e.permute(0, 2, 3, 1).contiguous().view(-1, self.embedding_dim)
        )

        distances = (
            torch.sum(z_e_flattened**2, dim=1, keepdim=True)
            + torch.sum(self.embeddings.weight**2, dim=1)
            - 2 * (z_e_flattened @ self.embeddings.weight.t())
        )
        encoding_indices = torch.argmin(distances, dim=1).unsqueeze(1)
        z_q = self.embeddings(encoding_indices).view(z_e.size())

        return self.decoder(z_q), z_e, z_q


class CustomImageDataset(Dataset):
    def __init__(self, image_datas, transform=None):
        self.image_datas = image_datas
        self.transform = transform

    def __len__(self):
        return len(self.image_datas)

    def __getitem__(self, idx):
        image = self.image_datas[idx]
        if self.transform:
            image = self.transform(image)
        return image


def cmap_semantic_image(img: Image.Image) -> np.ndarray:
    img.putpalette(CLASS_COLOR)
    return np.array(img.convert("L"))


def get_pystk_configs(
    num_players: int,
) -> Tuple[pystk.GraphicsConfig, pystk.RaceConfig]:
    graphic_config = pystk.GraphicsConfig.hd()
    graphic_config.screen_width = 960
    graphic_config.screen_height = 540

    race_config = pystk.RaceConfig()
    race_config.laps = 1
    race_config.num_kart = num_players
    race_config.players[0].kart = np.random.choice(RaceConfig.KARTS)
    race_config.players[0].controller = pystk.PlayerConfig.Controller.AI_CONTROL

    for _ in range(1, num_players):
        race_config.players.append(
            pystk.PlayerConfig(
                np.random.choice(RaceConfig.KARTS),
                pystk.PlayerConfig.Controller.AI_CONTROL,
                0,
            )
        )
    race_config.track = np.random.choice(RaceConfig.TRACKS)
    race_config.step_size = 0.345

    return (graphic_config, race_config)


def generate_data(
    graphic_config: pystk.GraphicsConfig,
    race_config: pystk.RaceConfig,
    sample_rate: float,
    max_samples: int = 64,
) -> npt.NDArray[np.float32]:
    datas, samples = [], 0
    race, state, steps, t0 = None, None, 0, 0

    while samples < max_samples:
        if (race is None or state is None) or any(
            kart.finish_time > 0 for kart in state.karts
        ):
            if race is not None:
                race.stop()
                del race
                pystk.clean()

            pystk.init(graphic_config)
            race = pystk.Race(race_config)
            race.start()
            race.step()

            state = pystk.WorldState()
            state.update()
            t0 = time.time()
            steps = 0

        race.step()
        state.update()

        if random.random() < sample_rate:
            samples += race_config.num_kart
            for kart_render_data in race.render_data:
                img = np.array(
                    Image.fromarray(kart_render_data.image).convert("L")
                ) / np.float32(255.0)
                depth = kart_render_data.depth.astype(np.float32)
                semantic = (kart_render_data.instance >> 24) & 0xFF
                semantic = cmap_semantic_image(
                    Image.fromarray(semantic.astype(np.uint8))
                ) / np.float32(255.0)
                data = np.stack((img, depth, semantic))
                datas.append(data)

        steps += 1
        delta_d = steps * race_config.step_size - (time.time() - t0)
        if delta_d > 0:
            time.sleep(delta_d)

    if race is not None:
        race.stop()
        del race
        pystk.clean()
    return np.array(datas)


def main(args):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    criterion = nn.MSELoss()
    model = VQVAE().to(device)
    optimizer = optim.Adam(model.parameters(), lr=args.lr)

    tensorboard_file_name = args.log_dir.joinpath("vae")
    logger = SummaryWriter(tensorboard_file_name, flush_secs=30)

    start_epoch = 0
    if args.model_path and args.model_path.exists():
        checkpoint = torch.load(args.model_path)
        start_epoch = checkpoint["epoch"]
        model.load_state_dict(checkpoint["model_state_dict"])
        optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
    elif args.model_path and not args.model_path.exists():
        print(f"{args.model_path} does not exist")

    epochs = 1000
    epoch_progress_bar = tqdm(range(start_epoch, epochs), position=0, desc="Loss: inf")

    for epoch in epoch_progress_bar:
        if epoch != start_epoch and epoch % args.eval_interval == 0:
            model.eval()
            graphic_config, race_config = get_pystk_configs(args.num_players)
            datas = generate_data(graphic_config, race_config, SAMPLE_RATE, 16)
            datas = torch.from_numpy(datas)
            with torch.no_grad():
                recon_images = torch.cat(
                    [
                        F.interpolate(
                            model(batch_imgs.cuda())[0].cpu(),
                            (540, 960),
                            mode="nearest",
                        ).squeeze(dim=1)
                        for batch_imgs in datas.split(args.batch_size)
                    ]
                ).unsqueeze(1)
            logger.add_images(
                "eval_vae/images", datas[:, :1, :, :].numpy(), epoch, dataformats="NCHW"
            )
            logger.add_images(
                "eval_vae/recon_images",
                recon_images,
                epoch,
                dataformats="NCHW",
            )

        if epoch != start_epoch and epoch % args.save_interval == 0:
            torch.save(
                {
                    "epoch": epoch,
                    "model_state_dict": model.state_dict(),
                    "optimizer_state_dict": optimizer.state_dict(),
                },
                args.save_dir.joinpath(f"vae_{epoch}.pth"),
            )

        # collect data
        graphic_config, race_config = get_pystk_configs(args.num_players)
        datas = generate_data(
            graphic_config, race_config, SAMPLE_RATE, args.max_samples
        )
        datas = torch.from_numpy(datas)
        dataset = CustomImageDataset(datas, transform=None)
        dataloader = DataLoader(
            dataset, batch_size=args.batch_size, shuffle=True, num_workers=1
        )
        dataloader_len = len(dataloader)

        # setup training
        model.train()
        train_loss = 0.0
        dataloader_progress_bar = tqdm(
            dataloader, position=1, leave=False, desc="Loss: inf"
        )

        # train
        for batch_idx, images in enumerate(dataloader_progress_bar):
            images = images.to(device)
            grayscale_images = images[:, 0, :, :]
            optimizer.zero_grad()

            outputs, z_e, z_q = model(images)
            recons = F.interpolate(outputs, (540, 960), mode="nearest").squeeze(dim=1)
            recon_loss = criterion(recons, grayscale_images)
            commitment_loss = torch.mean((z_e - z_q.detach()) ** 2)
            vq_loss = torch.mean((z_q - z_e.detach()) ** 2)
            loss = recon_loss + commitment_loss + vq_loss

            loss.backward()
            optimizer.step()
            # torch.cuda.empty_cache()
            loss_cpu = loss.item()
            train_loss += loss_cpu
            logger.add_scalar(
                "train_vae/recon_loss",
                recon_loss.item(),
                epoch * dataloader_len + batch_idx,
            )
            logger.add_scalar(
                "train_vae/commitment_loss",
                commitment_loss.item(),
                epoch * dataloader_len + batch_idx,
            )
            logger.add_scalar(
                "train_vae/vq_loss",
                vq_loss.item(),
                epoch * dataloader_len + batch_idx,
            )
            logger.add_scalar(
                "train_vae/batch_loss", loss_cpu, epoch * dataloader_len + batch_idx
            )
            dataloader_progress_bar.set_description(f"Loss: {loss_cpu:.6f}")

        train_loss /= dataloader_len
        logger.add_scalar("train_vae/epoch_loss", train_loss, epoch)
        epoch_progress_bar.set_description(f"Loss: {train_loss:.6f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--num_players", type=int, default=4)

    # model args
    parser.add_argument(
        "--model_path", type=Path, default=None, help="Load model from path."
    )
    parser.add_argument("--lr", type=float, default=1e-4)

    # train args
    parser.add_argument("--batch_size", type=int, default=2)
    parser.add_argument("--max_samples", type=int, default=256)
    parser.add_argument("--eval_interval", type=int, default=10)
    parser.add_argument("--save_interval", type=int, default=25)
    parser.add_argument("--beta_anneal_interval", type=int, default=15)
    parser.add_argument(
        "--log_dir",
        type=Path,
        default=os.path.join(Path(__file__).absolute().parent, "tensorboard"),
        help="Path to the directory in which the tensorboard logs are saved.",
    )
    parser.add_argument(
        "--save_dir",
        type=Path,
        default=os.path.join(Path(__file__).absolute().parent, "models"),
        help="Path to the directory in which the trained models are saved.",
    )
    args = parser.parse_args()

    main(args)
