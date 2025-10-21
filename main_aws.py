from utils.ReadConfig import ReadConfig as rc
from logpkg.log_kcld import LogKCld
from utils.celery.celery_config import celery_app
from utils.extensions.utilities_extention import UtilitiesExtension
from kombu import Queue,Exchange
from  utils.redis.redis_interface import RedisInterface
import time
logger = LogKCld()
from utils.celery.tasks.aws_tasks import get_ec2_instances,create_worker_nodes,terminate_worker_node

def main():
    read_config = rc()
    # instance_type = "t2.micro"
    # ami_id = "ami-02d3fd86e6a2f5122"
    # key_name = "NEW_KCR"
    # security_group_ids = ["sg-09ac434d5bead2ab1"]
    key_read=read_config.encryption_config
    #secure_exchange = Exchange('secure_exchange', type='direct')
    aws_config = read_config.aws_config
    ue=UtilitiesExtension(key_read['key'])
    redis_db_config=read_config.redis_db_config
    rd=RedisInterface(redis_db_config['redis_host'],redis_db_config['redis_port'],redis_db_config['redis_db'])

    queue_info={'exchange': Exchange('secure_exchange', type='direct'), 'queue': ue.encode_hostname_with_key('aws_interface'), 'routing_key': ue.encode_hostname_with_key('aws_interface'), 'delivery_mode': 2}
    instances = {}
    result=get_ec2_instances.apply_async(
         args=(aws_config['aws_access_key_id'], aws_config['aws_secret_access_key'], aws_config['region']), **queue_info)
    #
    print("Task sent. Waiting for result...")
    print(result.get(timeout=30))
    namespace="testCluster"
    min:int=2
    max:int=2

    min_max_tags={
        'MinCount':min,
        'MaxCount':max
    }

    #### Create instances
    # result = create_worker_nodes.apply_async(args=(aws_config['aws_access_key_id'], aws_config['aws_secret_access_key'], aws_config['region'],instance_type,ami_id,key_name,security_group_ids,namespace),
    #                                          kwargs=min_max_tags,**queue_info)
    # print("Task sent. Waiting for result...")
    # response= result.get(timeout=30)
    # instances={}
    # print(min_max_tags['MaxCount'])
    # #print(response)
    # for i in range(int(min_max_tags['MaxCount'])):
    #     instances[response['Instances'][i]['PrivateDnsName']]={'IpAddress':response['Instances'][i]['PrivateIpAddress'],'InstanceId':response['Instances'][i]['InstanceId'],'NameSpace':namespace,'InstanceType':response['Instances'][i]['InstanceType']}
    # print(instances)
    # for k,v in instances.items():
    #     rd.save_node(k,v)


    ### Terminate nodes
    # instances_to_terminate=rd.get_instance_ids_namespace(namespace)
    # print(instances_to_terminate)
    # result = terminate_worker_node.apply_async(args=(aws_config['aws_access_key_id'], aws_config['aws_secret_access_key'], aws_config['region'],instances_to_terminate),**queue_info)
    # response = result.get(timeout=30)
    # rd.delete_instance_ids(instances_to_terminate)
    # print(response)





    #awsiface = AwsInterface(aws_config['aws_access_key_id'], aws_config['aws_secret_access_key'], aws_config['region'])
    # awsiface.create_ec2_instance(instance_type, ami_id, key_name, security_group_ids)
    # print("checking the version")
    # awsiface.get_ec2_info()
    #ec2_instances = awsiface.get_ec2s_information()
    #print(aws_iface_data)
    # instance_ids_to_terminate = ['i-0601632618a211983', 'i-04d7bdc645e572aec']  # Replace with your instance IDs
    # response=awsiface.terminate_ec2_instances(instance_ids_to_terminate)
    # Terminate instances
    # response =  aws terminate_ec2_instances(instance_ids_to_terminate)

    # Print the response
    # print(response)


if __name__ == '__main__':
    instance_type = "t2.micro"
    ami_id = "ami-02d3fd86e6a2f5122"
    key_name = "NEW_KCR"
    security_group_ids = ["sg-09ac434d5bead2ab1"]
    main()
