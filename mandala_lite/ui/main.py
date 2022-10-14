from duckdb import DuckDBPyConnection as Connection
from abc import ABC, abstractmethod
from collections import defaultdict
import datetime
from pypika import Query, Table
import pyarrow.parquet as pq

from ..storages.rel_impls.utils import Transactable, transaction
from ..storages.kv import InMemoryStorage
from ..storages.rel_impls.duckdb_impl import DuckDBRelStorage
from ..storages.rels import RelAdapter, RemoteEventLogEntry, deserialize, SigAdapter
from ..storages.sigs import SigSyncer
from ..storages.remote_storage import RemoteStorage
from ..common_imports import *
from ..core.config import Config
from ..core.model import Call, FuncOp, ValueRef, unwrap, Delayed
from ..core.workflow import Workflow, CallStruct
from ..core.utils import Hashing

from ..core.weaver import ValQuery, FuncQuery
from ..core.compiler import traverse_all, Compiler

from .utils import wrap_inputs, wrap_outputs


class MODES:
    run = "run"
    query = "query"
    batch = "batch"


class GlobalContext:
    current: Optional["Context"] = None


class Context:
    OVERRIDES = {}

    def __init__(
        self, storage: "Storage" = None, mode: str = MODES.run, lazy: bool = False
    ):
        self.storage = storage
        self.mode = self.OVERRIDES.get("mode", mode)
        self.lazy = self.OVERRIDES.get("lazy", lazy)
        self.updates = {}
        self._updates_stack = []
        self._call_structs = []

    def _backup_state(self, keys: Iterable[str]) -> Dict[str, Any]:
        res = {}
        for k in keys:
            cur_v = self.__dict__[f"{k}"]
            if k == "storage":  # gotta use a pointer
                res[k] = cur_v
            else:
                res[k] = copy.deepcopy(cur_v)
        return res

    def __enter__(self) -> "Context":
        if GlobalContext.current is None:
            GlobalContext.current = self
        ### verify update keys
        updates = self.updates
        if not all(k in ("storage", "mode", "lazy") for k in updates.keys()):
            raise ValueError(updates.keys())
        ### backup state
        before_update = self._backup_state(keys=updates.keys())
        self._updates_stack.append(before_update)
        ### apply updates
        for k, v in updates.items():
            if v is not None:
                self.__dict__[f"{k}"] = v
        # Load state from remote
        if self.storage is not None:
            # self.storage.sync_with_remote()
            self.storage.sync_from_remote()
        return self

    def _undo_updates(self):
        """
        Roll back the updates from the current level
        """
        if not self._updates_stack:
            raise RuntimeError("No context to exit from")
        ascent_updates = self._updates_stack.pop()
        for k, v in ascent_updates.items():
            self.__dict__[f"{k}"] = v
        # unlink from global if done
        if len(self._updates_stack) == 0:
            GlobalContext.current = None

    def __exit__(self, exc_type, exc_value, exc_traceback):
        exc = None
        try:
            if self.mode == MODES.run:
                # commit calls from temp partition to main and tabulate them
                if Config.autocommit:
                    self.storage.commit()
                self.storage.sync_to_remote()
            elif self.mode == MODES.query:
                pass
            elif self.mode == MODES.batch:
                executor = SimpleWorkflowExecutor()
                workflow = Workflow.from_call_structs(self._call_structs)
                calls = executor.execute(workflow=workflow, storage=self.storage)
                self.storage.commit(calls=calls)
            else:
                raise ValueError(self.mode)
        except Exception as e:
            exc = e
        self._undo_updates()
        if exc is not None:
            raise exc
        if exc_type:
            raise exc_type(exc_value).with_traceback(exc_traceback)
        return None

    def __call__(self, storage: Optional["Storage"] = None, **updates):
        self.updates = {"storage": storage, **updates}
        return self

    def get_table(self, *queries: ValQuery) -> pd.DataFrame:
        # ! EXTREMELY IMPORTANT
        # We must sync any dirty cache elements to the DuckDB store before performing a query.
        # If we don't, we'll query a store that might be missing calls and objs.
        self.storage.commit()
        return self.storage.execute_query(select_queries=list(queries))


class RunContext(Context):
    OVERRIDES = {"mode": MODES.run, "lazy": False}


class QueryContext(Context):
    OVERRIDES = {
        "mode": MODES.query,
    }


class BatchContext(Context):
    OVERRIDES = {
        "mode": MODES.batch,
    }


run = RunContext()
query = QueryContext()
batch = BatchContext()


