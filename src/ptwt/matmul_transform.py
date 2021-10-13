# Created by moritz (wolter@cs.uni-bonn.de) at 14.04.20
"""
This module implements matrix based fwt and ifwt
based on the description in Strang Nguyen (p. 32).
As well as the description of boundary filters in
"Ripples in Mathematics" section 10.3 .
"""
import numpy as np
import torch
from .sparse_math import (
    _orth_by_qr,
    _orth_by_gram_schmidt,
    construct_strided_conv_matrix
)

from .conv_transform import get_filter_tensors


def cat_sparse_identity_matrix(sparse_matrix, new_length):
    """Concatenate a sparse input matrix and a sparse identity matrix.
    Args:
        sparse_matrix: The input matrix.
        new_length: The length up to which the diagonal should be elongated.

    Returns:
        Square [input, eye] matrix of size [new_length, new_length]
    """
    # assert square matrix.
    assert (
        sparse_matrix.shape[0] == sparse_matrix.shape[1]
    ), "wavelet matrices are square"
    assert new_length > sparse_matrix.shape[0],\
        "cant add negatively many entries."
    x = torch.arange(sparse_matrix.shape[0], new_length,
                     dtype=sparse_matrix.dtype,
                     device=sparse_matrix.device)
    y = torch.arange(sparse_matrix.shape[0], new_length,
                     dtype=sparse_matrix.dtype,
                     device=sparse_matrix.device)
    extra_indices = torch.stack([x, y])
    extra_values = torch.ones(
        [new_length - sparse_matrix.shape[0]], dtype=sparse_matrix.dtype,
        device=sparse_matrix.device)
    new_indices = torch.cat(
        [sparse_matrix.coalesce().indices(), extra_indices], -1)
    new_values = torch.cat(
        [sparse_matrix.coalesce().values(), extra_values], -1)
    new_matrix = torch.sparse_coo_tensor(new_indices, new_values)
    return new_matrix


def _construct_a(wavelet, length: int,
                 device: torch.device = torch.device("cpu"),
                 dtype=torch.float64) -> torch.tensor:
    """ Construct a raw analysis matrix.
        The resulting matrix will only be orthogonal in the Haar case,
        in most cases you will want to use construct_boundary_a instead.

    Args:
        wavelet (pywt.Wavelet): The wavelet filter to use.
        length (int): The length of the input signal to transfrom.
        device (torch.device, optional): Where to create the matrix.
            Choose cpu or GPU Defaults to torch.device("cpu").
        dtype (optional): The desired torch datatype. Choose torch.float32
            or torch.float64. Defaults to torch.float64.

    Returns:
        torch.tensor: The sparse raw analysis matrix.
    """
    dec_lo, dec_hi, _, _ = get_filter_tensors(
        wavelet, flip=False, device=device, dtype=dtype)
    analysis_lo = construct_strided_conv_matrix(
        dec_lo.squeeze(), length, 2, 'sameshift')
    analysis_hi = construct_strided_conv_matrix(
        dec_hi.squeeze(), length, 2, 'sameshift')
    analysis = torch.cat([analysis_lo, analysis_hi])
    return analysis


def _construct_s(wavelet, length: int,
                 device: torch.device = torch.device("cpu"),
                 dtype=torch.float64) -> torch.tensor:
    """ Create a raw synthesis matrix.

    Args:
        wavelet (pywt.Wavelet): The wavelet object to use.
        length (int): The lenght of the originally transformed signal.
        device (torch.device, optional): Choose cuda or cpu.
            Defaults to torch.device("cpu").
        dtype ([type], optional): The desired data type. Choose torch.float32
            or torch.float64. Defaults to torch.float64.

    Returns:
        torch.tensor: The raw sparse synthesis matrix.
    """
    _, _, rec_lo, rec_hi = get_filter_tensors(
        wavelet, flip=True, device=device, dtype=dtype)
    synthesis_lo = construct_strided_conv_matrix(
        rec_lo.squeeze(), length, 2, 'sameshift')
    synthesis_hi = construct_strided_conv_matrix(
        rec_hi.squeeze(), length, 2, 'sameshift')
    synthesis = torch.cat([synthesis_lo, synthesis_hi])
    return synthesis.transpose(0, 1)


def _get_to_orthogonalize(
        matrix: torch.Tensor, filt_len: int) -> torch.Tensor:
    """Find matrix rows with fewer entries than filt_len.
       These rows will need to be orthogonalized.

    Args:
        matrix (torch.Tensor): The wavelet matrix under consideration.
        filt_len (int): The number of entries we would expect per row.

    Returns:
        (torch.tensor): The row indices with too few entries.
    """
    unique, count = torch.unique_consecutive(
        matrix.coalesce().indices()[0, :], return_counts=True)
    return unique[count != filt_len]


def orthogonalize(matrix: torch.Tensor, filt_len: int,
                  method: str = 'qr') -> torch.Tensor:
    """ Orthogonalization for sparse filter matrices.

    Args:
        matrix (torch.Tensor): The sparse filter matrix to orthogonalize.
        filt_len (int): The length of the wavelet filter coefficients.
        method (str): The orthogonalization method to use. Choose qr
            or gramschmidt. The dense qr code will run much faster
            than sparse gramschidt. Choose gramschmidt if qr fails.
            Defaults to qr.

    Returns:
        torch.Tensor: Orthogonal sparse transformation matrix.
    """
    to_orthogonalize = _get_to_orthogonalize(matrix, filt_len)
    if len(to_orthogonalize) > 0:
        if method == 'qr':
            matrix = _orth_by_qr(matrix, to_orthogonalize)
        else:
            matrix = _orth_by_gram_schmidt(matrix, to_orthogonalize)

    return matrix


