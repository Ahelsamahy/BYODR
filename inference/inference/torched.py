from __future__ import absolute_import

import glob
import logging
import multiprocessing
import os

import numpy as np
import onnxruntime as ort

from .image import hwc_to_chw

logger = logging.getLogger(__name__)


class DynamicMomentum(object):
    """Low-pass filter with separate acceleration and deceleration momentum."""

    def __init__(self, up=.1, down=.9, ceiling=1.):
        self._previous_value = 0
        self._acceleration = up
        self._deceleration = down
        self._ceiling = ceiling

    def calculate(self, value):
        _momentum = self._acceleration if value > self._previous_value else self._deceleration
        _new_value = min(self._ceiling, _momentum * value + (1. - _momentum) * self._previous_value)
        self._previous_value = _new_value
        return _new_value


def _newest_file(paths, pattern):
    # Find the newest file regardless which directory it may come from.
    if isinstance(paths, tuple) or isinstance(paths, list):
        files = [_newest_file(path, pattern) for path in paths]
        files = [f for f in files if f is not None]
    else:
        path = paths
        files = [] if path is None else glob.glob(os.path.join(os.path.expanduser(path), pattern))
    match = max(files, key=os.path.getmtime) if len(files) > 0 else None
    return match


def _maneuver_index(turn='general.fallback'):
    _options = {'general.fallback': 0, 'intersection.left': 1, 'intersection.ahead': 2, 'intersection.right': 3}
    return _options[turn]


def _index_maneuver(index=0):
    _options = {0: 'general.fallback', 1: 'intersection.left', 2: 'intersection.ahead', 3: 'intersection.right'}
    return _options[index]


def maneuver_intention(turn='general.fallback', dtype=np.float32):
    command = np.zeros(4, dtype=dtype)
    command[_maneuver_index(turn=turn)] = 1
    return command


class TRTDriver(object):
    def __init__(self, user_directory, internal_directory, gpu_id=0, runtime_compilation=1):
        self._gpu_id = gpu_id
        self._rt_compile = runtime_compilation
        self.model_directories = [user_directory, internal_directory]
        self._lock = multiprocessing.Lock()
        self._zero_vector = np.zeros(shape=(75,), dtype=np.float32)
        # self._rt_options = {'device_id': str(gpu_id), 'trt_max_workspace_size': str(1 << 25), 'trt_fp16_enable': 'True'}
        # self._im_mean, self._im_std = [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]
        # self._img_transform = transforms.Compose([
        #     transforms.ToTensor(),
        #     transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        # ])
        self._sess = None
        self._onnx_file = None

    def _activate(self):
        rt_file = _newest_file(self.model_directories, 'runtime*.onnx')
        if rt_file is None or not os.path.isfile(rt_file):
            logger.warning("Missing optimized graph.")
            return

        logger.info("Located optimized graph '{}'.".format(rt_file))
        self._sess = ort.InferenceSession(rt_file)
        # self._sess.set_providers(['CPUExecutionProvider'])
        self._sess.set_providers(["CUDAExecutionProvider"], [{'device_id': str(self._gpu_id)}])
        self._onnx_file = rt_file

    def _deactivate(self):
        del self._sess
        self._sess = None
        self._onnx_file = None

    def _img_prepare(self, image):
        # return self._img_transform(image).cpu().numpy()
        # mean, std = self._im_mean, self._im_std
        # image = hwc_to_chw(image) / 255.
        # for channel in range(3):
        #     image[channel] = (image[channel] - mean[channel]) / std[channel]
        return hwc_to_chw(image)

    def will_compile(self):
        rt_file = _newest_file(self.model_directories, 'runtime*.onnx')
        return rt_file is not None and (self._onnx_file is None or os.path.getmtime(rt_file) > os.path.getmtime(self._onnx_file))

    def deactivate(self):
        with self._lock:
            self._deactivate()

    def activate(self):
        with self._lock:
            self._activate()

    def reactivate(self):
        with self._lock:
            self._deactivate()
            self._activate()

    def features(self, dave_image, alex_image):
        _out = self._forward_all(dave_image, alex_image)
        _coord, _key, _value = _out[6], _out[8], _out[9]
        return _coord, _key, _value

    def forward(self, dave_image, alex_image, maneuver_command=maneuver_intention(), destination=None):
        _out = self._forward_all(dave_image, alex_image, maneuver_command, destination)
        _action, _critic, _surprise, _gumbel, _brake, _br_critic, _coord, _query, _key, _value = _out
        return _action, _critic, _surprise, _gumbel, _brake, _br_critic, _coord, _query

    def _forward_all(self, dave_image, alex_image, maneuver_command=maneuver_intention(), destination=None):
        with self._lock:
            assert dave_image.dtype == np.uint8 and alex_image.dtype == np.uint8, "Expected np.uint8 images."
            assert self._sess is not None, "There is no session - run activation prior to calling this method."
            destination = self._zero_vector if destination is None else destination
            _feed = {
                'input/dave_image': np.array([self._img_prepare(dave_image)], dtype=np.uint8),
                'input/alex_image': np.array([self._img_prepare(alex_image)], dtype=np.uint8),
                'input/maneuver_command': np.array([maneuver_command], dtype=np.float32),
                'input/current_destination': np.array([destination], dtype=np.float32)
            }
            _out = [x.flatten() for x in self._sess.run(None, _feed)]
            _steering, _critic, _surprise, _gumbel, _brake, _br_critic, _coord, _query, _key, _value = _out
            return _steering, _critic, _surprise, _gumbel, _brake, _br_critic, _coord, _query, _key, _value