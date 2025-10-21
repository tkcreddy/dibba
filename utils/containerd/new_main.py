from containerd_cri_interface import CombinedContainerdInterface
import subprocess
import os

def main():
    client = CombinedContainerdInterface()

    # Pull image
    image_name = "docker.io/library/nginx:latest"
    client.ensure_image(image_name)

    # Mount paths
    shared_volume_host_path = "/tmp/shared"
    secrets_host_path = "/tmp/secrets"
    os.makedirs(shared_volume_host_path, exist_ok=True)
    os.makedirs(secrets_host_path, exist_ok=True)
    with open(os.path.join(secrets_host_path, "my-secret.txt"), "w") as f:
        f.write("supersecretvalue")

    pod_id = client.create_pod_sandbox(name="demo-pod", uid="demo-uid", namespace="default")

    try:
        container_id = "nginx-container"
        client.create_container(
            container_id=container_id,
            image=image_name,
            extra_mounts=[
                (shared_volume_host_path, "/mnt/shared"),
                (secrets_host_path, "/mnt/secrets")
            ]
        )

        pid = client.get_container_pid(container_id)
        print("Container PID:", pid)

        result = subprocess.run(["nsenter", f"--net=/proc/{pid}/ns/net", "ip", "a"], capture_output=True, text=True)
        print("Pod IP Info:\n", result.stdout)

        print("Streaming container logs:")
        subprocess.run(["ctr", "tasks", "logs", container_id])

    finally:
        client.stop_and_remove_pod(pod_id)
        print("Cleanup complete.")

if __name__ == "__main__":
    main()
