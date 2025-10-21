import logging
import time
from logpkg.log_kcld import LogKCld, log_to_file
from kafka.producer import KafkaProducer
import json
from faker import Faker
from utils.ReadConfig import ReadConfig as rc
from utils.singleton import Singleton
fake = Faker()
logger = LogKCld()


def get_faker_data():
    return {"name": fake.name(),
            "address": fake.address(),
            "by": fake.year()}


@log_to_file(logger)
def json_serializer(data):
    return json.dumps(data).encode("utf-8")


class Producer:

    @log_to_file(logger)
    def __init__(self, bootstrapserver, topic,**kwargs):
        self.bootstrap_servers = [bootstrapserver]
        self.topic = topic
        try:
            self.producer = KafkaProducer(bootstrap_servers=self.bootstrap_servers,**kwargs)
        except Exception as err:
            logging.error(f"Exception initializing the producer"
                          f" {err}")
            raise

    @log_to_file(logger)
    def send(self, data):
        try:
            self.producer.send(self.topic,json_serializer(data))
            logger.info(f"sending data {data}")
        except Exception as err:
            logging.error(f"Exception in sending {data} {err}")
            raise

    @log_to_file(logger)
    def flush(self):
        try:
            self.producer.flush()
        except Exception as err:
            logging.error(f"Exception in {err}")
            raise


