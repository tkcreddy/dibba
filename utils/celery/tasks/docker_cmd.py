from celery import Celery
from utils.docker.docker_interface import DockerManager
from utils.celery.celery_config import celery_app
# Initialize Celery
#celery_app = Celery('docker_tasks', broker='redis://localhost:6379/0')

# Initialize DockerManager
docker_manager = DockerManager()

@celery_app.task
def start_container_task(image_name, container_name=None, ports=None, env_vars=None, volumes=None, command=None):
    return docker_manager.start_container(image_name, container_name, ports, env_vars, volumes, command)

@celery_app.task
def stop_container_task(container_name_or_id):
    return docker_manager.stop_container(container_name_or_id)

@celery_app.task
def remove_container_task(container_name_or_id):
    return docker_manager.remove_container(container_name_or_id)

@celery_app.task
def pull_image_task(image_name):
    return docker_manager.pull_image(image_name)

@celery_app.task
def remove_image_task(image_name):
    return docker_manager.remove_image(image_name)

@celery_app.task
def create_volume_task(volume_name):
    return docker_manager.create_volume(volume_name)

@celery_app.task
def remove_volume_task(volume_name):
    return docker_manager.remove_volume(volume_name)

@celery_app.task
def create_network_task(network_name):
    return docker_manager.create_network(network_name)

@celery_app.task
def remove_network_task(network_name):
    return docker_manager.remove_network(network_name)
