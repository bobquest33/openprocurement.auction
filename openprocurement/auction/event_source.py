from sse import Sse as PySse
from flask import json, current_app, Blueprint, request, abort, session
from gevent.queue import Queue
import logging

LOGGER = logging.getLogger(__name__)


class SseStream(object):
    def __init__(self, queue, bidder_id=None, client_id=None):
        self.queue = queue
        self.client_id = client_id
        self.bidder_id = bidder_id

    def __iter__(self):
        sse = PySse()
        for data in sse:
            yield data.encode('u8')

        while True:
            message = self.queue.get()
            LOGGER.debug(' '.join([
                'Event Message to bidder:', str(self.bidder_id), ' Client:',
                str(self.client_id), 'MSG:', str(repr(message))
            ]))
            sse.add_message(message['event'], json.dumps(message['data']))
            for data in sse:
                yield data.encode('u8')


sse = Blueprint('sse', __name__)


@sse.route("/event_source")
def event_source():
    if 'remote_oauth' in session and 'client_id' in session:
        resp = current_app.remote_oauth.get('me')
        if resp.status == 200:
            bidder = resp.data['bidder_id']
            client_hash = session['client_id']
            if bidder not in current_app.auction_bidders:
                current_app.auction_bidders[bidder] = {
                    "clients": {},
                    "channels": {}
                }

            if client_hash not in current_app.auction_bidders[bidder]:
                current_app.auction_bidders[bidder]["clients"][client_hash] = {
                    'ip': request.headers.get('X-Forwarded-For'),
                    'User-Agent': request.headers.get('User-Agent'),
                }
                current_app.auction_bidders[bidder]["channels"][client_hash] = Queue()

            current_app.auction_bidders[bidder]["channels"][client_hash].put({
                "event": "Identification",
                "data": {"bidder_id": bidder}
            })
            send_event(
                bidder,
                current_app.auction_bidders[bidder]["clients"],
                "ClientsList"
            )
            return current_app.response_class(
                SseStream(
                    current_app.auction_bidders[bidder]["channels"][client_hash],
                    bidder_id=bidder,
                    client_id=client_hash
                ),
                direct_passthrough=True,
                mimetype='text/event-stream',
            )
    current_app.logger.debug('Disable event_source for anonimous.')
    return abort(401)


def send_event(bidder, data, event=""):
    for key in current_app.auction_bidders[bidder]["channels"]:
        current_app.auction_bidders[bidder]["channels"][key].put({
            "event": event,
            "data": data
        })
    return True