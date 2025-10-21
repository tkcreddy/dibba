import os

from logpkg.log_kcld import LogKCld,log_to_file
import platform
import subprocess
import socket
import psutil
logger = LogKCld()


@log_to_file(logger)
def get_disk_space():
    # Get disk space information using 'df' command and store it in a variable
    disk_space_info = subprocess.run(['df', '-h'], capture_output=True, text=True)
    return disk_space_info.stdout


@log_to_file(logger)
def get_system_info() -> dict:
    return {
        'System': platform.system(),
        'Node Name': platform.node(),
        'Release': platform.release(),
        'Version': platform.version(),
        'Machine': platform.machine(),
        'Processor': platform.processor(),
        'cpu_count': os.cpu_count(),
        'Memory': os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 * 1024 * 1024),
        'Logical_cpu_count': psutil.cpu_count(logical=True),
        'Physical_cpu_count': psutil.cpu_count(logical=False),
        'Cpu_Frequency': psutil.cpu_freq()
    }

@log_to_file(logger)
def get_system_usage():
    return { 'Cpu_usage': psutil.cpu_percent( percpu=True),
             'Virtual Memory': psutil.virtual_memory(),
            'Swap Memory': psutil.swap_memory()
             }
@log_to_file(logger)
def command_execute(command):
    return os.system(command)


def host_name():
    host=socket.gethostname()
    return  host

def host_ip():
    ip=socket.gethostbyname(socket.gethostname())
    return ip

def host_string():
    host_data= ''.join([host_name(), host_ip()])
    return host_data


class OsMetricsCmd:

    def __init__(self,msg_json):
        self.json_obj=msg_json['Os_Metrics_Cmd']
        self.key=list(self.json_obj.keys())[0]
        self.data:dict = {}

    def cmd_execute(self)->dict:
        cmd=self.json_obj[self.key]

        self.data[self.key]=os.system(cmd)
        if self.data[self.key] == 0:
            self.data[self.key]={"Error cmd not executed"}
        logger.info(f"data is {self.data}")
        return self.data
