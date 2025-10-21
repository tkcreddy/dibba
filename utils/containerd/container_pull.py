import grpc
from generated.runtime.v1 import api_pb2, api_pb2_grpc


class CRIImageClient:
    def __init__(self, socket_path="/run/containerd/containerd.sock"):
        self.channel = grpc.insecure_channel(f'unix://{socket_path}')
        self.stub = api_pb2_grpc.ImageServiceStub(self.channel)

    def image_exists(self, image_ref):
        try:
            list_response = self.stub.ListImages(api_pb2.ListImagesRequest())
            for img in list_response.images:
                if img.repo_tags and image_ref in img.repo_tags:
                    print(f"✅ Image already exists: {image_ref}")
                    return True
                if img.repo_digests and image_ref in img.repo_digests:
                    print(f"✅ Image already exists by digest: {image_ref}")
                    return True
            print(f"❌ Image not found locally: {image_ref}")
            return False
        except grpc.RpcError as e:
            print(f"⚠️ Error checking image existence: {e.code().name} - {e.details()}")
            return False

    def pull_image(self, image_ref):
        if self.image_exists(image_ref):
            return image_ref

        print(f"📦 Pulling image: {image_ref}")
        request = api_pb2.PullImageRequest(
            image=api_pb2.ImageSpec(image=image_ref)
        )
        try:
            response = self.stub.PullImage(request)
            print(f"✅ Pulled image: {response.image_ref}")
            return response.image_ref
        except grpc.RpcError as e:
            print(f"❌ Pull failed: {e.code().name} - {e.details()}")
            return None


if __name__ == "__main__":
    image = "docker.io/library/alpine:latest"
    client = CRIImageClient()
    pulled = client.pull_image(image)

    if pulled:
        print(f"\n🔍 Final Image Ref: {pulled}")