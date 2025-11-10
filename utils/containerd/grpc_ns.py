# utils/containerd/grpc_ns.py
import grpc

class _AddNamespaceInterceptor(grpc.UnaryUnaryClientInterceptor,
                               grpc.UnaryStreamClientInterceptor):
    def __init__(self, namespace: str, extra_md=None):
        self.namespace = namespace
        self.extra_md = extra_md or []

    def _inject(self, client_call_details):
        # Append metadata
        new_md = []
        if client_call_details.metadata:
            new_md.extend(client_call_details.metadata)

        # namespace first
        new_md.append(("containerd-namespace", self.namespace))

        # append any extra metadata
        new_md.extend(self.extra_md)

        return client_call_details._replace(metadata=new_md)

    def intercept_unary_unary(self, continuation, client_call_details, request):
        return continuation(self._inject(client_call_details), request)

    def intercept_unary_stream(self, continuation, client_call_details, request):
        return continuation(self._inject(client_call_details), request)


def _with_namespace_md(details, md_to_add):
    metadata = []
    if details.metadata:
        metadata = list(details.metadata)
    metadata.extend(md_to_add)

    class _NewCCD(grpc.ClientCallDetails):
        def __init__(self, d, md):
            self.method = d.method
            self.timeout = d.timeout
            self.credentials = d.credentials
            self.wait_for_ready = getattr(d, "wait_for_ready", None)
            self.compression = getattr(d, "compression", None)
            self.metadata = md

    return _NewCCD(details, metadata)
