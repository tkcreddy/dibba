from containerd_interface import ContainerdInterface

client = ContainerdInterface()

# Create pod (with optional shared volume)
pause_id = client.create_pod("mypod", shared_volume="/tmp/shared")

# Add containers to the pod
nginx_id = client.add_container_to_pod(pause_id, "nginx", cpu=512, memory=128 * 1024 * 1024)
fluentd_id = client.add_container_to_pod(pause_id, "fluent/fluentd", cpu=256, memory=128 * 1024 * 1024)

# Get IP address of the pod
print("Pod IP:", client.get_pod_ip(nginx_id))
