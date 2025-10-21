#!/usr/bin/env python3
"""
containerd_image_client.py

A minimal, no-shell-commands client to:
  1) Check if an image exists in containerd (native Images gRPC)
  2) Pull the image if missing (CRI ImageService.PullImage)

Tested against containerd v1.7/v2.0 on the default socket.

Requires:
  - grpcio
  - Your generated stubs for:
      * containerd services.images.v1
      * CRI runtime v1
"""

import os
import grpc
from typing import Optional, Tuple

# --- Adjust these imports to match your codegen paths ---
from generated.api.services.images.v1 import images_pb2, images_pb2_grpc
from pkg.apis.runtime.v1 import api_pb2 as cri_pb2
from pkg.apis.runtime.v1 import api_pb2_grpc as cri_grpc

CONTAINERD_SOCK = os.environ.get("CONTAINERD_SOCKET", "/run/containerd/containerd.sock")
# containerd uses namespaces; CRI typically lives in "k8s.io"
CONTAINERD_NAMESPACE = os.environ.get("CONTAINERD_NAMESPACE", "k8s.io")

def _channel() -> grpc.Channel:
    # grpc over unix domain socket
    return grpc.insecure_channel(f"unix://{CONTAINERD_SOCK}")

def _images_stub(ch: grpc.Channel) -> images_pb2_grpc.ImagesStub:
    return images_pb2_grpc.ImagesStub(ch)

def _cri_image_stub(ch: grpc.Channel) -> cri_grpc.ImageServiceStub:
    return cri_grpc.ImageServiceStub(ch)

def _md() -> Tuple[Tuple[str, str], ...]:
    # containerd expects this metadata header to scope requests to a namespace
    return (("containerd-namespace", CONTAINERD_NAMESPACE),)

class ContainerdImageClient:
    def __init__(self, namespace: Optional[str] = None, socket: Optional[str] = None) -> None:
        self.socket = socket or CONTAINERD_SOCK
        self.namespace = namespace or CONTAINERD_NAMESPACE
        self._ch = grpc.insecure_channel(f"unix://{self.socket}")
        self._images = _images_stub(self._ch)
        self._cri_images = _cri_image_stub(self._ch)
        self._md = (("containerd-namespace", self.namespace),)

    # ---------- Existence checks via native Images API ----------

    def image_get(self, name_or_ref: str):
        """
        Try a direct Get by name. If NOT_FOUND, return None.
        """
        try:
            return self._images.Get(
                images_pb2.GetImageRequest(name=name_or_ref),
                metadata=self._md
            ).image
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                return None
            raise

    def image_exists(self, ref: str) -> Tuple[bool, Optional[str]]:
        """
        Returns (exists, resolved_id_or_digest)
        - First try Get(ref)
        - If not found, List and match by name (handles refs with/without registry prefixes)
        """
        img = self.image_get(ref)
        if img:
            # Prefer digest if available
            digest = img.target.digest if img.target and img.target.digest else None
            name = img.name or ref
            return True, (digest or name)

        # Fallback: search in List for a matching name
        listed = self._images.List(images_pb2.ListImagesRequest(), metadata=self._md)
        for it in listed.images:
            if it.name == ref:
                digest = it.target.digest if it.target and it.target.digest else None
                return True, (digest or it.name)
        return False, None

    # ---------- Pull via CRI ImageService ----------

    def pull_image(
        self,
        ref: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        auth_token: Optional[str] = None,
    ) -> str:
        """
        Pull an image using CRI PullImage (no shell).
        Supports either basic auth (username/password) or registry token.
        Returns the image_ref (often a digest) from CRI.
        """
        auth = None
        if username or password or auth_token:
            auth = cri_pb2.AuthConfig()
            if username:
                auth.username = username
            if password:
                auth.password = password
            if auth_token:
                auth.auth = auth_token  # bearer/token, if your registry uses it

        req = cri_pb2.PullImageRequest(image=cri_pb2.ImageSpec(image=ref), auth=auth)
        resp = self._cri_images.PullImage(req)  # CRI doesn't use namespace header
        return resp.image_ref or ref

    # ---------- Ensure helper ----------

    def ensure_image(
        self,
        ref: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        auth_token: Optional[str] = None,
    ) -> str:
        """
        Ensure 'ref' exists in containerd.
        If missing, pull via CRI and re-check via Images API.
        Returns resolved digest or image name.
        """
        exists, resolved = self.image_exists(ref)
        if exists:
            print(f"✅ Image present: {resolved}")
            return resolved or ref

        print(f"📦 Missing. Pulling via CRI: {ref}")
        pulled_ref = self.pull_image(ref, username=username, password=password, auth_token=auth_token)
        # Re-check in images store after pull
        exists, resolved = self.image_exists(ref)
        if not exists:
            # Some CRI implementations store under digest; try by returned ref too
            if pulled_ref and pulled_ref != ref:
                exists, resolved = self.image_exists(pulled_ref)
        if not exists:
            raise RuntimeError(f"Pulled '{ref}' but not visible in containerd Images store.")
        print(f"✅ Pulled and available: {resolved}")
        return resolved or pulled_ref or ref


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <image-ref> [username] [password]")
        print(f"Example: {sys.argv[0]} docker.io/library/nginx:latest")
        sys.exit(2)

    ref = sys.argv[1]
    user = sys.argv[2] if len(sys.argv) > 2 else None
    pwd = sys.argv[3] if len(sys.argv) > 3 else None

    client = ContainerdImageClient()
    try:
        resolved = client.ensure_image(ref, username=user, password=pwd)
        print(f"🎯 Ready: {resolved}")
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
