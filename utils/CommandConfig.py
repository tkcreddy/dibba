import json
from utils.ReadConfig import ReadConfig as rc
from utils.singleton import Singleton
import os
class _CommandConfig:
    def __init__(self):
        read_confg = rc()
        self.base_dir = read_confg.set_config_dir
        self.file_path = os.path.join(self.base_dir, 'commands.json')
        # self.file_path = file_path
        self._config_data = None
        self.load_config()
    def load_config(self):
        with open(self.file_path, 'r') as file:
            self._config_data = json.load(file)
    @property
    def os_system_cmd(self):
        return self._config_data['OS_System_Cmd']
class CommandConfig(_CommandConfig, metaclass=Singleton):
    pass