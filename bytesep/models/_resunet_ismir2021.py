import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchlibrosa.stft import STFT, ISTFT, magphase
from inplace_abn.abn import InPlaceABNSync

from bytesep.models.pytorch_modules import Base, init_bn, init_layer


class ConvBlockRes(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, activation, momentum):
        r"""Residual block."""
        super(ConvBlockRes, self).__init__()

        self.activation = activation
        padding = [kernel_size[0] // 2, kernel_size[1] // 2]

        self.bn1 = nn.BatchNorm2d(in_channels, momentum=momentum)
        self.abn2 = InPlaceABNSync(
            num_features=out_channels, momentum=momentum, activation='leaky_relu'
        )

        self.conv1 = nn.Conv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=(1, 1),
            dilation=(1, 1),
            padding=padding,
            bias=False,
        )

        self.conv2 = nn.Conv2d(
            in_channels=out_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=(1, 1),
            dilation=(1, 1),
            padding=padding,
            bias=False,
        )

        if in_channels != out_channels:
            self.shortcut = nn.Conv2d(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            )
            self.is_shortcut = True
        else:
            self.is_shortcut = False

        self.init_weights()

    def init_weights(self):
        init_bn(self.bn1)
        init_layer(self.conv1)
        init_layer(self.conv2)

        if self.is_shortcut:
            init_layer(self.shortcut)

    def forward(self, x):
        origin = x
        x = self.conv1(F.leaky_relu_(self.bn1(x), negative_slope=0.01))
        x = self.conv2(self.abn2(x))

        if self.is_shortcut:
            return self.shortcut(origin) + x
        else:
            return origin + x


class EncoderBlockRes4B(nn.Module):
    def __init__(
        self, in_channels, out_channels, kernel_size, downsample, activation, momentum
    ):
        r"""Encoder block, contains 8 convolutional layers."""
        super(EncoderBlockRes4B, self).__init__()

        self.conv_block1 = ConvBlockRes(
            in_channels, out_channels, kernel_size, activation, momentum
        )
        self.conv_block2 = ConvBlockRes(
            out_channels, out_channels, kernel_size, activation, momentum
        )
        self.conv_block3 = ConvBlockRes(
            out_channels, out_channels, kernel_size, activation, momentum
        )
        self.conv_block4 = ConvBlockRes(
            out_channels, out_channels, kernel_size, activation, momentum
        )
        self.downsample = downsample

    def forward(self, x):
        encoder = self.conv_block1(x)
        encoder = self.conv_block2(encoder)
        encoder = self.conv_block3(encoder)
        encoder = self.conv_block4(encoder)
        encoder_pool = F.avg_pool2d(encoder, kernel_size=self.downsample)
        return encoder_pool, encoder


class DecoderBlockRes4B(nn.Module):
    def __init__(
        self, in_channels, out_channels, kernel_size, upsample, activation, momentum
    ):
        r"""Decoder block, contains 1 transpose convolutional and 8 convolutional layers."""
        super(DecoderBlockRes4B, self).__init__()
        self.kernel_size = kernel_size
        self.stride = upsample
        self.activation = activation

        self.conv1 = torch.nn.ConvTranspose2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=self.stride,
            stride=self.stride,
            padding=(0, 0),
            bias=False,
            dilation=(1, 1),
        )

        self.bn1 = nn.BatchNorm2d(in_channels, momentum=momentum)
        self.conv_block2 = ConvBlockRes(
            out_channels * 2, out_channels, kernel_size, activation, momentum
        )
        self.conv_block3 = ConvBlockRes(
            out_channels, out_channels, kernel_size, activation, momentum
        )
        self.conv_block4 = ConvBlockRes(
            out_channels, out_channels, kernel_size, activation, momentum
        )
        self.conv_block5 = ConvBlockRes(
            out_channels, out_channels, kernel_size, activation, momentum
        )

        self.init_weights()

    def init_weights(self):
        init_bn(self.bn1)
        init_layer(self.conv1)

    def forward(self, input_tensor, concat_tensor):
        x = self.conv1(F.relu_(self.bn1(input_tensor)))
        x = torch.cat((x, concat_tensor), dim=1)
        x = self.conv_block2(x)
        x = self.conv_block3(x)
        x = self.conv_block4(x)
        x = self.conv_block5(x)
        return x


