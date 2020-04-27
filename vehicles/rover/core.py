import Queue
import logging
import math
import multiprocessing
import threading
import time

import cv2
import numpy as np
import requests
import rospy
from geometry_msgs.msg import Twist, TwistStamped
from requests.auth import HTTPDigestAuth

from byodr.utils import timestamp
from byodr.utils.option import hash_dict, parse_option, PropertyError
from video import GstRawSource

logger = logging.getLogger(__name__)

CH_NONE, CH_THROTTLE, CH_STEERING, CH_BOTH = (0, 1, 2, 3)
CTL_LAST = 0

# Safety - cap the range that is availble through user configuration.
# Ranges are from the servo domain, i.e. 1/90 - the pair is for forwards and reverse.
# The ranges do not have to be symmetrical - backwards may seem faster here but not in practice (depends on the esc settings).
D_SPEED_PROFILES = {
    'economy': (5, 6),
    'default': (6, 8),
    'sport': (8, 10),
    'performance': (10, 10)
    # Yep, what's next?
}


class RosGate(object):
    """
         rostopic hz /roy_teleop/sensor/odometer
         subscribed to [/roy_teleop/sensor/odometer]
         average rate: 81.741
    """

    def __init__(self, **kwargs):
        self._errors = []
        self._hash = -1
        self._rps = 0  # Hall sensor revolutions per second.
        self._publisher = None
        self._subscriber = None
        self._lock = threading.Lock()
        self.restart(**kwargs)

    def restart(self, **kwargs):
        with self._lock:
            _hash = hash_dict(**kwargs)
            if _hash != self._hash:
                self._hash = _hash
                if self._subscriber:
                    self._subscriber.unregister()
                if self._publisher:
                    self._publisher.unregister()
                self._start(**kwargs)

    def get_errors(self):
        with self._lock:
            return self._errors

    def _start(self, **kwargs):
        errors = []
        self._dry_run = parse_option('dry.run', (lambda x: bool(int(x))), False, errors, **kwargs)
        self._steer_shift = parse_option('calibrate.steer.shift', int, 0, errors, **kwargs)
        self._throttle_zero = parse_option('calibrate.throttle.zero.position', int, 0, errors, **kwargs)
        self._throttle_reverse = parse_option('calibrate.throttle.reverse.position', int, 0, errors, **kwargs)
        self._wheel_radius = parse_option('chassis.wheel.radius.meter', float, 0, errors, **kwargs)
        self._circum_m = 2 * math.pi * self._wheel_radius
        self._gear_ratio = parse_option('chassis.hall.ticks.per.rotation', int, 0, errors, **kwargs)
        self._forward_shift = parse_option('throttle.forward.shift', int, 0, errors, **kwargs)
        self._backward_shift = parse_option('throttle.backward.shift', int, 0, errors, **kwargs)
        self._forward_range = 0
        self._backward_range = 0
        _profile_name = parse_option('throttle.speed.profile', str, 0, errors, **kwargs)
        if _profile_name in D_SPEED_PROFILES:
            # Scale by whats left of the servo domain.
            _forward_scale = 1. / min(1, (90 - self._throttle_zero - self._forward_shift))
            _backward_scale = 1. / min(1, (90 - self._throttle_zero - self._backward_shift))
            self._forward_range = (D_SPEED_PROFILES[_profile_name][0]) * _forward_scale
            self._backward_range = (D_SPEED_PROFILES[_profile_name][1]) * _backward_scale
        else:
            errors.append(PropertyError('throttle.speed.profile', 'Not recognized', suggestions=D_SPEED_PROFILES.keys()))
        self._errors = errors
        if not self._dry_run:
            logger.info("Starting ROS gate - wheel radius is {:2.2f}m and sensor tick ratio is {}.".format(
                self._wheel_radius, self._gear_ratio)
            )
            self._subscriber = rospy.Subscriber("roy_teleop/sensor/odometer", TwistStamped, self._update_odometer)
            self._publisher = rospy.Publisher('roy_teleop/command/drive', Twist, queue_size=1)

    def _update_odometer(self, message):
        # The odometer publishes revolutions per second.
        self._rps = float(message.twist.linear.y)

    def publish(self, throttle=0., steering=0., reverse_gear=False):
        # The microcontroller must know the shifts for true zero positions and thus adds the shift itself.
        # The esc can only be set into reverse using the correct servo value.
        # The negative reverse servo throttle should not be issued when the esc is already in reverse.
        with self._lock:
            throttle = max(-1., min(1., throttle))
            steering = max(-1., min(1., steering))
            servo_throttle = 0
            if throttle < -.92 and reverse_gear:
                servo_throttle = self._throttle_reverse
            elif throttle < 0:
                servo_throttle = self._backward_shift + int(throttle * self._backward_range)
            elif throttle > 0:
                servo_throttle = self._forward_shift + int(throttle * self._forward_range)
            # Fill and send.
            twist = Twist()
            twist.angular.x = self._steer_shift
            twist.angular.y = 90 + int(90 * steering)
            twist.linear.x = self._throttle_zero
            twist.linear.y = 90 + servo_throttle
            if self._dry_run:
                if abs(throttle) > 1e-2:
                    logger.info("Twist angular=({}, {}) and linear=({}, {}).".format(
                        twist.angular.x, twist.angular.y,
                        twist.linear.x, twist.linear.y
                    ))
            else:
                self._publisher.publish(twist)

    def get_odometer_value(self):
        # Convert to travel speed in meters per second.
        return (self._rps / self._gear_ratio) * self._circum_m


