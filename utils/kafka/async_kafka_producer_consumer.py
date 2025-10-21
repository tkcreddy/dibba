import asyncio
import json
import time
from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
from utils.ReadConfig import ReadConfig as rc
#import utils.ReadConfig as rc

class AsyncKafkaProducerWithState:
    def __init__(self, bootstrap_servers, topic):
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        self.producer = AIOKafkaProducer(bootstrap_servers=bootstrap_servers)
        self.state = {}

    async def produce_message(self, message):
        await self.producer.start()
        try:
        # Produce a message to the specified topic
            await self.producer.send_and_wait(self.topic, message.encode('utf-8'))

        # Update state (for example, track the last produced message offset)
        #     metadata = await self.producer.get_metadata(self.topic)
        #     partitions = metadata.topics[self.topic].partitions
        #     for partition in partitions:
        #         self.state[partition.id] = partition.leader.highwater
        finally:
            await self.producer.stop()

class AsyncKafkaConsumer:
    def __init__(self, bootstrap_servers, group_id, topic):
        self.bootstrap_servers = bootstrap_servers
        self.group_id = group_id
        self.topic = topic
        self.consumer = AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=bootstrap_servers,
            group_id=group_id,
            auto_offset_reset='earliest',  # You can change this to 'latest' if you want to start from the latest offset
            enable_auto_commit=True,
        )
        self.state = {}

    async def consume_messages(self):
        await self.consumer.start()

        # Consume messages
        try:
            async for msg in self.consumer:
                # Process the received message
                #print('Received message: {}'.format(msg.value.decode('utf-8')))
                print(msg.value.decode('utf-8'))

                # Update state (for example, track the last consumed message offset)
                self.state[msg.partition] = msg.offset
        finally:
            await self.consumer.stop()

def load_kafka_config(file_path):
    with open(file_path, 'r') as file:
        config = json.load(file)
    return config

# Example usage
async def main():
    read_config = rc('/Users/krishnareddy/PycharmProjects/kobraCld/config/config.json')
    kafka_config = read_config.kafka_config
    print(kafka_config['bootstrap_servers'])
    #producer = AsyncKafkaProducerWithState(kafka_config['bootstrap_servers'], kafka_config['topic'])
    consumer = AsyncKafkaConsumer(kafka_config['bootstrap_servers'], kafka_config['group_id'], kafka_config['topic'])
    #msg_id=0
    await consumer.consume_messages()
    #while True:

        #await producer.produce_message('Hello, Kafka!'+ str(msg_id))

    # await asyncio.gather(
    #     producer.produce_message('Hello, Kafka!'),
    #     consumer.consume_messages(),
    # )
        #await consumer.consume_messages()
        #msg_id = msg_id + 1
        #print("Producer state:", producer.state)
        #print("Consumer state:", consumer.state)
        #time.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())
