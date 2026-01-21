from utils.ReadConfig import ReadConfig as rc
from logpkg.log_kcld import LogKCld

logger = LogKCld()
from utils.aws.aws_interface import AwsInterface


def main() -> None:
    read_config = rc("/")
    instance_type = "t2.micro"
    ami_id = "ami-02d3fd86e6a2f5122"
    key_name = "NEW_KCR"
    security_group_ids = ["sg-09ac434d5bead2ab1"]
    aws_config = read_config.aws_config
    print(f"print {aws_config}")
    awsiface = AwsInterface(aws_config['aws_access_key_id'], aws_config['aws_secret_access_key'], aws_config['region'])
    awsiface.create_ec2_instance(instance_type, ami_id, key_name, security_group_ids)
    print("checking the version")
    awsiface.get_ec2_info()
    #ec2_instances = awsiface.get_ec2s_information()
    #print(ec2_instances)
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