class TwistHandler(object):
    def __init__(self, **kwargs):
        super(TwistHandler, self).__init__()
        self._gate = RosGate(**kwargs)
        self._quit_event = multiprocessing.Event()

    def _drive(self, steering=0, throttle=0, reverse_gear=False):
        try:
            if not self._quit_event.is_set():
                self._gate.publish(steering=steering, throttle=throttle, reverse_gear=reverse_gear)
        except Exception as e:
            logger.error("{}".format(e))

    def restart(self, **kwargs):
        if not self._quit_event.is_set():
            self._gate.restart(**kwargs)

    def get_errors(self):
        return self._gate.get_errors()

    def state(self):
        x, y = 0, 0
        return dict(x_coordinate=x,
                    y_coordinate=y,
                    heading=0,
                    velocity=self._gate.get_odometer_value(),
                    time=timestamp())

    def quit(self):
        self._quit_event.set()

    def noop(self):
        self._drive(steering=0, throttle=0)

    def drive(self, pilot, teleop):
        if pilot is None:
            self.noop()
        else:
            _reverse = teleop and teleop.get('arrow_down', 0)
            self._drive(steering=pilot.get('steering'), throttle=pilot.get('throttle'), reverse_gear=_reverse)


class CameraPtzThread(threading.Thread):
    def __init__(self, url, user, password, preset_duration_sec=3.8, scale=100, speed=1., flip=(1, 1)):
        super(CameraPtzThread, self).__init__()
        self._quit_event = multiprocessing.Event()
        self._preset_duration = preset_duration_sec
        self._scale = scale
        self._speed = speed
        self._flip = flip
        self._auth = None
        self._url = url
        self._ptz_xml = """
        <PTZData version='2.0' xmlns='http://www.isapi.org/ver20/XMLSchema'>
            <pan>{pan}</pan>
            <tilt>{tilt}</tilt>
            <zoom>0</zoom>
        </PTZData>
        """
        self.set_auth(user, password)
        self._queue = Queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._previous = (0, 0)

    def set_url(self, url):
        with self._lock:
            self._url = url

    def set_auth(self, user, password):
        with self._lock:
            self._auth = HTTPDigestAuth(user, password)

    def set_speed(self, speed):
        with self._lock:
            self._speed = speed

    def set_flip(self, flip):
        with self._lock:
            self._flip = flip

    def _norm(self, value):
        return max(-self._scale, min(self._scale, int(self._scale * value * self._speed)))

    def _perform(self, operation):
        # Goto a preset position takes time.
        prev = self._previous
        if type(prev) == tuple and prev[0] == 'goto_home' and time.time() - prev[1] < self._preset_duration:
            pass
        elif operation != prev:
            ret = self._run(operation)
            if ret and ret.status_code != 200:
                logger.warn("Got status {} on operation {}.".format(ret.status_code, operation))

    def _run(self, operation):
        ret = None
        prev = self._previous
        if type(operation) == tuple and operation[0] == 'set_home':
            x_count = prev[1] if type(prev) == tuple and prev[0] == 'set_home' else 0
            if x_count >= 100 and operation[1]:
                logger.info("Saving ptz home position.")
                self._previous = 'ptz_home_set'
                ret = requests.put(self._url + '/homeposition', auth=self._auth)
            else:
                self._previous = ('set_home', x_count + 1)
        elif operation == 'goto_home':
            self._previous = ('goto_home', time.time())
            ret = requests.put(self._url + '/homeposition/goto', auth=self._auth)
        else:
            pan, tilt = operation
            self._previous = operation
            ret = requests.put(self._url + '/continuous', data=self._ptz_xml.format(**dict(pan=pan, tilt=tilt)), auth=self._auth)
        return ret

    def add(self, pilot, teleop):
        try:
            if pilot and pilot.get('driver') == 'driver_mode.teleop.direct' and teleop:
                self._queue.put_nowait(teleop)
        except Queue.Full:
            pass

    def quit(self):
        self._quit_event.set()

    def run(self):
        while not self._quit_event.is_set():
            try:
                cmd = self._queue.get(block=True, timeout=0.050)
                with self._lock:
                    operation = (0, 0)
                    if cmd.get('button_x', 0):
                        operation = ('set_home', cmd.get('button_a', 0))
                    elif any([cmd.get(k, 0) for k in ('button_y', 'button_a')]):
                        operation = 'goto_home'
                    elif 'pan' in cmd and 'tilt' in cmd:
                        operation = (self._norm(cmd.get('pan')) * self._flip[0], self._norm(cmd.get('tilt')) * self._flip[1])
                    self._perform(operation)
            except Queue.Empty:
                pass


