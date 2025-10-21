import ipaddress
class IpAddress:

    def __init__(self, cidr_block, allocated_ips):
        self.cidr_block = cidr_block
        self.allocated_ips = allocated_ips
        self.free_ips = []

    def get_free_ips(self, count):
        """
        Finds free IP addresses in the given CIDR block.

        :param cidr_block: CIDR block as a string (e.g., '192.168.1.0/24').
        :param allocated_ips: List of currently allocated IPs as strings.
        :return: List of free IPs as strings.
        """
        # Create an IPv4 network object
        network = ipaddress.ip_network(self.cidr_block)

        # Convert allocated IPs to IPv4Address objects
        allocated = {ipaddress.ip_address(ip) for ip in self.allocated_ips}

        # Find all free IPs in the network
        list_free_ips = [str(ip) for ip in network.hosts() if ip not in allocated]
        if count is None:
            free_ips = list_free_ips
        else:
            free_ips = list_free_ips[:count]

        return free_ips


def main():
    # Example CIDR block and allocated IPs
    cidr_block = "192.168.1.0/24"
    allocated_ips = ["192.168.1.1", "192.168.1.2", "192.168.1.10"]

    # Get free IPs
    find_ip = IpAddress(cidr_block, allocated_ips)
    free_ips = find_ip.get_free_ips(5)

    # Print results
    print(f"Free IPs in {cidr_block}:")
    for ip in free_ips:
        print(ip)

if __name__ == "__main__":
    main()
