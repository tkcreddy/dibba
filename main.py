# This is a sample Python script.

# Press ⌃R to execute it or replace it with your code.
# Press Double ⇧ to search everywhere for classes, files, tool windows, actions, and settings.
import asyncio
import argparse
from utils.kafka.producer_kafka import Producer
from utils.ReadConfig import ReadConfig as rc
from server.nodes.SsOsSystemCmd import SsOsSystemCmd as ss
from logpkg.log_kcld import LogKCld,log_to_file
logger=LogKCld()


@log_to_file(logger)
async def main() ->None:
    parser = argparse.ArgumentParser(description='A Python CLI application')
    parser.add_argument('--configDir', type=str, help='Please specify ConfigDir')
    args = parser.parse_args()
    read_config = rc(args.configDir)
    kafka_config = read_config.kafka_config
    kafka_ssl_info = read_config.kafka_ssl
    os_system_cmd=ss()

    producer=Producer(kafka_config['bootstrap_servers'],kafka_config['topic'],**kafka_ssl_info)

    data=os_system_cmd.get_system_info()
    logger.info(f"data is {data}")
    producer.send(data)


if __name__ == '__main__':
    asyncio.run(main())
