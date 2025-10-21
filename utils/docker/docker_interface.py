import docker


class DockerManager:
    def __init__(self):
        self.client = docker.from_env()

    # List all containers (running and stopped)
    def list_containers(self, all_containers=False):
        containers = self.client.containers.list(all=all_containers)
        return [{"id": c.short_id, "name": c.name, "status": c.status, "image": c.image.tags} for c in containers]

    # Start a container
    def start_container(self, image_name, container_name=None, ports=None, env_vars=None, volumes=None, command=None):
        try:
            container = self.client.containers.run(
                image=image_name,
                name=container_name,
                ports=ports,
                environment=env_vars,
                volumes=volumes,
                command=command,
                detach=True
            )
            return f"Container {container.short_id} started successfully!"
        except docker.errors.APIError as e:
            return f"Error starting container: {e}"

    # Stop a running container
    def stop_container(self, container_name_or_id):
        try:
            container = self.client.containers.get(container_name_or_id)
            container.stop()
            return f"Container {container_name_or_id} stopped."
        except docker.errors.NotFound:
            return f"Container {container_name_or_id} not found."

    # Remove a container
    def remove_container(self, container_name_or_id):
        try:
            container = self.client.containers.get(container_name_or_id)
            container.remove()
            return f"Container {container_name_or_id} removed."
        except docker.errors.NotFound:
            return f"Container {container_name_or_id} not found."

    # List images
    def list_images(self):
        images = self.client.images.list()
        return [image.tags for image in images if image.tags]

    # Remove an image
    def remove_image(self, image_name):
        try:
            self.client.images.remove(image_name)
            return f"Image {image_name} removed."
        except docker.errors.ImageNotFound:
            return f"Image {image_name} not found."

    # Pull an image
    def pull_image(self, image_name):
        try:
            self.client.images.pull(image_name)
            return f"Image {image_name} pulled successfully."
        except docker.errors.APIError as e:
            return f"Error pulling image: {e}"

    # Create a volume
    def create_volume(self, volume_name):
        volume = self.client.volumes.create(name=volume_name)
        return f"Volume {volume.name} created."

    # List volumes
    def list_volumes(self):
        volumes = self.client.volumes.list()
        return [volume.name for volume in volumes]

    # Remove a volume
    def remove_volume(self, volume_name):
        try:
            volume = self.client.volumes.get(volume_name)
            volume.remove()
            return f"Volume {volume_name} removed."
        except docker.errors.NotFound:
            return f"Volume {volume_name} not found."

    # Create a network
    def create_network(self, network_name):
        network = self.client.networks.create(name=network_name)
        return f"Network {network.name} created."

    # List networks
    def list_networks(self):
        networks = self.client.networks.list()
        return [network.name for network in networks]

    # Remove a network
    def remove_network(self, network_name):
        try:
            network = self.client.networks.get(network_name)
            network.remove()
            return f"Network {network_name} removed."
        except docker.errors.NotFound:
            return f"Network {network_name} not found."


if __name__ == "__main__":
    docker_manager = DockerManager()

    # Example usage
    print(docker_manager.list_containers(all_containers=True))
    print(docker_manager.pull_image("nginx"))
    print(docker_manager.start_container("nginx", container_name="my_nginx", ports={"80/tcp": 8080}))
