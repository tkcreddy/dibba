import logging
import boto3
from logpkg.log_kcld import LogKCld, log_to_file
import json
from utils.redis.redis_interface import RedisInterface

logger = LogKCld()
rd=RedisInterface()


class AwsInterface:
    @log_to_file(logger)
    def __init__(self, aws_access_key_id: str, aws_secret_access_key: str, region_name: str):
        try:
            self.session = boto3.Session(aws_access_key_id=aws_access_key_id,
                                         aws_secret_access_key=aws_secret_access_key, region_name=region_name)
            self.ec2_client = self.session.client('ec2')
        except Exception as err:
            logging.error(f"Exception initializing AWS Interface "f" {err}")
            raise

    @log_to_file(logger)
    def create_ec2_instance(self, instance_type: str, ami_id: str, key_name: str, security_group_ids: list,subnet_id: str,
                            namespace: str | None, **kwargs) -> None:
        """
        Creates an EC2 instance using credentials loaded from a YAML file.

        Args:
            instance_type (str): EC2 instance type (e.g., "t2.micro").
            ami_id (str): Amazon Machine Image (AMI) ID.
            key_name (str): Name of the key pair to use for access.
            security_group_ids (list): List of security group IDs.
            namespace: str = Namespace for the namespace
            :param subnet_id:
        """
        kwargs.setdefault('MinCount', 1)
        kwargs.setdefault('MaxCount', 1)
        tags = [
            {"Key": "Namespace", "Value": namespace}
        ]

        response = self.ec2_client.run_instances(
            ImageId=ami_id,
            InstanceType=instance_type,
            KeyName=key_name,
            SecurityGroupIds=security_group_ids,
            SubnetId=subnet_id,
            TagSpecifications=[
                                  {
                                      "ResourceType": "instance",
                                      "Tags": tags
                                  }
                              ],
            **kwargs
        )
        #task_result = create_worker_nodes.AsyncResult(task_id).get(timeout=30)
        instances = {
            response['Instances'][i]['PrivateDnsName']: {
                'IpAddress': response['Instances'][i]['PrivateIpAddress'],
                'InstanceId': response['Instances'][i]['InstanceId'],
                'NameSpace': namespace,
                'InstanceType': response['Instances'][i]['InstanceType'],
            }
            for i in range(kwargs['MaxCount'])
        }
        # Save instances to Redis
        for k, v in instances.items():
            rd.save_node(k, v)

        # instance_id = response['Instances'][0]['InstanceId']
        # print(f"EC2 instance created with ID: {instance_id}")
        return response

    @log_to_file(logger)
    def get_ec2s_information(self) -> json:
        # print("inside the function")
        try:
            instances = []
            paginator = self.ec2_client.get_paginator('describe_instances')

            # Use paginator to iterate over all pages
            for page in paginator.paginate():
                for reservation in page.get('Reservations', []):
                    for instance in reservation.get('Instances', []):
                        name_tag = next(
                            (tag['Value'] for tag in instance.get('Tags', []) if tag['Key'] == 'Name'),
                            None
                        )
                        instance_details = {
                            "Name": name_tag,
                            "InstanceID": instance.get('InstanceId'),
                            "PrivateIpAddress": instance.get('PrivateIpAddress'),
                            "LaunchTime": instance.get('LaunchTime').strftime('%Y-%m-%d %H:%M:%S')
                        }
                        instances.append(instance_details)
        except Exception as err:
            logging.error(f"Exception getting AWS info "f" {err}")
            raise

        # Convert the list of instances to JSON format
        return json.dumps(instances, indent=4, default=str)

    @log_to_file(logger)
    def get_ec2_info(self):
        try:
            response = self.ec2_client.describe_instances()

            # Convert the response to JSON format and print
            response_json = json.dumps(response, indent=4, default=str)
            print(response_json)
        except Exception as err:
            logging.error(f"Exception getting AWS info "f" {err}")
            raise

    @log_to_file(logger)
    def terminate_ec2_instances(self, instance_ids):
        try:
            response = self.ec2_client.terminate_instances(InstanceIds=instance_ids)
            for instance in response['TerminatingInstances']:
                print(f"Instance {instance['InstanceId']} is in {instance['CurrentState']['Name']} state.")
            if response:
                rd.delete_instance_ids(instance_ids)
            return response
        except Exception as e:
            print(f"An error occurred: {e}")

    # List of instance IDs to terminate