class PTZCamera(object):
    def __init__(self, **kwargs):
        self._errors = []
        self._hash = -1
        self._camera = None
        self._lock = threading.Lock()
        self.restart(**kwargs)

    def restart(self, **kwargs):
        with self._lock:
            _hash = hash_dict(**kwargs)
            if _hash != self._hash:
                self._hash = _hash
                self._start(**kwargs)

    def get_errors(self):
        with self._lock:
            return self._errors

    def _start(self, **kwargs):
        errors = []
        ptz_enabled = parse_option('camera.ptz.enabled', (lambda x: bool(int(x))), False, errors, **kwargs)
        if ptz_enabled:
            _server = parse_option('camera.ip', str, errors=errors, **kwargs)
            _user = parse_option('camera.user', str, errors=errors, **kwargs)
            _password = parse_option('camera.password', str, errors=errors, **kwargs)
            _protocol = parse_option('camera.ptz.protocol', str, errors=errors, **kwargs)
            _path = parse_option('camera.ptz.path', str, errors=errors, **kwargs)
            _flip = parse_option('camera.ptz.flip', str, errors=errors, **kwargs)
            _speed = parse_option('camera.ptz.speed', float, 1.0, errors=errors, **kwargs)
            _flipcode = [1, 1]
            if _flip in ('pan', 'tilt', 'both'):
                _flipcode[0] = -1 if _flip in ('pan', 'both') else 1
                _flipcode[1] = -1 if _flip in ('tilt', 'both') else 1
            _port = 80 if _protocol == 'http' else 443
            _url = '{protocol}://{server}:{port}{path}'.format(**dict(protocol=_protocol, server=_server, port=_port, path=_path))
            logger.info("PTZ camera url={}.".format(_url))
            if self._camera is None:
                self._camera = CameraPtzThread(_url, _user, _password, speed=_speed, flip=_flipcode)
                self._camera.start()
            elif len(errors) == 0:
                self._camera.set_url(_url)
                self._camera.set_auth(_user, _password)
                self._camera.set_speed(_speed)
                self._camera.set_flip(_flipcode)
        # Already under lock.
        elif self._camera:
            self._camera.quit()
        self._errors = errors

    def add(self, pilot, teleop):
        with self._lock:
            if self._camera:
                self._camera.add(pilot, teleop)

    def close(self):
        with self._lock:
            if self._camera:
                self._camera.quit()
                self._camera.join()
                self._camera = None


