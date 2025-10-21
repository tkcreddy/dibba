import os

from logpkg.log_kcld import LogKCld, log_to_file

logger = LogKCld()


class OsCustomCmd:

    def __init__(self, msg_json) -> None:
        self.json_obj = msg_json['Os_Custom_Cmd']
        self.key = list(self.json_obj.keys())[0]
        self.data: dict = {}

    def cmd_execute(self) -> dict:
        cmd = self.json_obj[self.key]

        self.data[self.key] = os.system(cmd)
        if self.data[self.key] == 0:
            self.data[self.key] = {"Error cmd not executed"}
        logger.info(f"data is {self.data}")
        return self.data

    def command_execute(self, command)  -> int:
        count = os.cpu_count()
        self.data = os.system(command)
        return self.data
