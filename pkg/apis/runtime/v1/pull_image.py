import grpc
from pkg.apis.runtime.v1 import api_pb2, api_pb2_grpc

def pull_image(image_name):
    channel = grpc.insecure_channel("unix:///run/containerd/containerd.sock")
    stub = api_pb2_grpc.RuntimeServiceStub(channel)

    request = api_pb2.PullImageRequest(
        image=api_pb2.ImageSpec(image=image_name)
    )

    try:
        response = stub.PullImage(request)
        print("Image pulled successfully. Ref:", response.image_ref)
    except grpc.RpcError as e:
        print("Failed to pull image:", e.details(), e.code())

if __name__ == "__main__":
    pull_image("docker.io/library/nginx:latest")