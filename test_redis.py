import ssl, redis

CA = "/opt/dibba/config/ssl/ca.crt"
CERT = "/opt/dibba/config/ssl/client.crt"
KEY = "/opt/dibba/config/ssl/client.key"

ctx = ssl.create_default_context(cafile=CA)
ctx.load_cert_chain(certfile=CERT, keyfile=KEY)

r = redis.Redis(host="redis-server", port=6379, ssl=True, ssl_context=ctx, decode_responses=True)
print(r.ping())

