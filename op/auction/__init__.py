import argparse
import logging
# import requests
import iso8601
import couchdb
import json

from copy import deepcopy
from datetime import timedelta, datetime
from pytz import timezone
from multiprocessing import Event
from apscheduler.schedulers.background import BackgroundScheduler
from .server import run_server
from string import Template
SCHEDULER = BackgroundScheduler()
SCHEDULER.timezone = timezone('Europe/Kiev')

logging.basicConfig(level=logging.INFO,
                    format='%(levelname)s[%(asctime)s]: %(message)s')

PREMELIMITARY_BIDS_TEMPLATE = Template('''{
    "type": "premelimitary_bids",
    "start": "$start_time",
    "label": {"en": "Premelimitary bids"}
}''')

PAUSE_TEMPLATE = Template('''{
    "type": "pause",
    "start": "$start_time",
    "label": {"en": "Pause"}
}''')

BIDS_TEMPLATE = Template('''{
    "type": "bids",
    "bidder_id": "$bidder_id",
    "start": "$start_time",
    "label": {"en": "$bidder_name"},
    "amount": $amount
}''')

ROUNDS = 3
PAUSE_SECONDS = 5
BIDS_SECONDS = 10
PREMELIMITARY_BIDS_SECONDS = 5


class Auction(object):
    """docstring for Auction"""
    def __init__(self, auction_doc_id, port=8888,
                 database_url='http://localhost:9000/auction'):
        super(Auction, self).__init__()
        self.port = port
        self.auction_doc_id = auction_doc_id
        self.tender_url = 'http://api-sandbox.openprocurement.org/tenders/{0}/auction'.format(auction_doc_id)
        self._auction_data = {}
        self._end_auction_event = Event()
        self.database_url = database_url
        self.db = couchdb.client.Database(database_url)

    @property
    def startDate(self):
        date = iso8601.parse_date(
            self._auction_data['data']['period']['startDate']
        )
        if datetime.now(timezone('Europe/Kiev')) > date:
            date = datetime.now(timezone('Europe/Kiev')) + timedelta(seconds=10)
            self._auction_data['data']['period']['startDate'] = date.isoformat()
        return date

    def get_auction_info(self):
        # response = requests.get(self.tender_url)
        # if response.ok:
        #     self._auction_data = response.json()
        # else:
        self._auction_data = {
            "data": {
                "bids": [{
                    "amount": 500,
                    "currency": "UAH"
                }, {
                    "amount": 485,
                    "currency": "UAH"
                }],
                "minimalStep": {
                    "amount": 35,
                    "currency": "UAH"
                },
                "period": {
                    "startDate": "2014-10-29T14:13:00+02:00"
                }
            }
        }
        self.bidders_count = len(self._auction_data["data"]["bids"])

    def schedule_auction(self):
        self.get_auction_info()
        # Schedule Auction Workflow
        doc = self.db.get(self.auction_doc_id)
        if doc:
            self.db.delete(doc)
        auction_document = {"_id": self.auction_doc_id, "stages": [], "current_stage": -1}
        # Schedule PREMELIMITARY_BIDS
        premelimitary_bids = json.loads(PREMELIMITARY_BIDS_TEMPLATE.substitute(
            start_time=self.startDate.isoformat()
        ))
        auction_document['stages'].append(premelimitary_bids)
        SCHEDULER.add_job(self.start_auction, 'date', run_date=self.startDate)

        next_stage_timedelta = self.startDate + timedelta(
            seconds=PREMELIMITARY_BIDS_SECONDS
        )
        SCHEDULER.add_job(
            self.end_premelimitary_bids, 'date',
            run_date=next_stage_timedelta,
        )
        # Schedule Bids Rounds
        for round_id in xrange(ROUNDS):
            # Schedule PAUSE Stage
            pause_stage = json.loads(PAUSE_TEMPLATE.substitute(
                start_time=next_stage_timedelta.isoformat()
            ))
            auction_document['stages'].append(pause_stage)
            next_stage_timedelta += timedelta(seconds=PAUSE_SECONDS)
            SCHEDULER.add_job(
                self.next_stage, 'date',
                run_date=next_stage_timedelta,
            )

            # Schedule BIDS Stages
            for index in xrange(self.bidders_count):
                bid_stage = json.loads(BIDS_TEMPLATE.substitute(
                    start_time=next_stage_timedelta.isoformat(),
                    bidder_id="",
                    bidder_name="",
                    amount="null"
                ))
                auction_document['stages'].append(bid_stage)
                next_stage_timedelta += timedelta(seconds=BIDS_SECONDS)
                if index == self.bidders_count - 1 and round_id != ROUNDS - 1:
                    SCHEDULER.add_job(
                        self.end_round, 'date',
                        run_date=next_stage_timedelta,
                    )
                else:
                    SCHEDULER.add_job(
                        self.next_stage, 'date',
                        run_date=next_stage_timedelta,
                    )

        self.db.save(auction_document)
        self.server = run_server("0.0.0.0", self.port,
                                 db_url=self.database_url,
                                 auction_doc_id=self.auction_doc_id)
        SCHEDULER.add_job(
            self.end_auction, 'date',
            run_date=next_stage_timedelta
        )

    def wait_to_end(self):
        self._end_auction_event.wait()
    
    def start_auction(self):
        logging.info('---------------- Start auction ----------------')
        doc = self.db.get(self.auction_doc_id)
        doc["current_stage"] = 0
        self.db.save(doc)

    def end_premelimitary_bids(self):
        logging.info('---------------- End Premelimitary Bids ----------------')
        doc = self.db.get(self.auction_doc_id)
        # TODO: get premelimitary bids
        bids = deepcopy(self._auction_data['data']['bids'])
        for index, bid in enumerate(sorted(bids,
                                           key=lambda item: item["amount"],
                                           reverse=True)):
            doc["stages"][2 + index] = json.loads(BIDS_TEMPLATE.substitute(
                start_time=doc["stages"][2 + index]["start"],
                bidder_id=index,
                bidder_name="Bidder #{0}".format(index),
                amount=bid["amount"]
            ))
        doc["current_stage"] += 1
        self.db.save(doc)

    def end_round(self):
        logging.info('---------------- End Round ----------------')
        doc = self.db.get(self.auction_doc_id)
        doc["current_stage"] += 1
        bids = deepcopy(doc["stages"][doc["current_stage"] - self.bidders_count:doc["current_stage"]])
        for index, bid in enumerate(sorted(bids,
                                           key=lambda item: item["amount"],
                                           reverse=True)):
            bid["start"] = doc["stages"][doc["current_stage"] + 1 + index]["start"]
            doc["stages"][doc["current_stage"] + 1 + index] = bid
        self.db.save(doc)

    def next_stage(self):
        doc = self.db.get(self.auction_doc_id)
        doc["current_stage"] += 1
        self.db.save(doc)
        logging.info('---------------- Start stage {0} ----------------'.format(doc["current_stage"]))

    def end_auction(self):
        logging.info('---------------- End auction ----------------')
        self.server.terminate()
        self.put_auction_data()
        self._end_auction_event.set()

    def put_auction_data(self):
        # response = requests.put(requests.get(self.tender_url), data=self._auction_data)
        # if response.ok:
        #     logging.info('Auction data submitted')
        # else:
        #     logging.warn('Error while submit auction data: {}'.format(response.text))
        pass


def auction_run(auction_doc_id, port):
    auction = Auction(auction_doc_id, port)
    SCHEDULER.start()
    auction.schedule_auction()
    auction.wait_to_end()
    SCHEDULER.shutdown()


def main():
    parser = argparse.ArgumentParser(description='---- Auction ----')
    parser.add_argument('auction_doc_id', type=str, help='auction_doc_id')
    parser.add_argument('port', type=int, help='Port')
    args = parser.parse_args()
    auction_run(args.auction_doc_id, args.port)


##############################################################
if __name__ == "__main__":
    main()
