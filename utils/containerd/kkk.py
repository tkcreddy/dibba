    def image_needs_unpacking(self, image_name: str) -> bool:
        print("🔍 Checking if image is already unpacked (via snapshot list)...")
        try:
            request = snapshots_pb2.ListSnapshotsRequest(snapshotter=self.default_snapshotter)
            # The response is a stream, so we need to iterate through it
            response_stream = self.snapshots.List(request, metadata=self.metadata)

            image_key = image_name.replace("/", "-").replace(":", "-")
            for response in response_stream:
                # Check if this response contains snapshots (protobuf field name might vary)
                if hasattr(response, 'snapshots'):
                    for snapshot in response.snapshots:
                        if image_key in snapshot.name:
                            print(f"✅ Found snapshot for image: {snapshot.name}")
                            return False

            print("❗ No matching snapshot found — likely needs unpacking.")
            return True
        except grpc.RpcError as e:
            print(f"⚠️ Failed to list snapshots: {e}")
            return True

    def unpack_image(self, image_name: str):
        try:
            print(f"📦 Unpacking image: {image_name}")
            unpack_request = images_pb2.UnpackImageRequest(name=image_name)  # Note the 'Image' in the middle
            self.images.UnpackImage(unpack_request, metadata=self.metadata)  # Method might be UnpackImage
            print("✅ Image unpacked successfully.")
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.ALREADY_EXISTS:
                print("ℹ️  Image already unpacked.")
            else:
                print(f"❌ Failed to unpack image: {e.details()} (code: {e.code()})")