def matrix_wavedec(data, wavelet, level: int = None,
                   boundary: str = 'qr'):
    """Experimental computation of the sparse matrix fast wavelet transform.
    Args:
        wavelet: A wavelet object.
        data: Batched input data [batch_size, time], should be of even length.
              WARNING: If the input length is odd it will be padded on the
              right to make it even.
        level: The desired level up to which to compute the fwt.
        boundary: The desired approach to boundary value treatment.
            Choose qr or gramschmidt. Defaults to qr.
    Returns: The wavelet coefficients in a single vector.
             As well as the transformation matrices.
    """
    if len(data.shape) == 1:
        # assume time series
        data = data.unsqueeze(0)
    if data.shape[-1] % 2 != 0:
        # odd length input
        # print('input length odd, padding a zero on the right')
        data = torch.nn.functional.pad(data, [0, 1])

    dec_lo, dec_hi, rec_lo, rec_hi = wavelet.filter_bank
    assert len(dec_lo) == len(dec_hi), "All filters must have the same length."
    assert len(dec_hi) == len(rec_lo), "All filters must have the same length."
    assert len(rec_lo) == len(rec_hi), "All filters must have the same length."
    filt_len = len(dec_lo)

    length = data.shape[1]
    split_list = [length]
    fwt_mat_list = []

    if level is None:
        level = int(np.log2(length))
    else:
        assert level > 0, "level must be a positive integer."

    for s in range(1, level + 1):
        if split_list[-1] < filt_len:
            break
        an = construct_boundary_a(
            wavelet, split_list[-1], dtype=data.dtype, boundary=boundary,
            device=data.device)
        if s > 1:
            an = cat_sparse_identity_matrix(an, length)
        fwt_mat_list.append(an)
        new_split_size = length // np.power(2, s)
        split_list.append(new_split_size)
    coefficients = data.T

    for fwt_mat in fwt_mat_list:
        coefficients = torch.sparse.mm(fwt_mat, coefficients)
    split_list.append(length // np.power(2, level))
    return torch.split(coefficients, split_list[1:][::-1]), fwt_mat_list


def construct_boundary_a(wavelet, length: int,
                         device: torch.device = torch.device("cpu"),
                         boundary: str = 'gramschmidt',
                         dtype: torch.dtype = torch.float64):
    """ Construct a boundary-wavelet filter 1d-analysis matrix.

    Args:
        wavelet : The wavelet filter object to use.
        length (int):  The number of entries in the input signal.
        boundary (str): A string indicating the desired boundary treatment.
            Possible options are qr and gramschmidt. Defaults to
            gramschmidt.

    Returns:
        [torch.sparse.FloatTensor]: The analysis matrix.
    """
    a_full = _construct_a(wavelet, length, dtype=dtype, device=device)
    a_orth = orthogonalize(a_full, len(wavelet), method=boundary)
    return a_orth


def construct_boundary_s(wavelet, length,
                         device: torch.device = torch.device('cpu'),
                         boundary: str = 'gramschmidt',
                         dtype=torch.float64):
    """ Construct a boundary-wavelet filter 1d-synthesis matarix.

    Args:
        wavelet : The wavelet filter object to use.
        length (int):  The number of entries in the input signal.
        boundary (str): A string indicating the desired boundary treatment.
            Possible options are qr and gramschmidt. Defaults to
            gramschmidt.

    Returns:
        [torch.sparse.FloatTensor]: The synthesis matrix.
    """
    s_full = _construct_s(wavelet, length, dtype=dtype, device=device)
    s_orth = orthogonalize(
        s_full.transpose(1, 0), len(wavelet), method=boundary)
    return s_orth.transpose(1, 0)


def matrix_waverec(
        coefficients, wavelet, level: int = None,
        boundary: str = 'qr'):
    """Experimental matrix based inverse fast wavelet transform.

    Args:
        coefficients: The coefficients produced by the forward transform.
        wavelet: The wavelet used to compute the forward transform.
        level (int, optional): The level up to which the coefficients
            have been computed.

    Returns:
        The input signal reconstruction.
    """
    # if the coefficients come in a list concatenate!
    if type(coefficients) is tuple:
        coefficients = torch.cat(coefficients, 0)

    filt_len = len(wavelet)
    length = coefficients.shape[0]

    if level is None:
        level = int(np.log2(length))
    else:
        assert level > 0, "level must be a positive integer."

    ifwt_mat_lst = []
    split_lst = [length]
    for s in range(1, level + 1):
        if split_lst[-1] < filt_len:
            break
        sn = construct_boundary_s(
            wavelet, split_lst[-1], dtype=coefficients.dtype,
            boundary=boundary, device=coefficients.device)
        if s > 1:
            sn = cat_sparse_identity_matrix(sn, length)
        ifwt_mat_lst.append(sn)
        new_split_size = length // np.power(2, s)
        split_lst.append(new_split_size)
    reconstruction = coefficients
    for ifwt_mat in ifwt_mat_lst[::-1]:
        reconstruction = torch.sparse.mm(ifwt_mat, reconstruction)
    return reconstruction.T, ifwt_mat_lst[::-1]


if __name__ == '__main__':
    import pywt
    import torch
    import matplotlib.pyplot as plt
    a = _construct_a(pywt.Wavelet("haar"), 20,
                    torch.device('cpu'))
    s = _construct_s(pywt.Wavelet("haar"), 20,
                    torch.device('cpu'))
    plt.spy(torch.sparse.mm(s, a).to_dense(), marker='.')
    plt.show()
