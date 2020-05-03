if (page_utils.get_stream_type() == 'h264') {
    class CameraSocketResumer {
        constructor(uri, reconnect_ms) {
            this.uri = uri;
            this.reconnect_ms = reconnect_ms;
        }

        onopen(player) {
            player.playStream();
        }

        onclose(player) {
            setTimeout(function() {
                player.connect(this.uri);
            }, this.reconnect_ms);
        }
    }

    var camera_player = {
        el_canvas: null,
        wsavc: null,

        init: function(parent, port) {
            this.el_canvas = document.createElement("canvas");
            parent.appendChild(this.el_canvas);
            uri = "ws://" + document.location.hostname + ':' + port;
            this.wsavc = new WSAvcPlayer(this.el_canvas, "webgl", new CameraSocketResumer(uri, 100));
            this.wsavc.connect(uri);
        }
    }

    document.addEventListener("DOMContentLoaded", function() {
        camera_player.init(document.getElementById('camera1'), 9101);
    });
}