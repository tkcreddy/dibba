# test_redis.py
import ssl
import redis

CA = "/opt/dibba/config/ssl/ca.crt"
CERT = "/opt/dibba/config/ssl/client.crt"
KEY = "/opt/dibba/config/ssl/client.key"

r = redis.Redis(
    host="redis-server",
    port=6379,
    db=1,
    ssl=True,
    ssl_ca_certs=CA,
    ssl_certfile=CERT,
    ssl_keyfile=KEY,
    ssl_cert_reqs=ssl.CERT_REQUIRED,  # keep verification ON
    ssl_check_hostname=True,          # server cert SAN must include DNS:redis-server
    decode_responses=True,
)

print(r.ping())

