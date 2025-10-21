import socket
class OsUtils:
    def __init__(self):
        pass
    @property
    def hostname(self):
        self.host=socket.gethostname()
        return  self.host
    @property
    def hostip(self):
        self.ip=socket.gethostbyname(socket.gethostname())
        return self.ip
    @property
    def hoststring(self):
        self.hostdata=''.join([self.host,self.ip])
        return self.hostdata
    @property
    def host_string(self):
        self.hostdata='_'.join([self.host,self.ip])
        return self.hostdata
obj=OsUtils()
print(obj.hostname)
print(obj.hostip)
print(obj.hoststring)
print(obj.host_string)
