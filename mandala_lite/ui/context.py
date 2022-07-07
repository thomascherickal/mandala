from ..common_imports import *
from ..storages.main import Storage

class MODES:
    run = 'run'
    query = 'query'


class GlobalContext:
    current:Optional['Context'] = None


class Context:
    OVERRIDES = {}

    def __init__(self, storage:Storage=None, mode:str=MODES.run, 
                 lazy:bool=False):
        self.storage = storage
        self.mode = self.OVERRIDES.get('mode', mode)
        self.lazy = self.OVERRIDES.get('lazy', lazy)
        self.updates = {}
        self._updates_stack = []
    
    def _backup_state(self, keys:Iterable[str]) -> Dict[str, Any]:
        res = {}
        for k in keys:
            cur_v = self.__dict__[f'{k}']
            if k == 'storage': # gotta use a pointer
                res[k] = cur_v
            else:
                res[k] = copy.deepcopy(cur_v)
        return res

    def __enter__(self) -> 'Context':
        if GlobalContext.current is None:
            GlobalContext.current = self
        ### verify update keys
        updates = self.updates
        if not all(k in ('storage', 'mode', 'lazy') for k in updates.keys()):
            raise ValueError(updates.keys())
        ### backup state
        before_update = self._backup_state(keys=updates.keys())
        self._updates_stack.append(before_update)
        ### apply updates
        for k, v in updates.items():
            self.__dict__[f'{k}'] = v
        return self
    
    def __exit__(self, exc_type, exc_value, exc_traceback):
        if not self._updates_stack:
            raise RuntimeError('No context to exit from')
        ascent_updates = self._updates_stack.pop()
        for k, v in ascent_updates.items():
            self.__dict__[f'{k}'] = v
        if len(self._updates_stack) == 0:
            # unlink from global if done
            GlobalContext.current = None
        if exc_type:
            raise exc_type(exc_value).with_traceback(exc_traceback)
        return None
    
    def __call__(self, **updates):
        self.updates = updates
        return self


class RunContext(Context):
    OVERRIDES = {'mode': MODES.run, 'lazy': False}


class QueryContext(Context):
    OVERRIDES = {'mode': MODES.query, }


run = RunContext()
query = QueryContext()