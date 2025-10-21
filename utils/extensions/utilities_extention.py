import socket
import hashlib
import uuid
import hmac
import base64
import time
import re
from utils.singleton import Singleton
from utils.ReadConfig import ReadConfig as rc
from logpkg.log_kcld import LogKCld, log_to_file

logger = LogKCld()


class _UtilitiesExtension:
    @log_to_file(logger)
    def __init__(self, key):
        self.key = key

    @log_to_file(logger)
    def generate_time_based_uid(self) -> str:
        # Get current timestamp
        current_time = int(time.time() * 1000000)  # Convert to milliseconds

        # Create a UUID using the timestamp
        uid = uuid.uuid5(uuid.NAMESPACE_DNS, str(current_time))

        # Encode the UID to base64
        base64_encoded = base64.urlsafe_b64encode(uid.bytes).decode('utf-8')

        # Remove padding characters
        base64_encoded = base64_encoded.rstrip("=")
        base64_encoded = re.sub("-", "_", base64_encoded)

        return base64_encoded

    @log_to_file(logger)
    def generate_uuid_with_key(self) -> str:
        # Use SHA-256 hash function
        hash_object = hashlib.sha256(self.key.encode())
        hashed_key = hash_object.digest()

        # Encode the hashed key to base64
        base64_encoded = base64.b64encode(hashed_key).decode('utf-8')

        return base64_encoded[:-2]

    @log_to_file(logger)
    def encode_phrase_with_key(self, phrase: str = None, size: int = 48, hash_algorithm='sha256', ) -> str | None:
        """
        Encodes the hostname using the provided key and a hash algorithm.

        :param phrase:
        :param hash_algorithm: Hash algorithm to use (default is 'sha256').
        :return: Encoded hash of the hostname.
        """
        # Get the hostname of the machine
        if phrase is None:
            return None
        else:
            self.phrase = phrase

        # Create a HMAC object using the key and hash algorithm
        hmac_object = hmac.new(self.key.encode(), self.phrase.encode(), getattr(hashlib, hash_algorithm))

        # Return the hexadecimal digest of the HMAC
        return hmac_object.hexdigest()[:size]

    @log_to_file(logger)
    def encode_hostname_with_key(self, hostname: str = None, size: int = 48, hash_algorithm='sha256', ) -> str:
        """
        Encodes the hostname using the provided key and a hash algorithm.

        :param hostname:
        :param hash_algorithm: Hash algorithm to use (default is 'sha256').
        :return: Encoded hash of the hostname.
        """
        # Get the hostname of the machine
        self.hostname = socket.gethostname() if hostname is None else hostname
        # Create a HMAC object using the key and hash algorithm
        hmac_object = hmac.new(self.key.encode(), self.hostname.encode(), getattr(hashlib, hash_algorithm))

        # Return the hexadecimal digest of the HMAC
        return hmac_object.hexdigest()[:size]


class UtilitiesExtension(_UtilitiesExtension, metaclass=Singleton):
    pass


def main():
    read_config = rc("/Users/krishnareddy/PycharmProjects/kobraCldSS")
    # key = "your_key_here"
    key_read = read_config.encryption_config
    key = key_read['key']
    ue = UtilitiesExtension(key)
    print(ue.encode_hostname_with_key())


if __name__ == "__main__":
    main()
