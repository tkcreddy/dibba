import hmac
import hashlib
import socket
from logpkg.log_kcld import LogKCld

logger = LogKCld()
from utils.aws.aws_interface import

class HostnameBasedQueue:
    def __init__(self, key, hash_algorithm='sha256'):
        """
        Initializes the HostnameBasedQueue with a secret key and hash algorithm.
        :param key: Secret key for encoding the hostname.
        :param hash_algorithm: Hash algorithm to use (default: 'sha256').
        """
        self.key = key
        self.hash_algorithm = hash_algorithm

    def encode_hostname(self):
        """
        Encodes the hostname using the provided key and hash algorithm.
        :return: Encoded hash of the hostname.
        """
        hostname = socket.gethostname()
        hmac_object = hmac.new(self.key.encode(), hostname.encode(), getattr(hashlib, self.hash_algorithm))
        return hmac_object.hexdigest()

    def get_queue_name(self):
        """
        Generates a queue name based on the encoded hostname.
        :return: Queue name.
        """
        encoded_hostname = self.encode_hostname()
        return f"queue_{encoded_hostname[:8]}"  # Use first 8 characters of the hash

class TaskSubmitter:
    def __init__(self, key):
        self.hostname_queue = HostnameBasedQueue(key)
        self.queue_name = self.hostname_queue.get_queue_name()

    def submit_task(self, instance_type, key_name, security_group, ami_id, region):
        """
        Submits the AWS EC2 creation task to the dynamically generated queue.
        :param instance_type: Type of EC2 instance.
        :param key_name: Key pair name.
        :param security_group: Security group.
        :param ami_id: Amazon Machine Image ID.
        :param region: AWS region.
        """
        task = AWSTasks()
        result = task.create_instance.apply_async(
            args=(instance_type, key_name, security_group, ami_id, region),
            queue=self.queue_name
        )
        print(f"Task sent to queue: {self.queue_name}")
        return result.get(timeout=60)

# Example usage
if __name__ == "__main__":
    key = "56ixMxYFTmxLfIh6q2dcxYph8kQF1sK8eghI3NHszsHOhh2oK7"
    task_submitter = TaskSubmitter(key)

    # Task parameters
    instance_type = "t2.micro"
    key_name = "my-key-pair"
    security_group = "default"
    ami_id = "ami-12345678"
    region = "us-east-1"

    try:
        result = task_submitter.submit_task(instance_type, key_name, security_group, ami_id, region)
        print(f"Task Result: {result}")
    except Exception as e:
        print(f"Error: {e}")