class ResUNet143_DecouplePlusInplaceABN_ISMIR2021(nn.Module, Base):
    def __init__(self, input_channels, target_sources_num):
        super(ResUNet143_DecouplePlusInplaceABN_ISMIR2021, self).__init__()

        self.input_channels = input_channels
        window_size = 2048
        hop_size = 441
        center = True
        pad_mode = 'reflect'
        window = 'hann'
        activation = 'relu'
        momentum = 0.01

        # Downsample rate along the time axis.
        self.time_downsample_ratio = 2 ** 5

        self.stft = STFT(
            n_fft=window_size,
            hop_length=hop_size,
            win_length=window_size,
            window=window,
            center=center,
            pad_mode=pad_mode,
            freeze_parameters=True,
        )

        self.istft = ISTFT(
            n_fft=window_size,
            hop_length=hop_size,
            win_length=window_size,
            window=window,
            center=center,
            pad_mode=pad_mode,
            freeze_parameters=True,
        )

        self.bn0 = nn.BatchNorm2d(window_size // 2 + 1, momentum=momentum)

        self.encoder_block1 = EncoderBlockRes4B(
            in_channels=input_channels,
            out_channels=32,
            kernel_size=(3, 3),
            downsample=(2, 2),
            activation=activation,
            momentum=momentum,
        )
        self.encoder_block2 = EncoderBlockRes4B(
            in_channels=32,
            out_channels=64,
            kernel_size=(3, 3),
            downsample=(2, 2),
            activation=activation,
            momentum=momentum,
        )
        self.encoder_block3 = EncoderBlockRes4B(
            in_channels=64,
            out_channels=128,
            kernel_size=(3, 3),
            downsample=(2, 2),
            activation=activation,
            momentum=momentum,
        )
        self.encoder_block4 = EncoderBlockRes4B(
            in_channels=128,
            out_channels=256,
            kernel_size=(3, 3),
            downsample=(2, 2),
            activation=activation,
            momentum=momentum,
        )
        self.encoder_block5 = EncoderBlockRes4B(
            in_channels=256,
            out_channels=384,
            kernel_size=(3, 3),
            downsample=(2, 2),
            activation=activation,
            momentum=momentum,
        )
        self.encoder_block6 = EncoderBlockRes4B(
            in_channels=384,
            out_channels=384,
            kernel_size=(3, 3),
            downsample=(1, 2),
            activation=activation,
            momentum=momentum,
        )
        self.conv_block7a = EncoderBlockRes4B(
            in_channels=384,
            out_channels=384,
            kernel_size=(3, 3),
            downsample=(1, 1),
            activation=activation,
            momentum=momentum,
        )
        self.conv_block7b = EncoderBlockRes4B(
            in_channels=384,
            out_channels=384,
            kernel_size=(3, 3),
            downsample=(1, 1),
            activation=activation,
            momentum=momentum,
        )
        self.conv_block7c = EncoderBlockRes4B(
            in_channels=384,
            out_channels=384,
            kernel_size=(3, 3),
            downsample=(1, 1),
            activation=activation,
            momentum=momentum,
        )
        self.conv_block7d = EncoderBlockRes4B(
            in_channels=384,
            out_channels=384,
            kernel_size=(3, 3),
            downsample=(1, 1),
            activation=activation,
            momentum=momentum,
        )
        self.decoder_block1 = DecoderBlockRes4B(
            in_channels=384,
            out_channels=384,
            kernel_size=(3, 3),
            upsample=(1, 2),
            activation=activation,
            momentum=momentum,
        )
        self.decoder_block2 = DecoderBlockRes4B(
            in_channels=384,
            out_channels=384,
            kernel_size=(3, 3),
            upsample=(2, 2),
            activation=activation,
            momentum=momentum,
        )
        self.decoder_block3 = DecoderBlockRes4B(
            in_channels=384,
            out_channels=256,
            kernel_size=(3, 3),
            upsample=(2, 2),
            activation=activation,
            momentum=momentum,
        )
        self.decoder_block4 = DecoderBlockRes4B(
            in_channels=256,
            out_channels=128,
            kernel_size=(3, 3),
            upsample=(2, 2),
            activation=activation,
            momentum=momentum,
        )
        self.decoder_block5 = DecoderBlockRes4B(
            in_channels=128,
            out_channels=64,
            kernel_size=(3, 3),
            upsample=(2, 2),
            activation=activation,
            momentum=momentum,
        )
        self.decoder_block6 = DecoderBlockRes4B(
            in_channels=64,
            out_channels=32,
            kernel_size=(3, 3),
            upsample=(2, 2),
            activation=activation,
            momentum=momentum,
        )

        self.after_conv_block1 = EncoderBlockRes4B(
            in_channels=32,
            out_channels=32,
            kernel_size=(3, 3),
            downsample=(1, 1),
            activation=activation,
            momentum=momentum,
        )

        self.after_conv2 = nn.Conv2d(
            in_channels=32,
            out_channels=input_channels * 4,
            kernel_size=(1, 1),
            stride=(1, 1),
            padding=(0, 0),
            bias=True,
        )

        self.init_weights()

    def init_weights(self):
        init_bn(self.bn0)
        init_layer(self.after_conv2)

    def forward(self, input_dict):
        r"""
        Args:
            input: (batch_size, channels_num, segment_samples)

        Outputs:
            output_dict: {
                'wav': (batch_size, channels_num, segment_samples)
            }
        """
        mixture = input_dict['waveform']

        sp, cos_in, sin_in = self.wav_to_spectrogram_phase(mixture)
        # shapes: (batch_size, channels_num, time_steps, freq_bins)

        # batch normalization
        x = sp.transpose(1, 3)
        x = self.bn0(x)
        x = x.transpose(1, 3)
        # (batch_size, chanenls, time_steps, freq_bins)

        # Pad spectrogram to be evenly divided by downsample ratio.
        origin_len = x.shape[2]
        pad_len = (
            int(np.ceil(x.shape[2] / self.time_downsample_ratio))
            * self.time_downsample_ratio
            - origin_len
        )
        x = F.pad(x, pad=(0, 0, 0, pad_len))
        # (batch_size, channels, padded_time_steps, freq_bins)

        # Let frequency bins be evenly divided by 2, e.g., 1025 -> 1024.
        x = x[..., 0 : x.shape[-1] - 1]  # (bs, channels, T, F)

        (N_, C_, T_, F_) = x.shape

        # UNet
        (x1_pool, x1) = self.encoder_block1(x)  # x1_pool: (bs, 32, T / 2, F / 2)
        (x2_pool, x2) = self.encoder_block2(x1_pool)  # x2_pool: (bs, 64, T / 4, F / 4)
        (x3_pool, x3) = self.encoder_block3(x2_pool)  # x3_pool: (bs, 128, T / 8, F / 8)
        (x4_pool, x4) = self.encoder_block4(
            x3_pool
        )  # x4_pool: (bs, 256, T / 16, F / 16)
        (x5_pool, x5) = self.encoder_block5(
            x4_pool
        )  # x5_pool: (bs, 384, T / 32, F / 32)
        (x6_pool, x6) = self.encoder_block6(
            x5_pool
        )  # x6_pool: (bs, 384, T / 32, F / 64)
        (x_center, _) = self.conv_block7a(x6_pool)  # (bs, 384, T / 32, F / 64)
        (x_center, _) = self.conv_block7b(x_center)  # (bs, 384, T / 32, F / 64)
        (x_center, _) = self.conv_block7c(x_center)  # (bs, 384, T / 32, F / 64)
        (x_center, _) = self.conv_block7d(x_center)  # (bs, 384, T / 32, F / 64)
        x7 = self.decoder_block1(x_center, x6)  # (bs, 384, T / 32, F / 32)
        x8 = self.decoder_block2(x7, x5)  # (bs, 384, T / 16, F / 16)
        x9 = self.decoder_block3(x8, x4)  # (bs, 256, T / 8, F / 8)
        x10 = self.decoder_block4(x9, x3)  # (bs, 128, T / 4, F / 4)
        x11 = self.decoder_block5(x10, x2)  # (bs, 64, T / 2, F / 2)
        x12 = self.decoder_block6(x11, x1)  # (bs, 32, T, F)
        (x, _) = self.after_conv_block1(x12)  # (bs, 32, T, F)
        x = self.after_conv2(x)  # (bs, channels * 3, T, F)

        # Recover shape
        x = F.pad(x, pad=(0, 1))  # Pad frequency, e.g., 1024 -> 1025.
        x = x[:, :, 0:origin_len, :]  # (bs, channels * 3, T, F)

        mask_mag1 = torch.sigmoid(x[:, 0 : self.input_channels, :, :])
        _mask_real = x[:, self.input_channels : self.input_channels * 2, :, :]
        _mask_imag = x[:, self.input_channels * 2 : self.input_channels * 3, :, :]
        _, mask_cos, mask_sin = magphase(_mask_real, _mask_imag)

        linear_mag = x[:, self.input_channels * 3 : self.input_channels * 4, :, :]

        # e^{jX + jM}
        out_cos = cos_in * mask_cos - sin_in * mask_sin
        out_sin = sin_in * mask_cos + cos_in * mask_sin

        # out_mag = sp * mask_mag
        out_mag = F.relu_(sp * mask_mag1 + linear_mag)
        out_real = out_mag * out_cos
        out_imag = out_mag * out_sin

        length = mixture.shape[2]

        wav_out = [
            self.istft(
                out_real[:, channel : channel + 1, :, :],
                out_imag[:, channel : channel + 1, :, :],
                length,
            )
            for channel in range(self.input_channels)
        ]
        # (batch_size, channels_num, segments_num)

        wav_out = torch.stack(wav_out, dim=1)
        # (batch_size, channels_num, segments_num)

        output_dict = {'waveform': wav_out}

        # from IPython import embed; embed(using=False); os._exit(0)

        return output_dict