class Storage(Transactable):
    """
    Groups together all the components of the storage system.

    Responsible for things that require multiple components to work together,
    e.g.
        - committing: moving calls from the "temporary" partition to the "main"
        partition. See also `CallStorage`.
        - synchronizing: connecting an operation with the storage and performing
        any necessary updates
    """

    def __init__(
        self,
        root: Optional[Union[Path, RemoteStorage]] = None,
        timestamp: Optional[datetime.datetime] = None,
    ):
        self.root = root
        self.call_cache = InMemoryStorage()
        self.obj_cache = InMemoryStorage()
        # all objects (inputs and outputs to operations, defaults) are saved here
        # stores the memoization tables
        self.rel_storage = DuckDBRelStorage()
        # manipulates the memoization tables
        self.rel_adapter = RelAdapter(rel_storage=self.rel_storage)
        self.sig_adapter = self.rel_adapter.sig_adapter
        self.sig_syncer = SigSyncer(sig_adapter=self.sig_adapter, root=self.root)

        self.last_timestamp = (
            timestamp if timestamp is not None else datetime.datetime.fromtimestamp(0)
        )

    ############################################################################
    ### `Transactable` interface
    ############################################################################
    def _get_connection(self) -> Connection:
        return self.rel_storage._get_connection()

    def _end_transaction(self, conn: Connection):
        return self.rel_storage._end_transaction(conn=conn)

    ############################################################################
    def call_exists(self, call_uid: str) -> bool:
        return self.call_cache.exists(call_uid) or self.rel_adapter.call_exists(
            call_uid
        )

    def call_get(self, call_uid: str) -> Call:
        if self.call_cache.exists(call_uid):
            return self.call_cache.get(call_uid)
        else:
            lazy_call = self.rel_adapter.call_get_lazy(call_uid)
            # load the values of the inputs and outputs
            inputs = {k: self.obj_get(v.uid) for k, v in lazy_call.inputs.items()}
            outputs = [self.obj_get(v.uid) for v in lazy_call.outputs]
            call_without_outputs = lazy_call.set_input_values(inputs=inputs)
            call = call_without_outputs.set_output_values(outputs=outputs)
            return call

    def call_set(self, call_uid: str, call: Call) -> None:
        self.call_cache.set(call_uid, call)

    def obj_get(self, obj_uid: str) -> ValueRef:
        if self.obj_cache.exists(obj_uid):
            return self.obj_cache.get(obj_uid)
        return self.rel_adapter.obj_get(uid=obj_uid)

    def obj_set(self, obj_uid: str, vref: ValueRef) -> None:
        self.obj_cache.set(obj_uid, vref)

    def preload_objs(self, keys: list[str]):
        keys_not_in_cache = [key for key in keys if not self.obj_cache.exists(key)]
        for idx, row in self.rel_adapter.obj_gets(keys_not_in_cache).iterrows():
            self.obj_cache.set(k=row[Config.uid_col], v=row["value"])

    def evict_caches(self):
        for k in self.call_cache.keys():
            self.call_cache.delete(k=k)
        for k in self.obj_cache.keys():
            self.obj_cache.delete(k=k)

    def commit(self, calls: Optional[list[Call]] = None):
        """
        Flush calls and objs from the cache that haven't yet been written to DuckDB.
        """
        if calls is None:
            new_objs = {
                key: self.obj_cache.get(key) for key in self.obj_cache.dirty_entries
            }
            new_calls = [
                self.call_cache.get(key) for key in self.call_cache.dirty_entries
            ]
        else:
            new_objs = {}
            for call in calls:
                for vref in itertools.chain(call.inputs.values(), call.outputs):
                    new_objs[vref.uid] = vref
            new_calls = calls
        self.rel_adapter.obj_sets(new_objs)
        self.rel_adapter.upsert_calls(new_calls)
        if Config.evict_on_commit:
            self.evict_caches()

        # Remove dirty bits from cache.
        self.obj_cache.dirty_entries.clear()
        self.call_cache.dirty_entries.clear()

    ############################################################################
    ### remote sync operations
    ############################################################################
    @transaction()
    def bundle_to_remote(
        self, conn: Optional[Connection] = None
    ) -> RemoteEventLogEntry:
        """
        Collect the new calls according to the event log, and pack them into a
        dict of binary blobs to be sent off to the remote server.

        NOTE: this also renames tables and columns to their immutable internal
        names.
        """
        # Bundle event log and referenced calls into tables.
        # event_log_df = self.rel_storage.get_data(
        #     self.rel_adapter.EVENT_LOG_TABLE, conn=conn
        # )
        event_log_df = self.rel_adapter.get_event_log(conn=conn)
        tables_with_changes = {}
        table_names_with_changes = event_log_df["table"].unique()

        event_log_table = Table(self.rel_adapter.EVENT_LOG_TABLE)
        for table_name in table_names_with_changes:
            table = Table(table_name)
            tables_with_changes[table_name] = self.rel_storage.execute_arrow(
                query=Query.from_(table)
                .join(event_log_table)
                .on(table[Config.uid_col] == event_log_table[Config.uid_col])
                .select(table.star),
                conn=conn,
            )
        # pass to internal names
        # evaluated_tables = {
        #     k: self.rel_adapter.evaluate_call_table(ta=v)
        #     for k, v in tables_with_changes.items()
        #     if k != Config.vref_table
        # }
        # logger.debug(f"Sending tables with changes: {evaluated_tables}")
        tables_with_changes = self.sig_adapter.rename_tables(
            tables_with_changes, to="internal"
        )
        output = {}
        for table_name, table in tables_with_changes.items():
            buffer = io.BytesIO()
            pq.write_table(table, buffer)
            output[table_name] = buffer.getvalue()
        # self.rel_adapter.clear_event_log(conn=conn)
        return output

    @transaction()
    def apply_from_remote(
        self, changes: list[RemoteEventLogEntry], conn: Optional[Connection] = None
    ):
        """
        Apply new calls from the remote server.

        NOTE: this also renames tables and columns to their UI names.
        """
        for raw_changeset in changes:
            changeset_data = {}
            for table_name, serialized_table in raw_changeset.items():
                buffer = io.BytesIO(serialized_table)
                deserialized_table = pq.read_table(buffer)
                changeset_data[table_name] = deserialized_table
            # pass to UI names
            changeset_data = self.sig_adapter.rename_tables(
                tables=changeset_data, to="ui", conn=conn
            )
            for table_name, deserialized_table in changeset_data.items():
                self.rel_storage.upsert(table_name, deserialized_table, conn=conn)
        # evaluated_tables = {k: self.rel_adapter.evaluate_call_table(ta=v, conn=conn) for k, v in data.items() if k != Config.vref_table}
        # logger.debug(f'Applied tables from remote: {evaluated_tables}')

    def sync_from_remote(self):
        """
        Pull new calls from the remote server.

        Note that the server's schema (i.e. signatures) can be a super-schema of
        the local schema, but all local schema elements must be present in the
        remote schema, because this is enforced by how schema updates are
        performed.
        """
        if not isinstance(self.root, RemoteStorage):
            return
        # apply signature changes from the server first, because the new calls
        # from the server may depend on the new schema.
        self.sig_syncer.sync_from_remote()
        # next, pull new calls
        new_log_entries, timestamp = self.root.get_log_entries_since(
            self.last_timestamp
        )
        self.apply_from_remote(new_log_entries)
        self.last_timestamp = timestamp
        logger.debug("synced from remote")

    def sync_to_remote(self):
        """
        Send calls to the remote server.

        As with `sync_from_remote`, the server may have a super-schema of the
        local schema. The current signatures are first pulled and applied to the
        local schema.
        """
        if not isinstance(self.root, RemoteStorage):
            # todo: there should be a way to completely ignore the event log
            # when there's no remote
            self.rel_adapter.clear_event_log()
        else:
            # collect new work and send it to the server
            changes = self.bundle_to_remote()
            self.root.save_event_log_entry(changes)
            # clear the event log only *after* the changes have been received
            self.rel_adapter.clear_event_log()
            logger.debug("synced to remote")

    def sync_with_remote(self):
        if not isinstance(self.root, RemoteStorage):
            return
        self.sync_to_remote()
        self.sync_from_remote()

    ############################################################################
    ### signature sync, renaming, refactoring
    ############################################################################
    @property
    def is_clean(self) -> bool:
        """
        Check that the storage has no uncommitted calls or objects.
        """
        return (
            self.call_cache.is_clean and self.obj_cache.is_clean
        )  # and self.rel_adapter.event_log_is_clean()

    ############################################################################
    ### creating contexts
    ############################################################################
    def run(self, **kwargs) -> Context:
        return run(storage=self, **kwargs)

    def query(self, **kwargs) -> Context:
        return query(storage=self, **kwargs)

    def batch(self, **kwargs) -> Context:
        return batch(storage=self, **kwargs)

    ############################################################################
    ### low-level primitives to make calls in contexts
    ############################################################################
    def execute_query(self, select_queries: List[ValQuery]) -> pd.DataFrame:
        """
        Execute the given queries and return the result as a pandas DataFrame.
        """
        if not select_queries:
            return pd.DataFrame()
        val_queries, func_queries = traverse_all(select_queries)
        compiler = Compiler(val_queries=val_queries, func_queries=func_queries)
        query = compiler.compile(select_queries=select_queries)
        df = self.rel_storage.execute_df(query=str(query))

        # now, evaluate the table
        keys_to_collect = [
            item for _, column in df.items() for _, item in column.items()
        ]
        self.preload_objs(keys_to_collect)
        result = df.applymap(lambda key: unwrap(self.obj_get(key)))

        # finally, name the columns
        cols = [
            f"unnamed_{i}" if query.column_name is None else query.column_name
            for i, query in zip(range(len((result.columns))), select_queries)
        ]
        result.rename(columns=dict(zip(result.columns, cols)), inplace=True)
        return result

    def call_run(
        self, func_op: FuncOp, inputs: Dict[str, Union[Any, ValueRef]]
    ) -> Tuple[List[ValueRef], Call]:
        # if op.is_invalidated:
        #     raise RuntimeError(
        #         "This function has been invalidated due to a change in the signature, and cannot be called"
        #     )
        # wrap inputs
        wrapped_inputs = wrap_inputs(inputs)
        # get call UID using *internal names* to guarantee the same UID will be
        # assigned regardless of renamings
        hashable_input_uids = {}
        for k, v in wrapped_inputs.items():
            internal_k = func_op.sig.ui_to_internal_input_map[k]
            if internal_k in func_op.sig._new_input_defaults_uids:
                if func_op.sig._new_input_defaults_uids[internal_k] == v.uid:
                    continue
            hashable_input_uids[internal_k] = v.uid
        call_uid = Hashing.get_content_hash(
            obj=[
                hashable_input_uids,
                func_op.sig.versioned_internal_name,
            ]
        )
        # check if call UID exists in call storage
        if self.call_exists(call_uid):
            # get call from call storage
            call = self.call_get(call_uid)
            # get outputs from obj storage
            self.preload_objs([v.uid for v in call.outputs])
            wrapped_outputs = [self.obj_get(v.uid) for v in call.outputs]
            # return outputs and call
            return wrapped_outputs, call
        else:
            # compute op
            if Config.autounwrap_inputs:
                raw_inputs = {k: v.obj for k, v in wrapped_inputs.items()}
            else:
                raw_inputs = wrapped_inputs
            outputs = func_op.compute(raw_inputs)
            # wrap outputs
            wrapped_outputs = wrap_outputs(outputs, call_uid=call_uid)
            # create call
            call = Call(
                uid=call_uid,
                inputs=wrapped_inputs,
                outputs=wrapped_outputs,
                func_op=func_op,
            )
            # save *detached* call in call storage
            self.call_set(call_uid, call)
            # set inputs and outputs in obj storage
            for v in itertools.chain(wrapped_outputs, wrapped_inputs.values()):
                self.obj_set(v.uid, v)
            # return outputs and call
            return wrapped_outputs, call

    def call_query(
        self, func_op: FuncOp, inputs: Dict[str, ValQuery]
    ) -> List[ValQuery]:
        if not all(isinstance(inp, ValQuery) for inp in inputs.values()):
            raise NotImplementedError()
        func_query = FuncQuery(func_op=func_op, inputs=inputs)
        for k, v in inputs.items():
            v.add_consumer(consumer=func_query, consumed_as=k)
        outputs = [
            ValQuery(creator=func_query, created_as=i)
            for i in range(func_op.sig.n_outputs)
        ]
        func_query.set_outputs(outputs=outputs)
        return outputs

    def call_batch(
        self, func_op: FuncOp, inputs: Dict[str, ValueRef]
    ) -> Tuple[List[ValueRef], CallStruct]:
        outputs = [ValueRef.make_delayed() for _ in range(func_op.sig.n_outputs)]
        call_struct = CallStruct(func_op=func_op, inputs=inputs, outputs=outputs)
        # context._call_structs.append((self.func_op, wrapped_inputs, outputs))
        return outputs, call_struct


class WorkflowExecutor(ABC):
    @abstractmethod
    def execute(self, workflow: Workflow, storage: Storage) -> List[Call]:
        pass


class SimpleWorkflowExecutor(WorkflowExecutor):
    def execute(self, workflow: Workflow, storage: Storage) -> List[Call]:
        result = []
        for op_node in workflow.op_nodes:
            call_structs = workflow.op_node_to_call_structs[op_node]
            for call_struct in call_structs:
                func_op, inputs, outputs = (
                    call_struct.func_op,
                    call_struct.inputs,
                    call_struct.outputs,
                )
                assert all(
                    [not ValueRef.is_delayed(vref=inp) for inp in inputs.values()]
                )
                vref_outputs, call = storage.call_run(
                    func_op=func_op,
                    inputs=inputs,
                )
                # overwrite things
                for output, vref_output in zip(outputs, vref_outputs):
                    output.obj = vref_output.obj
                    output.uid = vref_output.uid
                    output.in_memory = True
                result.append(call)
        # filter out repeated calls
        result = list({call.uid: call for call in result}.values())
        return result