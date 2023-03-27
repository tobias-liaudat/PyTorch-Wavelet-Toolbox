"""This module implements two dimensional padded wavelet transforms."""


from typing import List, Optional, Tuple, Union

import pywt
import torch

from ._util import Wavelet, _as_wavelet, _outer
from .conv_transform import _get_pad, _translate_boundary_strings, get_filter_tensors
from complexPyTorch.complexLayers import ComplexConv2d
from complexPyTorch.complexLayers import ComplexConvTranspose2d

def construct_2d_filt(lo: torch.Tensor, hi: torch.Tensor) -> torch.Tensor:
    """Construct two dimensional filters using outer products.

    Args:
        lo (torch.Tensor): Low-pass input filter.
        hi (torch.Tensor): High-pass input filter

    Returns:
        torch.Tensor: Stacked 2d filters of dimension
            [filt_no, 1, height, width].
            The four filters are ordered ll, lh, hl, hh.

    """
    ll = _outer(lo, lo)
    lh = _outer(hi, lo)
    hl = _outer(lo, hi)
    hh = _outer(hi, hi)
    filt = torch.stack([ll, lh, hl, hh], 0)
    filt = filt.unsqueeze(1)
    return filt


def fwt_pad2(
    data: torch.Tensor, wavelet: Union[Wavelet, str], mode: str = "reflect"
) -> torch.Tensor:
    """Pad data for the 2d FWT.

    Args:
        data (torch.Tensor): Input data with 4 dimensions.
        wavelet (Wavelet or str): A pywt wavelet compatible object or
            the name of a pywt wavelet.
        mode (str): The padding mode.
            Supported modes are "reflect", "zero", "constant" and "periodic".
            Defaults to reflect.

    Returns:
        The padded output tensor.

    """
    mode = _translate_boundary_strings(mode)

    wavelet = _as_wavelet(wavelet)
    padb, padt = _get_pad(data.shape[-2], len(wavelet.dec_lo))
    padr, padl = _get_pad(data.shape[-1], len(wavelet.dec_lo))
    data_pad = torch.nn.functional.pad(data, [padl, padr, padt, padb], mode=mode)
    return data_pad


def wavedec2(
    data: torch.Tensor,
    wavelet: Union[Wavelet, str],
    level: Optional[int] = None,
    mode: str = "reflect",
) -> List[Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor, torch.Tensor]]]:
    """Non seperated two dimensional wavelet transform.

    Args:
        data (torch.Tensor): The input data tensor with up to three dimensions.
            2d inputs are interpreted as [height, width],
            3d inputs are interpreted as [batch_size, height, width].
        wavelet (Wavelet or str): A pywt wavelet compatible object or
            the name of a pywt wavelet.
        level (int): The number of desired scales.
            Defaults to None.
        mode (str): The padding mode. Options are
            "reflect", "zero", "constant" and "periodic".
            Defaults to "reflect".

    Returns:
        list: A list containing the wavelet coefficients.
              The coefficients are in pywt order. That is:
              [cAn, (cHn, cVn, cDn), … (cH1, cV1, cD1)] .
              A denotes approximation, H horizontal, V vertical
              and D diagonal coefficients.

    Raises:
        ValueError: If the dimensionality of the input data tensor is unsupported.

    Example:
        >>> import torch
        >>> import ptwt, pywt
        >>> import numpy as np
        >>> import scipy.misc
        >>> face = np.transpose(scipy.misc.face(),
        >>>                     [2, 0, 1]).astype(np.float64)
        >>> pytorch_face = torch.tensor(face)
        >>> coefficients = ptwt.wavedec2(pytorch_face, pywt.Wavelet("haar"),
        >>>                              level=2, mode="zero")

    """
    if data.dim() == 2:
        data = data.unsqueeze(0).unsqueeze(0)
    elif data.dim() == 3:
        data = data.unsqueeze(1)
    elif data.dim() == 4:
        raise ValueError(
            "Wavedec2 does not support four input dimensions. \
             Optionally-batched two dimensional inputs work."
        )
    elif data.dim() == 1:
        raise ValueError("Wavedec2 needs more than one input dimension to work.")

    wavelet = _as_wavelet(wavelet)
    dec_lo, dec_hi, _, _ = get_filter_tensors(
        wavelet, flip=True, device=data.device, dtype=data.dtype
    )
    dec_filt = construct_2d_filt(lo=dec_lo, hi=dec_hi)

    if level is None:
        level = pywt.dwtn_max_level([data.shape[-1], data.shape[-2]], wavelet)

    result_lst: List[
        Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor, torch.Tensor]]
    ] = []
    res_ll = data
    for _ in range(level):
        res_ll = fwt_pad2(res_ll, wavelet, mode=mode)
        # conv2d supports complex convolution
        res = torch.nn.functional.conv2d(res_ll, dec_filt, stride=2)

        # if torch.is_complex(res_ll):
        #     complex_conv_layer = ComplexConv2d(
        #         in_channels=res_ll.shape[1],
        #         out_channels=dec_filt.shape[0],
        #         kernel_size=(dec_filt.shape[2], dec_filt.shape[3]),
        #         stride=2,
        #         bias=False
        #     )
        #     complex_conv_layer.conv_r.weight.requires_grad = False
        #     complex_conv_layer.conv_i.weight.requires_grad = False
        #     _ = complex_conv_layer.conv_r.weight.copy_(dec_filt.real)
        #     _ = complex_conv_layer.conv_i.weight.copy_(dec_filt.real)
        #     res = complex_conv_layer(res_ll)
        # else:
        #     # Functional API
        #     res = torch.nn.functional.conv2d(res_ll, dec_filt, stride=2)
        #     # # Module API
        #     # conv_layer = torch.nn.Conv2d(
        #     #     in_channels=res_ll.shape[1],
        #     #     out_channels=dec_filt.shape[0],
        #     #     kernel_size=(dec_filt.shape[2], dec_filt.shape[3]),
        #     #     stride=2,
        #     #     bias=False
        #     # )
        #     # conv_layer.weight.requires_grad = False
        #     # _ = conv_layer.weight.copy_(dec_filt)
        #     # res = conv_layer(res_ll)

        res_ll, res_lh, res_hl, res_hh = torch.split(res, 1, 1)
        result_lst.append((res_lh, res_hl, res_hh))
    result_lst.append(res_ll)
    return result_lst[::-1]