class GstSource(object):
    def __init__(self, image_publisher, **kwargs):
        self.errors = []
        self._image_publisher = image_publisher
        self._camera_shape = None
        self._flipcode = None
        self._hash = -1
        self._source = None
        self._lock = threading.Lock()
        self.restart(**kwargs)

    def restart(self, **kwargs):
        with self._lock:
            _hash = hash_dict(**kwargs)
            if _hash != self._hash:
                self._hash = _hash
                self._start(**kwargs)

    def _publish(self, _b):
        if self._camera_shape is not None:
            _img = np.fromstring(_b.extract_dup(0, _b.get_size()), dtype=np.uint8).reshape(self._camera_shape)
            self._image_publisher.publish(cv2.flip(_img, self._flipcode) if self._flipcode else _img)

    def _start(self, **kwargs):
        errors = []
        _server = parse_option('camera.ip', str, errors=errors, **kwargs)
        _user = parse_option('camera.user', str, errors=errors, **kwargs)
        _password = parse_option('camera.password', str, errors=errors, **kwargs)
        _rtsp_port = parse_option('camera.rtsp.port', int, 0, errors=errors, **kwargs)
        _rtsp_path = parse_option('camera.rtsp.path', str, errors=errors, **kwargs)
        _img_wh = parse_option('camera.image.shape', str, errors=errors, **kwargs)
        _img_flip = parse_option('camera.image.flip', str, errors=errors, **kwargs)
        _shape = [int(x) for x in _img_wh.split('x')]
        _shape = (_shape[1], _shape[0], 3)
        _rtsp_url = 'rtsp://{user}:{password}@{ip}:{port}{path}'.format(
            **dict(user=_user, password=_password, ip=_server, port=_rtsp_port, path=_rtsp_path)
        )
        _url = "rtspsrc " \
               "location={} " \
               "latency=0 drop-on-latency=true ! queue ! " \
               "rtph264depay ! h264parse ! queue ! avdec_h264 ! videoconvert ! " \
               "videoscale ! video/x-raw,format=BGR ! queue".format(_rtsp_url)

        # flipcode = 0: flip vertically
        # flipcode > 0: flip horizontally
        # flipcode < 0: flip vertically and horizontally
        if _img_flip in ('both', 'vertical', 'horizontal'):
            self._flipcode = 0 if _img_flip == 'vertical' else 1 if _img_flip == 'horizontal' else -1

        self.errors = errors
        if len(errors) == 0:
            # Do not use our method - already under lock.
            if self._source:
                self._source.close()
            logger.info("Camera rtsp url = {}.".format(_rtsp_url))
            logger.info("Using image flipcode={}".format(self._flipcode))
            self._source = GstRawSource(fn_callback=self._publish, command=_url)
            self._source.open()

    def check(self):
        with self._lock:
            if self._source:
                self._source.check()

    def close(self):
        with self._lock:
            if self._source:
                self._source.close()