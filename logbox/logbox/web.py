from __future__ import absolute_import

import json
import logging

import tornado
from six.moves import range
from tornado import web

from .core import *

logger = logging.getLogger(__name__)


class EventViewer(object):
    def __init__(self):
        pass

    @staticmethod
    def _trigger_str(trigger):
        if trigger == TRIGGER_SERVICE_START:
            return 'startup'
        elif trigger == TRIGGER_SERVICE_END:
            return 'shutdown'
        elif trigger == TRIGGER_SERVICE_STEP:
            return 'time'
        elif trigger == TRIGGER_PHOTO_SNAPSHOT:
            return 'photo'
        else:
            raise ValueError("Unexpected trigger '{}'.".format(trigger))

    def __call__(self, *args, **kwargs):
        return [
            str(kwargs.get('_id')),
            kwargs.get('time'),
            1 if kwargs.get('img_num_bytes', 0) > 0 else 0,
            self._trigger_str(kwargs.get('trigger')),
            kwargs.get('pil_driver_mode'),
            kwargs.get('pil_cruise_speed'),
            kwargs.get('pil_desired_speed'),
            kwargs.get('veh_velocity'),
            kwargs.get('pil_steering'),
            kwargs.get('pil_throttle'),
            kwargs.get('pil_is_steering_intervention'),
            kwargs.get('pil_is_throttle_intervention'),
            kwargs.get('pil_is_save_event'),
            kwargs.get('veh_gps_latitude'),
            kwargs.get('veh_gps_longitude'),
            kwargs.get('inf_steer_action'),
            kwargs.get('inf_obstruction'),
            kwargs.get('inf_steer_penalty'),
            kwargs.get('inf_obstruction_penalty'),
            kwargs.get('inf_running_penalty')
        ]


class DataTableRequestHandler(web.RequestHandler):
    # noinspection PyAttributeOutsideInit
    def initialize(self, **kwargs):
        self._box = kwargs.get('mongo_box')
        self._view = EventViewer()

    def data_received(self, chunk):
        pass

    @tornado.gen.coroutine
    def get(self):
        # logger.info(self.request.arguments)
        try:
            # Parse the draw as integer for security reasons: https://datatables.net/manual/server-side.
            draw = int(self.get_query_argument('draw'))
            start = int(self.get_query_argument('start'))
            length = int(self.get_query_argument('length'))
            time_order = self.get_query_argument('order[0][dir]')
            time_order = 1 if time_order == 'desc' else -1
        except ValueError:
            draw = 0
            start = 0
            length = 10
            time_order = -1

        c_total, cursor = self._box.paginate_events(start=start, length=length, order=time_order)
        data = []
        for _ in range(length):
            try:
                data.append(self._view(**next(cursor)))
            except StopIteration:
                break

        blob = dict(
            draw=draw,
            recordsTotal=c_total,
            recordsFiltered=c_total,
            data=data
        )
        # Include the error property when errors occur.
        message = json.dumps(blob)
        response_code = 200
        self.set_header('Content-Type', 'application/json')
        self.set_header('Content-Length', len(message))
        self.set_status(response_code)
        self.write(message)


class JPEGImageRequestHandler(web.RequestHandler):
    # noinspection PyAttributeOutsideInit
    def initialize(self, **kwargs):
        self._box = kwargs.get('mongo_box')

    def data_received(self, chunk):
        pass

    @tornado.gen.coroutine
    def get(self):
        _fields = self._box.load_event_image_fields(self.get_query_argument('object_id'))
        _written = False
        if _fields is not None:
            _exists = _fields[1] > 0
            if _exists:
                img = cv2.resize(cv2_image_from_bytes(_fields[-1]), (200, 80))
                _bytes = jpeg_encode(img).tobytes()
                self.set_status(200)
                self.set_header('Content-Type', 'image/jpeg')
                self.set_header('Content-Length', len(_bytes))
                self.write(_bytes)
                _written = True
        if not _written:
            self.set_status(404)
            self.write(u'')