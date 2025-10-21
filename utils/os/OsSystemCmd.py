import os
import subprocess
import platform
from logpkg.log_kcld import LogKCld,log_to_file
logger = LogKCld()

class OsSystemCmd():
    def __init__(self,msg_json):
        self.json_obj=msg_json['Os_System_Cmd']
        self.key=list(self.json_obj.keys())[0]
        self.data:dict = {}



    def get_cpu_info(self)->dict:
        cmd=self.json_obj[self.key]
        try:
            data_prv = subprocess.check_output(cmd).decode('utf-8').replace("\n"," ")
            logger.info(f"data is {self.data}")
        except os.error as e:
            self.data[self.key] = "Error cmd not executed"
            logger.error(f"Error cmd not executed {e}")
        except Exception as e:
            logger.error(f"Command not found {e}")
        return self.data


    def get_system_info(self)->dict:
        # Retrieve system information using 'platform' module
        self.data[self.key] = {
            'System': platform.system(),
            'Node Name': platform.node(),
            'Release': platform.release(),
            'Version': platform.version(),
            'Machine': platform.machine(),
            'Processor': platform.processor(),
            'cpu_count': os.cpu_count(),
            'Memory': os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")/(1024*1024*1024)

        }
        return self.data
    # @staticmethod
    # def list_function():
    #     list_function = {"get_system_info": OsSystemCmd.get_system_info, "get_cpu_info": OsSystemCmd.cmd_execute}
    #     print(f"list of function {list_function}")
    #     return list_function