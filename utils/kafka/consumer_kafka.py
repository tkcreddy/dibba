import logging
import time
from logpkg.log_kcld import LogKCld, log_to_file
from kafka.consumer import KafkaConsumer
import json
from utils.ReadConfig import ReadConfig as rc

logger = LogKCld()
import multiprocessing

stop_event = multiprocessing.Event()


@log_to_file(logger)
def json_serializer(data):
    return json.dumps(data).encode("utf-8")


class Consumer:
    @log_to_file(logger)
    def __init__(self, bootstrapserver, topic, group_id, **kwargs):
        self.bootstrap_servers = [bootstrapserver]
        self.topic = topic
        self.group_id = group_id
        try:
            self.consumer = KafkaConsumer(self.topic, bootstrap_servers=self.bootstrap_servers, group_id=self.group_id,
                                          **kwargs)
            # self.consumer = KafkaConsumer(self.topic,bootstrap_servers=self.bootstrap_servers,group_id=self.group_id,security_protocol=self.security_protocol,ssl_cafile=self.ssl_cafile,ssl_certfile=self.ssl_certfile,ssl_keyfile=self.ssl_keyfile,ssl_password="welcome123")
        except Exception as err:
            logging.error(f"Exception in {err}")
            raise

    def consume_messages(self) -> str:
        self.consumer.subscribe()
        msg_json = ''
        # Consume messages
        try:
            for msg in self.consumer:
                # Process the received message
                # print('Received message: {}'.format(msg.value.decode('utf-8')))
                # print(msg.value.decode('utf-8'))
                msg_json = msg.value.decode('utf-8')
                print(msg_json)

                # Update state (for example, track the last consumed message offset)
                self.state[msg.partition] = msg.offset
        finally:
            self.consumer.stop()
        return msg_json

# def main():
#     read_config = rc('/Users/krishnareddy/PycharmProjects/kobraCld/config/config.json')
#     kafka_config = read_config.kakfa_config
#     print(kafka_config['bootstrap_servers'])
#     consumer = Consumer(kafka_config['bootstrap_servers'], kafka_config['topic'],kafka_config['group_id'])
#     consumer.consumer.subscribe(kafka_config['topic'])
#     #consumer.subscribe(['coba'])
#     while not stop_event.is_set():
#         for msg in consumer.consumer:
#             print(f'data is {msg.value.decode('utf-8')}')
#             if stop_event.is_set():
#                 break
#     consumer.consumer.close()
#
# if __name__ == "__main__":
#     main()
