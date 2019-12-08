import os
import copy
import yaml


from bobsled import storages, environments, tasks, runners, callbacks

DEFAULT_SETTINGS = {
    "environments": {
        "provider": "YamlEnvironmentProvider",
        "args": {"filename": "environments.yml"},
    },
    "tasks": {"provider": "YamlTaskProvider", "args": {"filename": "tasks.yml"}},
    "runner": {"provider": "LocalRunService", "args": {}},
    "storage": {"provider": "InMemoryStorage", "args": {}},
    "callbacks": [],
    "secret_key": None,
}


class Bobsled:
    def __init__(self):
        filename = os.environ.get("BOBSLED_SETTINGS_FILE", "bobsled.yml")
        with open(filename) as f:
            settings = copy.deepcopy(DEFAULT_SETTINGS)
            settings.update(yaml.safe_load(f))

        if settings["secret_key"] is None:
            raise ValueError("must set 'secret_key' setting")
        self.settings = settings

        EnvCls = getattr(environments, settings["environments"]["provider"])
        TaskCls = getattr(tasks, settings["tasks"]["provider"])
        RunCls = getattr(runners, settings["runner"]["provider"])
        StorageCls = getattr(storages, settings["storage"]["provider"])

        callback_classes = []
        for cb in settings["callbacks"]:
            PluginCls = getattr(callbacks, cb["plugin"])
            callback_classes.append(PluginCls(**cb["args"]))

        self.storage = StorageCls(**settings["storage"]["args"])
        self.env = EnvCls(**settings["environments"]["args"])
        self.tasks = TaskCls(storage=self.storage, **settings["tasks"]["args"])
        self.run = RunCls(
            storage=self.storage,
            environment=self.env,
            callbacks=callback_classes,
            **settings["runner"]["args"]
        )

    async def initialize(self):
        await self.storage.connect()
        await self.tasks.update_tasks()
        tasks = await bobsled.tasks.get_tasks()
        self.run.initialize(tasks)


bobsled = Bobsled()
