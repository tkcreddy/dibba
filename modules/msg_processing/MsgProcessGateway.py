import multiprocessing
from logpkg.log_kcld import LogKCld,log_to_file
import subprocess
from utils.osutils.OsSystemCmd import OsSystemCmd
from utils.osutils.OsMetricsCmd import OsMetricsCmd
from utils.osutils.OsCustomCmd import OsCustomCmd
import json
import jsonpickle
import os
import asyncio
logger=LogKCld()

class MsgProcess():
    def __init__(self,msg:json)-> None:
        self.msg=msg
    @log_to_file(logger)
    def msg_process(self)->json:
        msg_json=json.loads(self.msg)
        #msg_json=self.msg
        print(f"this is the message from async {msg_json}")
        try:
            result = {}
            if msg_json:
                first_key = list(msg_json.keys())[0]
                print(f"key is {first_key}")

                if first_key == "Os_System_Cmd":
                    print(f"inside the if for validation {msg_json}")
                    print(f"key inside the if {list(msg_json['Os_System_Cmd'].keys())[0]}")
                    cmd_key=list(msg_json['Os_System_Cmd'].keys())[0]
                    os_system_cmd=OsSystemCmd(msg_json)
                    if hasattr(os_system_cmd, cmd_key):
                        method_to_call = getattr(os_system_cmd, cmd_key)
                        result = method_to_call()
                        print(result)
                    else:
                        print(f"Method '{cmd_key}' not found.")
                    #list_function=os_system_cmd.list_function()
                    #result=list_function[cmd_key]()

                elif first_key == "OS_Metrics_Cmd":
                    OsMetricsCmd(msg_json[first_key])
                elif first_key == "OS_Custom_cmd":
                    OsCustomCmd(msg_json[first_key])

                else:
                    print(f"passing in {__name__}")
                    pass
            else:
                print("Error: Empty JSON data")

            # result['hostname'] = os.uname()
            # # Execute the OS command
            # result['cpu_count'] = os.cpu_count()
            # result['Total_memory'] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")/(1024*1024*1024)
            # result = subprocess.run(command, shell=True, check=True, capture_output=True)
            # print('Command executed successfully. Output:', result.stdout.decode('utf-8'))
        except subprocess.CalledProcessError as e:
            print('Error executing command:', e)
        except Exception as e:
            print('Error Executing OS cmd:', e)
        print(f"Command output is {result}")
        return result