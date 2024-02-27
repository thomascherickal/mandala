from ..common_imports import *
from ..core.config import *
from ..core.model import Ref, FuncOp
from ..core.builtins_ import StructOrientations
from ..queries.graphs import copy_subgraph
from ..core.prov import propagate_struct_provenance, BUILTIN_OP_IDS
from ..queries.weaver import CallNode, ValNode
from ..queries.viz import GraphPrinter, visualize_graph
from .funcs import FuncInterface
from .storage import Storage, ValueLoader


class RefFunctor:
    """
    An in-memory dynamic representation of a slice of storage representing some
    computation, with a set of operations that turn it into a generalized
    dataframe.
    """

    def __init__(
        self,
        call_nodes: Dict[str, CallNode],
        val_nodes: Dict[str, ValNode],
        storage: Storage,
        prov_df: Optional[pd.DataFrame] = None,
    ):
        self.call_nodes = call_nodes
        self.val_nodes = val_nodes

        self.storage = storage
        if prov_df is None:
            prov_df = storage.rel_storage.get_data(table=Config.provenance_table)
            prov_df = propagate_struct_provenance(prov_df)
        self.prov_df = prov_df

    def __getitem__(
        self, indexer: Union[str, Iterable[str], np.ndarray]
    ) -> Union[pd.Series, pd.DataFrame, "RefFunctor"]:
        if isinstance(indexer, str):
            return pd.Series(self.val_nodes[indexer].refs, name=indexer)
        elif isinstance(indexer, list) and all(isinstance(x, str) for x in indexer):
            return pd.DataFrame({col: self.val_nodes[col].refs for col in indexer})
        elif isinstance(indexer, np.ndarray):
            # boolean mask
            if indexer.dtype == bool:
                res = self.copy()
                for k, v in res.val_nodes.items():
                    v.mask(indexer)
                for k, v in res.call_nodes.items():
                    v.mask(indexer)
                return res
            else:
                raise NotImplementedError(
                    "Indexing with a non-boolean mask is not supported"
                )
        else:
            raise ValueError(
                f"Invalid indexer type into {self.__class__}: {type(indexer)}"
            )

    def eval(
        self, indexer: Optional[Union[str, List[str]]] = None
    ) -> Union[pd.Series, pd.DataFrame]:
        if indexer is None:
            indexer = list(self.val_nodes.keys())
        if isinstance(indexer, str):
            full_uids_series = self[indexer].apply(lambda x: x.full_uid)
            res = self.storage.eval_df(
                full_uids_df=full_uids_series.to_frame(), values="objs"
            )
            return res[indexer]
        else:
            full_uids_df = self[indexer].applymap(lambda x: x.full_uid)
            return self.storage.eval_df(full_uids_df=full_uids_df, values="objs")

    def creators(self, col: str) -> np.ndarray:
        calls, output_names = self.storage.get_creators(
            refs=self.val_nodes[col].refs, prov_df=self.prov_df
        )
        return np.array(
            [
                call.func_op.sig.versioned_ui_name if call is not None else None
                for call in calls
            ]
        )

    def consumers(self, col: str) -> np.ndarray:
        calls_list, input_names_list = self.storage.get_consumers(
            refs=self.val_nodes[col].refs, prov_df=self.prov_df
        )
        return np.array(
            [
                tuple(
                    [
                        call.func_op.sig.versioned_ui_name if call is not None else None
                        for call in calls
                    ]
                )
                for calls in calls_list
            ]
        )

    def back(
        self,
        cols: Optional[Union[str, List[str]]] = None,
        inplace: bool = False,
        silent_failure: bool = False,
        verbose: bool = False,
    ) -> "RefFunctor":
        """
        Given some columns, expand the data structure to include the ops and
        calls that created them, and the inputs to those calls.
        """
        if inplace:
            res = self
        else:
            res = self.copy()

        if cols is None:
            # this means we want to expand the entire graph
            node_frontier = res.val_nodes.keys()
            visited = set()
            while True:
                res = res.back(
                    cols=list(node_frontier),
                    inplace=inplace,
                    silent_failure=True,
                    verbose=verbose,
                )
                if verbose:
                    res.print()
                visited |= node_frontier
                nodes_after = set(res.val_nodes.keys())
                node_frontier = nodes_after - visited
                if not node_frontier:
                    break
            return res

        if verbose:
            logger.info(f"Expanding graph to include the provenance of columns {cols}")
        if isinstance(cols, str):
            cols = [cols]
        creator_data = {
            col: res.storage.get_creators(
                refs=res.val_nodes[col].refs, prov_df=res.prov_df
            )
            for col in cols
        }
        creator_calls = {col: v[0] for col, v in creator_data.items()}
        creator_output_names = {col: v[1] for col, v in creator_data.items()}
        filtered_cols = []
        for col in cols:
            if any(call is None for call in creator_calls[col]):
                reason = f"Some refs in column {col} were not created by any op"
                if silent_failure:
                    if verbose:
                        logger.info(f"{reason}; skipping column {col}")
                    continue
                else:
                    raise ValueError(reason)
            creator_ops = [
                call.func_op.sig.versioned_ui_name for call in creator_calls[col]
            ]
            if len(set(creator_ops)) > 1:
                reason = f"Values in column {col} were created by multiple ops: {creator_ops}"
                if silent_failure:
                    if verbose:
                        logger.info(f"{reason}; skipping column {col}")
                    continue
                else:
                    raise ValueError(reason)
            if len(set(creator_output_names[col])) > 1:
                reason = f"Values in column {col} were created by the same op, but with different output names: {creator_output_names[col]}"
                if silent_failure:
                    if verbose:
                        logger.info(f"{reason}; skipping column {col}")
                    continue
                else:
                    raise ValueError(reason)
            filtered_cols.append(col)
        cols = filtered_cols
        creator_calls = {col: creator_calls[col] for col in cols}
        creator_output_names = {col: creator_output_names[col] for col in cols}
        # col -> Op object that created its values
        creator_ops = {col: creator_calls[col][0].func_op for col in cols}
        creator_calls_uids = {
            col: [call.causal_uid for call in calls]
            for col, calls in creator_calls.items()
        }
        proto_call_nodes = {
            col: CallNode(
                inputs={},
                func_op=op,
                outputs={},
                call_uids=creator_calls_uids[col],
                constraint=None,
            )
            for col, op in creator_ops.items()
        }
        groups = defaultdict(list)
        for col_, call_node in proto_call_nodes.items():
            groups[call_node.call_uids_hash].append(col_)

        for col_group in groups.values():
            ### collect a bunch of data about this group
            col_representative = col_group[0]
            call_representative = creator_calls[col_representative][0]
            # the output names under which the cols appear
            col_to_output_name = {
                col: creator_output_names[col][0] for col in col_group
            }
            # the calls that created the columns
            group_calls = creator_calls[col_representative]
            # the input names and types for the op
            op = creator_ops[col_representative]
            input_types = op.input_types

            #! for struct calls, figure out the orientation
            if op.is_builtin:
                output_names = list(col_to_output_name.values())
                orientation = (
                    StructOrientations.construct
                    if any(x in output_names for x in ("lst", "dct", "st"))
                    else StructOrientations.destruct
                )
            else:
                orientation = None

            # create val nodes for the inputs
            input_nodes = {}
            for input_name, input_type in input_types.items():
                input_node = ValNode(
                    tp=input_type,
                    refs=[call.inputs[input_name] for call in group_calls],
                    constraint=None,
                )
                for v in res.val_nodes.values():
                    if v.refs_hash == input_node.refs_hash:
                        input_nodes[input_name] = v
                        break
                else:
                    input_nodes[input_name] = input_node
                    res.val_nodes[res.get_new_vname(hint=input_name)] = input_node

            # create the call node
            calls_hash = CallNode.get_call_uids_hash(
                call_uids=creator_calls_uids[col_representative]
            )
            for cnode in res.call_nodes.values():
                if cnode.call_uids_hash == calls_hash:
                    call_node = cnode
                    #! we may have to manually link some outputs to the existing
                    # call node
                    for col in col_group:
                        col_val_node = res.val_nodes[col]
                        if col_val_node not in call_node.outputs.values():
                            call_node.outputs[col_to_output_name[col]] = col_val_node
                            col_val_node.creators.append(call_node)
                            col_val_node.created_as.append(col_to_output_name[col])
                    break
            else:
                call_node = CallNode.link(
                    inputs=input_nodes,
                    func_op=op,
                    outputs={
                        col_to_output_name[col]: res.val_nodes[col] for col in col_group
                    },
                    constraint=None,
                    call_uids=creator_calls_uids[col_representative],
                    orientation=orientation,
                )
                res.call_nodes[res.get_new_cname(op)] = call_node
        return res

    ############################################################################
    ### creating new RefFunctors
    ############################################################################
    def copy(self) -> "RefFunctor":
        # must copy the graph
        val_map, call_map = copy_subgraph(
            vqs=set(self.val_nodes.values()),
            fqs=set(self.call_nodes.values()),
        )
        return RefFunctor(
            call_nodes={k: call_map[v] for k, v in self.call_nodes.items()},
            val_nodes={k: val_map[v] for k, v in self.val_nodes.items()},
            storage=self.storage,
            prov_df=self.prov_df,
        )

    @staticmethod
    def from_refs(
        refs: Iterable[Ref],
        storage: Storage,
        prov_df: Optional[pd.DataFrame] = None,
        name: Optional[str] = None,
    ) -> "RefFunctor":
        val_node = ValNode(
            constraint=None,
            tp=None,
            refs=list(refs),
        )
        name = "v0" if name is None else name
        return RefFunctor(
            call_nodes={},
            val_nodes={name: val_node},
            storage=storage,
            prov_df=prov_df,
        )

    @staticmethod
    def from_op(
        func: FuncInterface,
        storage: Storage,
        prov_df: Optional[pd.DataFrame] = None,
    ) -> "RefFunctor":
        """
        Get a RefFunctor expressing the memoization table for a single function
        """
        reftable = storage.get_table(func, values="lazy", meta=True)
        op = func.func_op
        if op.is_builtin:
            raise ValueError("Cannot create a RefFunctor from a builtin op")
        input_nodes = {
            input_name: ValNode(
                constraint=None,
                tp=op.input_types[input_name],
                refs=reftable[input_name].values.tolist(),
            )
            for input_name in op.input_types.keys()
        }
        output_nodes = {
            dump_output_name(i): ValNode(
                constraint=None,
                tp=tp,
                refs=reftable[dump_output_name(i)].values.tolist(),
            )
            for i, tp in enumerate(op.output_types)
        }
        call_node = CallNode.link(
            inputs=input_nodes,
            func_op=op,
            outputs=output_nodes,
            constraint=None,
            call_uids=reftable[Config.causal_uid_col].values.tolist(),
            orientation=None,
        )
        return RefFunctor(
            call_nodes={func.func_op.sig.versioned_ui_name: call_node},
            val_nodes={
                k: v
                for k, v in itertools.chain(input_nodes.items(), output_nodes.items())
            },
            storage=storage,
            prov_df=prov_df,
        )

    ############################################################################
    ### visualization
    ############################################################################
    def _get_string_representation(self) -> str:
        printer = GraphPrinter(
            vqs=set(self.val_nodes.values()),
            fqs=set(self.call_nodes.values()),
            names={v: k for k, v in self.val_nodes.items()},
            value_loader=ValueLoader(storage=self.storage),
        )
        return printer.print_computational_graph(show_sources_as="name_only")

    def __repr__(self) -> str:
        return self._get_string_representation()

    def print(self):
        print(self._get_string_representation())

    def show(self, how: Literal["inline", "browser"] = "browser"):
        visualize_graph(
            vqs=set(self.val_nodes.values()),
            fqs=set(self.call_nodes.values()),
            layout="computational",
            names={v: k for k, v in self.val_nodes.items()},
            show_how=how,
        )

    def get_new_vname(self, hint: Optional[str] = None) -> str:
        """
        Return the first name of the form `v{i}` that is not in self.val_nodes
        """
        if hint is not None and hint not in self.val_nodes:
            return hint
        i = 0
        prefix = "v" if hint is None else hint
        while f"{prefix}{i}" in self.val_nodes:
            i += 1
        return f"{prefix}{i}"

    def get_new_cname(self, op: FuncOp) -> str:
        if op.sig.versioned_ui_name not in self.call_nodes:
            return op.sig.versioned_ui_name
        i = 0
        while f"{op.sig.versioned_ui_name}_{i}" in self.call_nodes:
            i += 1
        return f"{op.sig.versioned_ui_name}_{i}"

    def rename(self, columns: Dict[str, str], inplace: bool = False):
        for old_name, new_name in columns.items():
            if old_name not in self.val_nodes:
                raise ValueError(f"Column {old_name} does not exist")
            if new_name in self.val_nodes:
                raise ValueError(f"Column {new_name} already exists")
        if inplace:
            res = self
        else:
            res = self.copy()
        for old_name, new_name in columns.items():
            res.val_nodes[new_name] = res.val_nodes.pop(old_name)
        return res

    def r(
        self,
        inplace: bool = False,
        **kwargs,
    ) -> "RefFunctor":
        """
        Fast alias for rename
        """
        return self.rename(columns=kwargs, inplace=inplace)
