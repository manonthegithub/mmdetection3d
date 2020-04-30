import torch
import torch.nn as nn

import mmdet3d.ops.spconv as spconv
from mmdet3d.ops import SparseBasicBlock
from mmdet.ops import build_norm_layer
from ..registry import MIDDLE_ENCODERS


@MIDDLE_ENCODERS.register_module
class SparseUnet(nn.Module):

    def __init__(self,
                 in_channels,
                 output_shape,
                 pre_act=False,
                 norm_cfg=dict(type='BN1d', eps=1e-3, momentum=0.01),
                 base_channels=16,
                 out_conv_channels=128,
                 encode_conv_channels=((16, ), (32, 32, 32), (64, 64, 64),
                                       (64, 64, 64)),
                 encode_paddings=((1, ), (1, 1, 1), (1, 1, 1), ((0, 1, 1), 1,
                                                                1)),
                 decode_conv_channels=((64, 64, 64), (64, 64, 32),
                                       (32, 32, 16), (16, 16, 16)),
                 decode_paddings=((1, 0), (1, 0), (0, 0), (0, 1))):
        """SparseUnet for PartA^2

        See https://arxiv.org/abs/1907.03670 for more detials.

        Args:
            in_channels (int): the number of input channels
            output_shape (list[int]): the shape of output tensor
            pre_act (bool): use pre_act_block or post_act_block
            norm_cfg (dict): normalize layer config
            base_channels (int): out channels for conv_input layer
            out_conv_channels (int): out channels for conv_out layer
            encode_conv_channels (tuple[tuple[int]]):
                conv channels of each encond block
            encode_paddings (tuple[tuple[int]]): paddings of each encode block
            decode_conv_channels (tuple[tuple[int]]):
                conv channels of each decode block
            decode_paddings (tuple[tuple[int]]): paddings of each decode block
        """
        super().__init__()
        self.sparse_shape = output_shape
        self.output_shape = output_shape
        self.in_channels = in_channels
        self.pre_act = pre_act
        self.base_channels = base_channels
        self.out_conv_channels = out_conv_channels
        self.encode_conv_channels = encode_conv_channels
        self.encode_paddings = encode_paddings
        self.decode_conv_channels = decode_conv_channels
        self.decode_paddings = decode_paddings
        self.stage_num = len(self.encode_conv_channels)
        # Spconv init all weight on its own
        # TODO: make the network could be modified

        if pre_act:
            # TODO: use ConvModule to encapsulate
            self.conv_input = spconv.SparseSequential(
                spconv.SubMConv3d(
                    in_channels,
                    self.base_channels,
                    3,
                    padding=1,
                    bias=False,
                    indice_key='subm1'), )
            make_block = self.pre_act_block
        else:
            norm_name, norm_layer = build_norm_layer(norm_cfg,
                                                     self.base_channels)
            self.conv_input = spconv.SparseSequential(
                spconv.SubMConv3d(
                    in_channels,
                    self.base_channels,
                    3,
                    padding=1,
                    bias=False,
                    indice_key='subm1'),
                norm_layer,
                nn.ReLU(),
            )
            make_block = self.post_act_block

        encoder_out_channels = self.make_encode_layers(make_block, norm_cfg,
                                                       self.base_channels)
        self.make_decode_layers(make_block, norm_cfg, encoder_out_channels)

        norm_name, norm_layer = build_norm_layer(norm_cfg,
                                                 self.out_conv_channels)
        self.conv_out = spconv.SparseSequential(
            # [200, 176, 5] -> [200, 176, 2]
            spconv.SparseConv3d(
                encoder_out_channels,
                self.out_conv_channels, (3, 1, 1),
                stride=(2, 1, 1),
                padding=0,
                bias=False,
                indice_key='spconv_down2'),
            norm_layer,
            nn.ReLU(),
        )

    def forward(self, voxel_features, coors, batch_size):
        """Forward of SparseUnet

        Args:
            voxel_features (torch.float32): shape [N, C]
            coors (torch.int32): shape [N, 4](batch_idx, z_idx, y_idx, x_idx)
            batch_size (int): batch size

        Returns:
            dict: backbone features
        """
        coors = coors.int()
        input_sp_tensor = spconv.SparseConvTensor(voxel_features, coors,
                                                  self.sparse_shape,
                                                  batch_size)
        x = self.conv_input(input_sp_tensor)

        encode_features = []
        for i, stage_name in enumerate(self.encoder):
            stage = getattr(self, stage_name)
            x = stage(x)
            encode_features.append(x)

        # for detection head
        # [200, 176, 5] -> [200, 176, 2]
        out = self.conv_out(encode_features[-1])
        spatial_features = out.dense()

        N, C, D, H, W = spatial_features.shape
        spatial_features = spatial_features.view(N, C * D, H, W)

        ret = {'spatial_features': spatial_features}

        # for segmentation head, with output shape:
        # [400, 352, 11] <- [200, 176, 5]
        # [800, 704, 21] <- [400, 352, 11]
        # [1600, 1408, 41] <- [800, 704, 21]
        # [1600, 1408, 41] <- [1600, 1408, 41]
        decode_features = []
        x = encode_features[-1]
        for i in range(self.stage_num, 0, -1):
            x = self.UR_block_forward(
                encode_features[i - 1],
                x,
                getattr(self, 'conv_up_t{}'.format(i)),
                getattr(self, 'conv_up_m{}'.format(i)),
                getattr(self, 'inv_conv{}'.format(i)),
            )
            decode_features.append(x)

        seg_features = decode_features[-1].features

        ret.update({'seg_features': seg_features})

        return ret

    def UR_block_forward(self, x_lateral, x_bottom, conv_t, conv_m, conv_inv):
        """Forward of upsample and residual block.

        Args:
            x_lateral (SparseConvTensor): lateral tensor
            x_bottom (SparseConvTensor): tensor from bottom layer
            conv_t (SparseBasicBlock): convolution for lateral tensor
            conv_m (SparseSequential): convolution for merging features
            conv_inv (SparseSequential): convolution for upsampling

        Returns:
            SparseConvTensor: upsampled feature
        """
        x_trans = conv_t(x_lateral)
        x = x_trans
        x.features = torch.cat((x_bottom.features, x_trans.features), dim=1)
        x_m = conv_m(x)
        x = self.channel_reduction(x, x_m.features.shape[1])
        x.features = x_m.features + x.features
        x = conv_inv(x)
        return x

    @staticmethod
    def channel_reduction(x, out_channels):
        """Channel reduction for element-wise add.

        Args:
            x (SparseConvTensor): x.features (N, C1)
            out_channels (int): the number of channel after reduction

        Returns:
            SparseConvTensor: channel reduced feature
        """
        features = x.features
        n, in_channels = features.shape
        assert (in_channels %
                out_channels == 0) and (in_channels >= out_channels)

        x.features = features.view(n, out_channels, -1).sum(dim=2)
        return x

    def pre_act_block(self,
                      in_channels,
                      out_channels,
                      kernel_size,
                      indice_key=None,
                      stride=1,
                      padding=0,
                      conv_type='subm',
                      norm_cfg=None):
        """Make pre activate sparse convolution block.

        Args:
            in_channels (int): the number of input channels
            out_channels (int): the number of out channels
            kernel_size (int): kernel size of convolution
            indice_key (str): the indice key used for sparse tensor
            stride (int): the stride of convolution
            padding (int or list[int]): the padding number of input
            conv_type (str): conv type in 'subm', 'spconv' or 'inverseconv'
            norm_cfg (dict): normal layer configs

        Returns:
            spconv.SparseSequential: pre activate sparse convolution block.
        """
        # TODO: use ConvModule to encapsulate
        assert conv_type in ['subm', 'spconv', 'inverseconv']

        norm_name, norm_layer = build_norm_layer(norm_cfg, in_channels)
        if conv_type == 'subm':
            m = spconv.SparseSequential(
                norm_layer,
                nn.ReLU(inplace=True),
                spconv.SubMConv3d(
                    in_channels,
                    out_channels,
                    kernel_size,
                    padding=padding,
                    bias=False,
                    indice_key=indice_key),
            )
        elif conv_type == 'spconv':
            m = spconv.SparseSequential(
                norm_layer,
                nn.ReLU(inplace=True),
                spconv.SparseConv3d(
                    in_channels,
                    out_channels,
                    kernel_size,
                    stride=stride,
                    padding=padding,
                    bias=False,
                    indice_key=indice_key),
            )
        elif conv_type == 'inverseconv':
            m = spconv.SparseSequential(
                norm_layer,
                nn.ReLU(inplace=True),
                spconv.SparseInverseConv3d(
                    in_channels,
                    out_channels,
                    kernel_size,
                    bias=False,
                    indice_key=indice_key),
            )
        else:
            raise NotImplementedError
        return m

    def post_act_block(self,
                       in_channels,
                       out_channels,
                       kernel_size,
                       indice_key,
                       stride=1,
                       padding=0,
                       conv_type='subm',
                       norm_cfg=None):
        """Make post activate sparse convolution block.

        Args:
            in_channels (int): the number of input channels
            out_channels (int): the number of out channels
            kernel_size (int): kernel size of convolution
            indice_key (str): the indice key used for sparse tensor
            stride (int): the stride of convolution
            padding (int or list[int]): the padding number of input
            conv_type (str): conv type in 'subm', 'spconv' or 'inverseconv'
            norm_cfg (dict[str]): normal layer configs

        Returns:
            spconv.SparseSequential: post activate sparse convolution block.
        """
        # TODO: use ConvModule to encapsulate
        assert conv_type in ['subm', 'spconv', 'inverseconv']

        norm_name, norm_layer = build_norm_layer(norm_cfg, out_channels)
        if conv_type == 'subm':
            m = spconv.SparseSequential(
                spconv.SubMConv3d(
                    in_channels,
                    out_channels,
                    kernel_size,
                    bias=False,
                    indice_key=indice_key),
                norm_layer,
                nn.ReLU(inplace=True),
            )
        elif conv_type == 'spconv':
            m = spconv.SparseSequential(
                spconv.SparseConv3d(
                    in_channels,
                    out_channels,
                    kernel_size,
                    stride=stride,
                    padding=padding,
                    bias=False,
                    indice_key=indice_key),
                norm_layer,
                nn.ReLU(inplace=True),
            )
        elif conv_type == 'inverseconv':
            m = spconv.SparseSequential(
                spconv.SparseInverseConv3d(
                    in_channels,
                    out_channels,
                    kernel_size,
                    bias=False,
                    indice_key=indice_key),
                norm_layer,
                nn.ReLU(inplace=True),
            )
        else:
            raise NotImplementedError
        return m

    def make_encode_layers(self, make_block, norm_cfg, in_channels):
        """make encode layers using sparse convs

        Args:
            make_block (method): a bounded function to build blocks
            norm_cfg (dict[str]): normal layer configs
            in_channels (int): the number of encoder input channels

        Returns:
            int: the number of encoder output channels
        """
        self.encoder = []
        for i, blocks in enumerate(self.encode_conv_channels):
            blocks_list = []
            for j, out_channels in enumerate(tuple(blocks)):
                padding = tuple(self.encode_paddings[i])[j]
                # each stage started with a spconv layer
                # except the first stage
                if i != 0 and j == 0:
                    blocks_list.append(
                        make_block(
                            in_channels,
                            out_channels,
                            3,
                            norm_cfg=norm_cfg,
                            stride=2,
                            padding=padding,
                            indice_key='spconv{}'.format(i + 1),
                            conv_type='spconv'))
                else:
                    blocks_list.append(
                        make_block(
                            in_channels,
                            out_channels,
                            3,
                            norm_cfg=norm_cfg,
                            padding=padding,
                            indice_key='subm{}'.format(i + 1)))
                in_channels = out_channels
            stage_name = 'conv{}'.format(i + 1)
            stage_layers = spconv.SparseSequential(*blocks_list)
            self.add_module(stage_name, stage_layers)
            self.encoder.append(stage_name)
        return out_channels

    def make_decode_layers(self, make_block, norm_cfg, in_channels):
        """make decode layers using sparse convs

        Args:
            make_block (method): a bounded function to build blocks
            norm_cfg (dict[str]): normal layer configs
            in_channels (int): the number of encoder input channels

        Returns:
            int: the number of encoder output channels
        """
        block_num = len(self.decode_conv_channels)
        for i, block_channels in enumerate(self.decode_conv_channels):
            paddings = self.decode_paddings[i]
            setattr(
                self, 'conv_up_t{}'.format(block_num - i),
                SparseBasicBlock(
                    in_channels,
                    block_channels[0],
                    conv_cfg=dict(
                        type='SubMConv3d',
                        indice_key='subm{}'.format(block_num - i)),
                    norm_cfg=norm_cfg))
            setattr(
                self, 'conv_up_m{}'.format(block_num - i),
                make_block(
                    in_channels * 2,
                    block_channels[1],
                    3,
                    norm_cfg=norm_cfg,
                    padding=paddings[0],
                    indice_key='subm{}'.format(block_num - i)))
            setattr(
                self,
                'inv_conv{}'.format(block_num - i),
                make_block(
                    in_channels,
                    block_channels[2],
                    3,
                    norm_cfg=norm_cfg,
                    padding=paddings[1],
                    indice_key='spconv{}'.format(block_num - i)
                    if block_num - i != 1 else 'subm1',
                    conv_type='inverseconv' if block_num - i != 1 else
                    'subm')  # use submanifold conv instead of inverse conv
                # in the last block
            )
            in_channels = block_channels[2]