def waverec2(
    coeffs: List[Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor, torch.Tensor]]],
    wavelet: Union[Wavelet, str],
) -> torch.Tensor:
    """Reconstruct a signal from wavelet coefficients.

    Args:
        coeffs (list): The wavelet coefficient list produced by wavedec2.
        wavelet (Wavelet or str): A pywt wavelet compatible object or
            the name of a pywt wavelet.

    Returns:
        torch.Tensor: The reconstructed signal.

    Raises:
        ValueError: If `coeffs` is not in the shape as it is returned from `wavedec2`.

    Example:
        >>> import ptwt, pywt, torch
        >>> import numpy as np
        >>> import scipy.misc
        >>> face = np.transpose(scipy.misc.face(),
        >>>                     [2, 0, 1]).astype(np.float64)
        >>> pytorch_face = torch.tensor(face)
        >>> coefficients = ptwt.wavedec2(pytorch_face, pywt.Wavelet("haar"),
        >>>                              level=2, mode="constant")
        >>> reconstruction = ptwt.waverec2(coefficients, pywt.Wavelet("haar"))

    """
    wavelet = _as_wavelet(wavelet)

    if not isinstance(coeffs[0], torch.Tensor):
        raise ValueError(
            "First element of coeffs must be the approximation coefficient tensor."
        )

    _, _, rec_lo, rec_hi = get_filter_tensors(
        wavelet, flip=False, device=coeffs[0].device, dtype=coeffs[0].dtype
    )
    filt_len = rec_lo.shape[-1]
    rec_filt = construct_2d_filt(lo=rec_lo, hi=rec_hi)

    res_ll = coeffs[0]
    for c_pos, res_lh_hl_hh in enumerate(coeffs[1:]):
        res_ll = torch.cat(
            [res_ll, res_lh_hl_hh[0], res_lh_hl_hh[1], res_lh_hl_hh[2]], 1
        )

        if torch.is_complex(res_ll):
            complex_conv_tr_layer = ComplexConvTranspose2d(
                in_channels=res_ll.shape[1],
                out_channels=rec_filt.shape[1],
                kernel_size=(rec_filt.shape[2], rec_filt.shape[3]),
                stride=2,
                bias=False,
            )
            complex_conv_tr_layer.conv_tran_r.weight.requires_grad = False
            complex_conv_tr_layer.conv_tran_i.weight.requires_grad = False
            _ = complex_conv_tr_layer.conv_tran_r.weight.copy_(rec_filt.real)
            _ = complex_conv_tr_layer.conv_tran_i.weight.copy_(rec_filt.imag)
            res_ll = complex_conv_tr_layer(res_ll)
        
        else:
            # Functional API
            res_ll = torch.nn.functional.conv_transpose2d(res_ll, rec_filt, stride=2)
            # # Moduler API
            # conv_tr_layer = torch.nn.ConvTranspose2d(
            #     in_channels=res_ll.shape[1],
            #     out_channels=rec_filt.shape[1],
            #     kernel_size=(rec_filt.shape[2], rec_filt.shape[3]),
            #     stride=2,
            #     bias=False,
            # )
            # conv_tr_layer.weight.requires_grad = False
            # _ = conv_tr_layer.weight.copy_(rec_filt)
            # res_ll = conv_tr_layer(res_ll)

        # remove the padding
        padl = (2 * filt_len - 3) // 2
        padr = (2 * filt_len - 3) // 2
        padt = (2 * filt_len - 3) // 2
        padb = (2 * filt_len - 3) // 2
        if c_pos < len(coeffs) - 2:
            pred_len = res_ll.shape[-1] - (padl + padr)
            next_len = coeffs[c_pos + 2][0].shape[-1]
            pred_len2 = res_ll.shape[-2] - (padt + padb)
            next_len2 = coeffs[c_pos + 2][0].shape[-2]
            if next_len != pred_len:
                padr += 1
                pred_len = res_ll.shape[-1] - (padl + padr)
                assert (
                    next_len == pred_len
                ), "padding error, please open an issue on github "
            if next_len2 != pred_len2:
                padb += 1
                pred_len2 = res_ll.shape[-2] - (padt + padb)
                assert (
                    next_len2 == pred_len2
                ), "padding error, please open an issue on github "
        if padt > 0:
            res_ll = res_ll[..., padt:, :]
        if padb > 0:
            res_ll = res_ll[..., :-padb, :]
        if padl > 0:
            res_ll = res_ll[..., padl:]
        if padr > 0:
            res_ll = res_ll[..., :-padr]
    return res_ll
