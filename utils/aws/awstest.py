import boto3
import yaml


def create_ec2_instance(instance_type, ami_id, key_name, security_group_ids):
    """
    Creates an EC2 instance using credentials loaded from a YAML file.

    Args:
        yaml_file_path (str): Path to the YAML file containing AWS credentials.
        instance_type (str): EC2 instance type (e.g., "t2.micro").
        ami_id (str): Amazon Machine Image (AMI) ID.
        key_name (str): Name of the key pair to use for access.
        security_group_ids (list): List of security group IDs.
    """

    session = boto3.Session(
        aws_access_key_id="AKIAUH2O47GCTJGKEISM",
        aws_secret_access_key="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",
        region_name="us-west-1"
    )
    ec2_client = session.client('ec2')

    response = ec2_client.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        KeyName=key_name,
        SecurityGroupIds=security_group_ids,
        MinCount=1,
        MaxCount=1
    )

    instance_id = response['Instances'][0]['InstanceId']
    print(f"EC2 instance created with ID: {instance_id}")


# Example usage
def  main():
    instance_type = "t2.micro"
    ami_id = "ami-02d3fd86e6a2f5122"
    key_name = "NEW_KCR"
    security_group_ids = ["sg-09ac434d5bead2ab1"]

    create_ec2_instance(instance_type, ami_id, key_name, security_group_ids)

if __name__ == "__main__":
    #main()
    session = boto3.Session(
        aws_access_key_id="AKIAUH2O47GCTJGKEISM",
        aws_secret_access_key="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",
        region_name="us-west-1"
    )
    ec2_client = session.client('ec2')
    response = ec2_client.describe_instances()

    # Loop through the reservations and print instance details
    for reservation in response['Reservations']:
        for instance in reservation['Instances']:
            print(f"Instance ID: {instance['InstanceId']}")
            print(f"Instance Type: {instance['InstanceType']}")
            print(f"State: {instance['State']['Name']}")
            print("-" * 20)