import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# ... keep your existing imports + CalicoEtcdClient ...

import etcd3


class CalicoEtcdError(Exception):
    pass


@dataclass
class EtcdAuthTLS:
    ca_cert: Optional[str] = None
    cert_cert: Optional[str] = None
    cert_key: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None


class CalicoEtcdClient:
    """
    CRUD for Calico v3 resources stored in etcd v3.

    Key format (etcd datastore mode):
      /<prefix>/resources/v3/projectcalico.org/<kind>/<name>

    Examples:
      /calico/resources/v3/projectcalico.org/ippools/default-ipv4-ippool
      /calico/resources/v3/projectcalico.org/nodes/ip-10-0-0-12
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 2379,
        prefix: str = "/calico",
        tls: Optional[EtcdAuthTLS] = None,
        timeout: int = 5,
    ):
        self.prefix = prefix.rstrip("/")
        self._client = etcd3.client(
            host=host,
            port=port,
            ca_cert=tls.ca_cert if tls else None,
            cert_cert=tls.cert_cert if tls else None,
            cert_key=tls.cert_key if tls else None,
            user=tls.user if tls else None,
            password=tls.password if tls else None,
            timeout=timeout,
        )

    # -------------------------
    # Key helpers
    # -------------------------
    def key(self, kind: str, name: str, group: str = "projectcalico.org") -> str:
        kind = kind.strip().lower()
        name = name.strip()
        if not name:
            raise ValueError("name must be non-empty")
        return f"{self.prefix}/resources/v3/{group}/{kind}/{name}"

    def kind_prefix(self, kind: str, group: str = "projectcalico.org") -> str:
        kind = kind.strip().lower()
        return f"{self.prefix}/resources/v3/{group}/{kind}/"

    # -------------------------
    # Basic ops
    # -------------------------
    def get(self, kind: str, name: str) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
        """
        Returns (resource_dict_or_None, mod_revision_or_None)
        """
        k = self.key(kind, name)
        val, meta = self._client.get(k)
        if val is None:
            return None, None
        try:
            return json.loads(val.decode("utf-8")), meta.mod_revision
        except Exception as e:
            raise CalicoEtcdError(f"Failed to decode JSON for key={k}: {e}")

    def list_kind(self, kind: str) -> List[Dict[str, Any]]:
        """
        List all resources of a kind.
        """
        pfx = self.kind_prefix(kind)
        out: List[Dict[str, Any]] = []
        for value, meta in self._client.get_prefix(pfx):
            try:
                out.append(json.loads(value.decode("utf-8")))
            except Exception:
                # skip malformed entries rather than failing the whole list
                continue
        return out

    def delete(self, kind: str, name: str) -> bool:
        """
        Delete resource. Returns True if key was deleted.
        """
        k = self.key(kind, name)
        deleted = self._client.delete(k)
        return bool(deleted)

    # -------------------------
    # Safe create/update using transactions
    # -------------------------
    def create(self, resource: Dict[str, Any], kind: str, name: str) -> None:
        """
        Create only if key does not exist.
        """
        k = self.key(kind, name)
        body = json.dumps(resource, separators=(",", ":"), sort_keys=True).encode("utf-8")

        # compare: version(key) == 0  (key must not exist)
        ok, _ = self._client.transaction(
            compare=[self._client.transactions.version(k) == 0],
            success=[self._client.transactions.put(k, body)],
            failure=[],
        )
        if not ok:
            raise CalicoEtcdError(f"Create failed: {kind}/{name} already exists")

    def update(self, resource: Dict[str, Any], kind: str, name: str, expect_mod_revision: Optional[int] = None) -> int:
        """
        Update resource. If expect_mod_revision is provided, update only if key is unchanged.
        Returns new mod_revision.
        """
        k = self.key(kind, name)
        body = json.dumps(resource, separators=(",", ":"), sort_keys=True).encode("utf-8")

        if expect_mod_revision is None:
            # blind update requires key exists
            ok, _ = self._client.transaction(
                compare=[self._client.transactions.version(k) > 0],
                success=[self._client.transactions.put(k, body)],
                failure=[],
            )
            if not ok:
                raise CalicoEtcdError(f"Update failed: {kind}/{name} does not exist")
        else:
            ok, _ = self._client.transaction(
                compare=[self._client.transactions.mod_revision(k) == expect_mod_revision],
                success=[self._client.transactions.put(k, body)],
                failure=[],
            )
            if not ok:
                raise CalicoEtcdError(
                    f"Update conflict: {kind}/{name} mod_revision changed (expected {expect_mod_revision})"
                )

        # fetch new mod_revision
        _, meta = self._client.get(k)
        if not meta:
            raise CalicoEtcdError(f"Update succeeded but key disappeared: {kind}/{name}")
        return meta.mod_revision

    def upsert(self, resource: Dict[str, Any], kind: str, name: str) -> int:
        """
        Create if missing, else update.
        Returns mod_revision.
        """
        existing, rev = self.get(kind, name)
        if existing is None:
            self.create(resource, kind, name)
            _, new_rev = self.get(kind, name)
            return int(new_rev or 0)
        else:
            return int(self.update(resource, kind, name, expect_mod_revision=rev))


# -------------------------
# Example resource builders
# -------------------------
def make_ippool(
    name: str,
    cidr: str,
    nat_outgoing: bool = True,
    ipip_mode: str = "Never",   # "Always" | "CrossSubnet" | "Never"
    vxlan_mode: str = "Never",  # "Always" | "CrossSubnet" | "Never"
    disabled: bool = False,
) -> Dict[str, Any]:
    return {
        "apiVersion": "projectcalico.org/v3",
        "kind": "IPPool",
        "metadata": {"name": name},
        "spec": {
            "cidr": cidr,
            "natOutgoing": nat_outgoing,
            "ipipMode": ipip_mode,
            "vxlanMode": vxlan_mode,
            "disabled": disabled,
        },
    }


def make_node(name: str, labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {
        "apiVersion": "projectcalico.org/v3",
        "kind": "Node",
        "metadata": {"name": name, "labels": labels or {}},
        "spec": {},
    }


def _deep_get(d: Dict[str, Any], path: List[str], default=None):
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


class CalicoEtcdClient(CalicoEtcdClient):  # type: ignore[misc]
    """
    Extend CalicoEtcdClient with Node-focused helpers.
    """

    def list_nodes(self) -> List[Dict[str, Any]]:
        # Calico stores as kind "nodes" in key path
        return self.list_kind("nodes")

    def list_nodes_compact(self) -> List[Dict[str, Optional[str]]]:
        nodes = self.list_nodes()
        out: List[Dict[str, Optional[str]]] = []
        for n in nodes:
            name = _deep_get(n, ["metadata", "name"])
            ipv4 = _deep_get(n, ["spec", "bgp", "ipv4Address"])
            vxlan = _deep_get(n, ["spec", "ipv4VXLANTunnelAddr"])
            if name:
                out.append(
                    {
                        "name": str(name),
                        "ipv4Address": str(ipv4) if ipv4 is not None else None,
                        "ipv4VXLANTunnelAddr": str(vxlan) if vxlan is not None else None,
                    }
                )
        # stable sort by name like calicoctl usually feels
        out.sort(key=lambda x: x["name"] or "")
        return out

    def print_nodes_egrep_style(self) -> None:
        """
        Mimic:
          calicoctl get node -o yaml | egrep -n 'name:|ipv4Address|ipv4VXLANTunnelAddr'
        Not exact YAML line numbers (those depend on YAML formatting),
        but prints similar 'n: field: value' lines per node.
        """
        rows = self.list_nodes_compact()
        line = 1
        for r in rows:
            print(f"{line}:    name: {r['name']}")
            line += 1
            if r.get("ipv4Address") is not None:
                print(f"{line}:      ipv4Address: {r['ipv4Address']}")
            else:
                print(f"{line}:      ipv4Address: <missing>")
            line += 1
            if r.get("ipv4VXLANTunnelAddr") is not None:
                print(f"{line}:    ipv4VXLANTunnelAddr: {r['ipv4VXLANTunnelAddr']}")
            else:
                print(f"{line}:    ipv4VXLANTunnelAddr: <missing>")
            line += 1

    def delete_node(self, node_name: str) -> bool:
        """
        Equivalent of:
          calicoctl delete node <node_name>
        """
        return self.delete("nodes", node_name)
