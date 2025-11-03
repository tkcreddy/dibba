import etcd3
import json

# --- Connection config ---
etcd = etcd3.client(
    host="etcd-1",
    port=2379,
    timeout=5
)

# --- Fetch all workload endpoints (pods) ---
POD_PREFIX = "/calico/resources/v3/projectcalico.org/workloadendpoints/"

pod_ips = []

for value, meta in etcd.get_prefix(POD_PREFIX):
    data = json.loads(value)
    name = data["metadata"]["name"]
    node = data["spec"].get("node", "unknown")
    ip_networks = data["spec"].get("ipNetworks", [])
    pod_ips.append((name, node, ip_networks))

# --- Print table ---
print(f"{'POD':30} {'NODE':20} {'IP(s)'}")
print("=" * 70)
for name, node, ips in pod_ips:
    print(f"{name:30} {node:20} {', '.join(ips)}")

etcd.close()
