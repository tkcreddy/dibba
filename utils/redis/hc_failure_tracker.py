import redis
class HcFailureTracker:

    def __init__(self):
    # Connect to Redis
        self.redis_client = redis.StrictRedis(host='localhost', port=6379, db=1, decode_responses=True)


    def hc_failure_tracker(self,hash_name, field, status,current_time, expiration_time=None,increment_by=1,):
        """
        Increment a field in a Redis hash by a specified amount.
        Optionally set an expiration for the hash.

        :param hash_name: The name of the hash.
        :param field: The field to increment.
        :param increment_by: The amount to increment by (default is 1).
        :param expiration_time: Expiration time in seconds (optional).
        :return: The new value of the field.
        """
        # Increment the field value
        new_value = self.redis_client.hincrby(hash_name, field, increment_by)
        self.set_field_with_expiry(hash_name, field, new_value, expiration_time)


        # If an expiration time is provided, set it for the hash
        if expiration_time is not None:
            ttl = self.redis_client.ttl(field)
            if ttl == -1:  # If the hash has no expiration
                self.redis_client.expire(field, expiration_time)

        return new_value

    def set_field_with_expiry(self,hashname, field, value, expiry):
        """
        Simulate field expiration by creating a separate key for the hash field.

        Args:
            hashname (str): Name of the hash.
            field (str): Field name.
            value (str): Value to store.
            expiry (int): Expiration time in seconds.
        """
        key = f"{hashname}:{field}"
        self.redis_client.setex(key, expiry, value)
        print(f"Set {field} in {hashname} with expiry of {expiry} seconds.")


