import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable


def conditionalImport(hparams):
	"""
	'Import' different model modules, based on hparams.
	"""
	Invertible1x1Conv = getInvertableConv(hparams)
	AffineCouplingBlock = getAffineCouplingBlock(hparams)
	WN = getWN(hparams)
	Upsampler = getUpsampler(hparams)
	Squeezer = getSqueezer(hparams)
	return Invertible1x1Conv, AffineCouplingBlock, WN, Upsampler, Squeezer


def getInvertableConv(hparams):
    InvertableConv_source = hparams.InvertableConv_source
    core_type = hparams.core_type
    if InvertableConv_source.lower() == "NVIDIA".lower():
        from models.NVIDIA.Invertible1x1Conv import Invertible1x1Conv
        return Invertible1x1Conv
    elif InvertableConv_source.lower() == "yoyo".lower():
        if core_type.lower() == "WaveGlow".lower():
            from models.yoyololicon.Invertible1x1Conv_1d import Invertible1x1Conv
            return Invertible1x1Conv
        elif core_type.lower() == "WaveFlow".lower():
            from models.yoyololicon.Invertible1x1Conv_2d import Invertible1x1Conv
            return Invertible1x1Conv
        else:
            raise NotImplementedError
    elif InvertableConv_source.lower() == "None".lower() or InvertableConv_source is None:
        return None
    else:
        raise NotImplementedError


def getAffineCouplingBlock(hparams):
	AffineCouplingBlock_source = hparams.AffineCouplingBlock_source
	if AffineCouplingBlock_source.lower() == "NVIDIA".lower():
		raise NotImplementedError
		from models.NVIDIA.AffineCouplingBlock import AffineCouplingBlock
		return AffineCouplingBlock
	elif AffineCouplingBlock_source.lower() == "yoyo".lower():
		from models.yoyololicon.AffineCouplingBlock import AffineCouplingBlock
		return AffineCouplingBlock
	else:
		raise NotImplementedError


def getWN(hparams):
    WN_source = hparams.WN_source
    core_type = hparams.core_type
    if WN_source.lower() == "NVIDIA".lower():
        if core_type.lower() == "WaveGlow".lower():
            from models.NVIDIA.WN_1d import WN
            return WN
        elif core_type.lower() == "WaveFlow".lower():
            from models.NVIDIA.WN_2d import WN
            return WN
        else:
            raise NotImplementedError
    elif WN_source.lower() == "yoyo".lower():
        if core_type.lower() == "WaveGlow".lower():
            from models.yoyololicon.WN_1d import WN
            return WN
        elif core_type.lower() == "WaveFlow".lower():
            from models.yoyololicon.WN_2d import WN
            return WN
        else:
            raise NotImplementedError
    else:
        raise NotImplementedError


def getUpsampler(hparams):
	upsampler_source = hparams.upsampler_source
	if upsampler_source.lower() == "NVIDIA".lower():
		from models.NVIDIA.upsampler import Upsampler
		return Upsampler
	elif upsampler_source.lower() == "yoyo".lower():
		from models.yoyololicon.upsampler import Upsampler
		return Upsampler
	elif upsampler_source.lower() == "cookie".lower():
		from models.cookie.upsampler import Upsampler
		return Upsampler
	elif upsampler_source.lower() == "L0SG".lower():
		from models.L0SG.upsampler import Upsampler
		return Upsampler
	else:
		raise NotImplementedError


def getSqueezer(hparams):
	squeezer_source = hparams.squeezer_source
	if squeezer_source.lower() == "NVIDIA".lower():
		from models.NVIDIA.squeeze_to_vector import Squeezer
		return Squeezer
	elif squeezer_source.lower() == "PaddlePaddle".lower():
		from models.PaddlePaddle.squeeze_to_vector import Squeezer
		return Squeezer
	elif squeezer_source.lower() == "L0SG".lower():
		from models.L0SG.squeeze_to_vector import Squeezer
		return Squeezer
	elif squeezer_source.lower() == "yoyo".lower():
		from models.yoyololicon.squeeze_to_vector import Squeezer
		return Squeezer
	else:
		raise NotImplementedError
