# Copyright 2024 Marimo. All rights reserved.
from __future__ import annotations

import asyncio
import builtins
import contextlib
import dataclasses
import io
import itertools
import os
import pathlib
import signal
import sys
import threading
import time
import traceback
from copy import copy, deepcopy
from multiprocessing import connection
from typing import TYPE_CHECKING, Any, Callable, Iterator, List, Optional, cast
from uuid import uuid4

from marimo import _loggers
from marimo._ast.cell import CellConfig, CellImpl
from marimo._ast.compiler import compile_cell
from marimo._ast.variables import is_local
from marimo._ast.visitor import ImportData, Name, VariableData
from marimo._config.config import ExecutionType, MarimoConfig, OnCellChangeType
from marimo._config.settings import GLOBAL_SETTINGS
from marimo._data.preview_column import (
    get_column_preview_dataframe,
    get_column_preview_for_sql,
)
from marimo._dependencies.dependencies import DependencyManager
from marimo._messaging.cell_output import CellChannel
from marimo._messaging.context import http_request_context, run_id_context
from marimo._messaging.errors import (
    Error,
    MarimoInterruptionError,
    MarimoStrictExecutionError,
    MarimoSyntaxError,
    UnknownError,
)
from marimo._messaging.ops import (
    CellOp,
    CompletedRun,
    DataColumnPreview,
    FunctionCallResult,
    HumanReadableStatus,
    InstallingPackageAlert,
    MissingPackageAlert,
    PackageStatusType,
    RemoveUIElements,
    SQLTablePreview,
    VariableDeclaration,
    Variables,
    VariableValue,
    VariableValues,
)
from marimo._messaging.print_override import print_override
from marimo._messaging.streams import (
    QueuePipe,
    ThreadSafeStderr,
    ThreadSafeStdin,
    ThreadSafeStdout,
    ThreadSafeStream,
)
from marimo._messaging.tracebacks import write_traceback
from marimo._messaging.types import (
    KernelMessage,
    Stderr,
    Stdin,
    Stdout,
    Stream,
)
from marimo._output.rich_help import mddoc
from marimo._plugins.core.web_component import JSONType
from marimo._plugins.ui._core.ui_element import MarimoConvertValueException
from marimo._runtime import dataflow, handlers, marimo_pdb, patches
from marimo._runtime.app_meta import AppMeta
from marimo._runtime.context import (
    ContextNotInitializedError,
    ExecutionContext,
    get_context,
)
from marimo._runtime.context.kernel_context import (
    KernelRuntimeContext,
    initialize_kernel_context,
)
from marimo._runtime.context.types import teardown_context
from marimo._runtime.control_flow import MarimoInterrupt
from marimo._runtime.input_override import input_override
from marimo._runtime.packages.module_registry import ModuleRegistry
from marimo._runtime.packages.package_manager import PackageManager
from marimo._runtime.packages.package_managers import create_package_manager
from marimo._runtime.packages.utils import is_python_isolated
from marimo._runtime.params import CLIArgs, QueryParams
from marimo._runtime.redirect_streams import redirect_streams
from marimo._runtime.reload.autoreload import ModuleReloader
from marimo._runtime.reload.module_watcher import ModuleWatcher
from marimo._runtime.requests import (
    AppMetadata,
    CodeCompletionRequest,
    ControlRequest,
    CreationRequest,
    DeleteCellRequest,
    ExecuteMultipleRequest,
    ExecuteScratchpadRequest,
    ExecuteStaleRequest,
    ExecutionRequest,
    FunctionCallRequest,
    InstallMissingPackagesRequest,
    PreviewDatasetColumnRequest,
    PreviewSQLTableRequest,
    RenameRequest,
    SetCellConfigRequest,
    SetUIElementValueRequest,
    SetUserConfigRequest,
    StopRequest,
)
from marimo._runtime.runner import cell_runner
from marimo._runtime.runner.hooks import (
    ON_FINISH_HOOKS,
    POST_EXECUTION_HOOKS,
    PRE_EXECUTION_HOOKS,
    PREPARATION_HOOKS,
)
from marimo._runtime.runner.hooks_on_finish import OnFinishHookType
from marimo._runtime.runner.hooks_post_execution import PostExecutionHookType
from marimo._runtime.runner.hooks_pre_execution import PreExecutionHookType
from marimo._runtime.runner.hooks_preparation import PreparationHookType
from marimo._runtime.scratch import SCRATCH_CELL_ID
from marimo._runtime.state import State
from marimo._runtime.utils.set_ui_element_request_manager import (
    SetUIElementRequestManager,
)
from marimo._runtime.validate_graph import check_for_errors
from marimo._runtime.win32_interrupt_handler import Win32InterruptHandler
from marimo._server.model import SessionMode
from marimo._server.types import QueueType
from marimo._sql.engines import SQLAlchemyEngine
from marimo._sql.get_engines import get_engines_from_variables
from marimo._tracer import kernel_tracer
from marimo._types.ids import CellId_t, UIElementId, VariableName
from marimo._utils.assert_never import assert_never
from marimo._utils.platform import is_pyodide
from marimo._utils.signals import restore_signals
from marimo._utils.typed_connection import TypedConnection

if TYPE_CHECKING:
    import queue
    from collections.abc import Sequence
    from types import ModuleType

    from marimo._plugins.ui._core.ui_element import UIElement

LOGGER = _loggers.marimo_logger()


@mddoc
def defs() -> tuple[str, ...]:
    """Get the definitions of the currently executing cell.

    Returns:
        tuple[str, ...]: A tuple of the currently executing cell's defs.
    """
    try:
        ctx = get_context()
    except ContextNotInitializedError:
        return tuple()

    if ctx.execution_context is not None:
        return tuple(
            sorted(
                defn
                for defn in ctx.graph.cells[ctx.execution_context.cell_id].defs
            )
        )
    return tuple()


@mddoc
def refs() -> tuple[str, ...]:
    """Get the references of the currently executing cell.

    Returns:
        tuple[str, ...]: A tuple of the currently executing cell's refs.
    """
    try:
        ctx = get_context()
    except ContextNotInitializedError:
        return tuple()

    # builtins that have not been shadowed by the user
    unshadowed_builtins = set(builtins.__dict__.keys()).difference(
        set(ctx.graph.definitions.keys())
    )

    if ctx.execution_context is not None:
        return tuple(
            sorted(
                defn
                for defn in ctx.graph.cells[ctx.execution_context.cell_id].refs
                # exclude builtins that have not been shadowed
                if defn not in unshadowed_builtins
            )
        )
    return tuple()


@mddoc
def query_params() -> QueryParams:
    """Get the query parameters of a marimo app.

    Examples:
        Keep the text input in sync with the URL query parameters:

        ```python3
        # In it's own cell
        query_params = mo.query_params()

        # In another cell
        search = mo.ui.text(
            value=query_params["search"] or "",
            on_change=lambda value: query_params.set("search", value),
        )
        search
        ```

        You can also set the query parameters reactively:

        ```python3
        toggle = mo.ui.switch(label="Toggle me")
        toggle

        # In another cell
        query_params["is_enabled"] = toggle.value
        ```

    Returns:
        QueryParams: A QueryParams object containing the query parameters.
            You can directly interact with this object like a dictionary.
            If you mutate this object, changes will be persisted to the frontend
            query parameters and any other cells referencing the query parameters
            will automatically re-run.
    """
    return get_context().query_params


@mddoc
def app_meta() -> AppMeta:
    """Get the metadata of a marimo app.

    The `AppMeta` class provides access to runtime metadata about a marimo app,
    such as its display theme and execution mode.

    Examples:
        Get the current theme and conditionally set a plotting library's theme:

        ```python
        import altair as alt

        # Enable dark theme for Altair when marimo is in dark mode
        alt.themes.enable(
            "dark" if mo.app_meta().theme == "dark" else "default"
        )
        ```

        Show content only in edit mode:

        ```python
        # Only show this content when editing the notebook
        mo.md("# Developer Notes") if mo.app_meta().mode == "edit" else None
        ```

        Get the current request headers or user info:

        ```python
        request = mo.app_meta().request
        print(request.headers)
        print(request.user)
        ```

    Returns:
        AppMeta: An AppMeta object containing the app's metadata.
    """
    return AppMeta()


@mddoc
def cli_args() -> CLIArgs:
    """Get the command line arguments of a marimo notebook.

    Examples:
        `marimo edit notebook.py -- -size 10`

        ```python3
        # Access the command line arguments
        size = mo.cli_args().get("size") or 100

        for i in range(size):
            print(i)
        ```

    Returns:
        CLIArgs: A dictionary containing the command line arguments.
            This dictionary is read-only and cannot be mutated.
    """
    return get_context().cli_args


@mddoc
def notebook_dir() -> pathlib.Path | None:
    """Get the directory of the currently executing notebook.

    Returns:
        pathlib.Path | None: A pathlib.Path object representing the directory of the current
            notebook, or None if the notebook's directory cannot be determined.

    Examples:
        ```python
        data_file = mo.notebook_dir() / "data" / "example.csv"
        # Use the directory to read a file
        if data_file.exists():
            print(f"Found data file: {data_file}")
        else:
            print("No data file found")
        ```
    """
    try:
        ctx = get_context()
    except ContextNotInitializedError:
        # If we are not running in a notebook (e.g. exported to Jupyter),
        # return the current working directory
        return pathlib.Path().absolute()

    filename = ctx.filename
    if filename is not None:
        return pathlib.Path(filename).parent.absolute()

    return None


class URLPath(pathlib.PurePosixPath):
    """
    Wrapper around pathlib.Path that preserves the "://" in the URL protocol.
    """

    def __str__(self) -> str:
        return super().__str__().replace(":/", "://")


@mddoc
def notebook_location() -> pathlib.PurePath | None:
    """Get the location of the currently executing notebook.

    In WASM, this is the URL of webpage, for example, `https://my-site.com`.
    For nested paths, this is the URL including the origin and pathname.
    `https://<my-org>.github.io/<my-repo>/folder`.

    In non-WASM, this is the directory of the notebook, which is the same as
    `mo.notebook_dir()`.

    Examples:
        In order to access data both locally and when a notebook runs via
        WebAssembly (e.g. hosted on GitHub Pages), you can use this
        approach to fetch data from the notebook's location.

        ```python
        import polars as pl

        data_path = mo.notebook_location() / "public" / "data.csv"
        df = pl.read_csv(str(data_path))
        df.head()
        ```

    Returns:
        Path | None: A Path object representing the URL or directory of the current
            notebook, or None if the notebook's directory cannot be determined.
    """
    if is_pyodide():
        from js import location  # type: ignore

        path_location = pathlib.Path(str(location))
        # The location looks like https://marimo-team.github.io/marimo-gh-pages-template/notebooks/assets/worker-BxJ8HeOy.js
        # We want to crawl out of the assets/ folder
        if "assets" in path_location.parts:
            return URLPath(str(path_location.parent.parent))
        return URLPath(str(path_location))

    else:
        return notebook_dir()


@dataclasses.dataclass
class CellMetadata:
    """CellMetadata class for storing cell metadata.

    Metadata the kernel needs to persist, even when a cell is removed
    from the graph or when a cell can't be formed from user code due to syntax
    errors.

    Attributes:
        config (CellConfig): Configuration for the cell.
    """

    config: CellConfig = dataclasses.field(default_factory=CellConfig)


class Kernel:
    """Kernel that manages the dependency graph and its execution.

    Args:
        cell_configs (dict[CellId_t, CellConfig]): Initial configuration for each cell.
        app_metadata (AppMetadata): Metadata about the notebook.
        user_config (MarimoConfig): The initial user configuration.
        stream (Stream): Object used to communicate with the server/outside world.
        stdout (Stdout | None): Replacement for sys.stdout.
        stderr (Stderr | None): Replacement for sys.stderr.
        stdin (Stdin | None): Replacement for sys.stdin.
        module (ModuleType): Module in which to execute code.
        enqueue_control_request (Callable[[ControlRequest], None]): Callback to enqueue control requests.
        debugger_override (marimo_pdb.MarimoPdb | None): A replacement for the built-in Pdb.
    """

    def __init__(
        self,
        *,
        cell_configs: dict[CellId_t, CellConfig],
        app_metadata: AppMetadata,
        user_config: MarimoConfig,
        stream: Stream,
        stdout: Stdout | None,
        stderr: Stderr | None,
        stdin: Stdin | None,
        module: ModuleType,
        enqueue_control_request: Callable[[ControlRequest], None],
        preparation_hooks: list[PreparationHookType] | None = None,
        pre_execution_hooks: list[PreExecutionHookType] | None = None,
        post_execution_hooks: list[PostExecutionHookType] | None = None,
        on_finish_hooks: list[OnFinishHookType] | None = None,
        debugger_override: marimo_pdb.MarimoPdb | None = None,
    ) -> None:
        self.app_metadata = app_metadata
        self.query_params = QueryParams(app_metadata.query_params)
        self.cli_args = CLIArgs(app_metadata.cli_args)
        self.stream = stream
        self.stdout = stdout
        self.stderr = stderr
        self.stdin = stdin
        self.enqueue_control_request = enqueue_control_request
        # timestamp at which most recently processed interrupt was seen;
        # the kernel rejects run requests that were issued before that
        # timestamp, to save the user from having to spam the interrupt button
        self.last_interrupt_timestamp: Optional[float] = None

        self._preparation_hooks = (
            preparation_hooks
            if preparation_hooks is not None
            else PREPARATION_HOOKS
        )
        self._pre_execution_hooks = (
            pre_execution_hooks
            if pre_execution_hooks is not None
            else PRE_EXECUTION_HOOKS
        )
        self._post_execution_hooks = (
            post_execution_hooks
            if post_execution_hooks is not None
            else POST_EXECUTION_HOOKS
        )
        self._on_finish_hooks = (
            on_finish_hooks if on_finish_hooks is not None else ON_FINISH_HOOKS
        )

        self._globals_lock = threading.RLock()
        self._completion_worker_started = False

        self.debugger = debugger_override
        if self.debugger is not None:
            patches.patch_pdb(self.debugger)

        self._module = module
        if self.app_metadata.filename is not None:
            # TODO(akshayka): When a file is renamed / moved to another folder,
            # we need to update sys.path.
            try:
                notebook_directory = str(
                    pathlib.Path(self.app_metadata.filename).parent.absolute()
                )
                if notebook_directory not in sys.path:
                    sys.path.insert(0, notebook_directory)
            except Exception as e:
                LOGGER.warning(
                    "Failed to add directory to path (error %e)", str(e)
                )
        elif "" not in sys.path:
            # an empty string represents ...
            #   the current directory, when using
            #      marimo edit filename.py / marimo run
            #   the marimo home directory, when using
            #      marimo edit (ie homepage)
            sys.path.insert(0, "")

        self.graph = dataflow.DirectedGraph()
        # When autorun on startup is disabled, this holds cells that have
        # not yet been run; these cells are removed when they or their
        # descendants are run
        self._uninstantiated_execution_requests: dict[
            CellId_t, ExecutionRequest
        ] = {}
        self.cell_metadata: dict[CellId_t, CellMetadata] = {
            cell_id: CellMetadata(config=config)
            for cell_id, config in cell_configs.items()
        }
        self.module_registry = ModuleRegistry(
            self.graph, excluded_modules=set()
        )
        self.package_manager: PackageManager | None = None
        self.module_reloader: ModuleReloader | None = None
        self.module_watcher: ModuleWatcher | None = None

        # Load runtime settings from user config
        self.user_config = user_config
        self.reactive_execution_mode: OnCellChangeType = user_config[
            "runtime"
        ]["on_cell_change"]
        self.execution_type: ExecutionType = user_config.get(
            "experimental", {}
        ).get("execution_type", "relaxed")
        self._update_runtime_from_user_config(user_config)

        # initializers to override construction of ui elements
        self.ui_initializers: dict[str, Any] = {}
        # errored cells
        self.errors: dict[CellId_t, tuple[Error, ...]] = {}
        # Mapping from state to the cell when its setter
        # was invoked. New state updates evict older ones.
        self.state_updates: dict[State[Any], CellId_t] = {}

        # Webbrowser may not be set (e.g. docker container) or stubbed/broken
        # (e.g. in pyodide). Set default to just inject an iframe of the
        # expected page to output.
        patches.patch_webbrowser()
        # micropip only patched in non-pyodide environments.
        if not is_pyodide():
            patches.patch_micropip(self.globals)

        exec("import marimo as __marimo__", self.globals)

    def teardown(self) -> None:
        """Teardown resources owned by the kernel."""
        if self.stdout is not None:
            self.stdout.stop()
        if self.stderr is not None:
            self.stderr.stop()
        if self.stdin is not None:
            self.stdin.stop()
        self.stream.stop()

        if self.module_watcher is not None:
            self.module_watcher.stop()

        # TODO(akshayka): There's a memory leak in run mode, with memory
        # usage increasing with each session creation. Somehow the kernel
        # globals appear to leak, even though the thread exits. As a hack we
        # manually clear kernel memory.
        self._module.__dict__.clear()

    def lazy(self) -> bool:
        return self.reactive_execution_mode == "lazy"

    def _execute_stale_cells_callback(self) -> None:
        return self.enqueue_control_request(ExecuteStaleRequest())

    def _execute_install_missing_packages_callback(
        self, package_manager: str, packages: list[str]
    ) -> None:
        version = {pkg: "" for pkg in packages}
        return self.enqueue_control_request(
            InstallMissingPackagesRequest(
                manager=package_manager, versions=version
            )
        )

    def _update_runtime_from_user_config(self, config: MarimoConfig) -> None:
        package_manager = config["package_management"]["manager"]
        autoreload_mode = config["runtime"]["auto_reload"]
        self.reactive_execution_mode = config["runtime"]["on_cell_change"]
        self.user_config = config

        if (
            self.package_manager is None
            or package_manager != self.package_manager.name
        ):
            self.package_manager = create_package_manager(package_manager)

            if self._should_update_script_metadata():
                # All marimo notebooks depend on the marimo package; if the
                # notebook already has marimo as a dependency, or an optional
                # dependency group with marimo, such as marimo[sql], this is a
                # NOOP.
                self._update_script_metadata(["marimo"])

        if (
            (autoreload_mode == "lazy" or autoreload_mode == "autorun")
            # Pyodide doesn't support hot module reloading
            and not is_pyodide()
        ):
            if self.module_reloader is None:
                self.module_reloader = ModuleReloader()

            if (
                self.module_watcher is not None
                and self.module_watcher.mode != autoreload_mode
            ):
                self.module_watcher.stop()
                self.module_watcher = None

            if self.module_watcher is None:
                self.module_watcher = ModuleWatcher(
                    self.graph,
                    reloader=self.module_reloader,
                    enqueue_run_stale_cells=self._execute_stale_cells_callback,
                    mode=autoreload_mode,
                    stream=self.stream,
                )
        else:
            self.module_reloader = None
            if self.module_watcher is not None:
                self.module_watcher.stop()
                self.module_watcher = None

    @property
    def globals(self) -> dict[Any, Any]:
        return self._module.__dict__

    @contextlib.contextmanager
    def lock_globals(self) -> Iterator[None]:
        # The only other thread accessing globals is the completion worker. If
        # we haven't started a completion worker, there's no need to lock
        # globals.
        if self._completion_worker_started:
            with self._globals_lock:
                yield
        else:
            yield

    def start_completion_worker(
        self, completion_queue: QueueType[CodeCompletionRequest]
    ) -> None:
        """Must be called after context is initialized"""
        from marimo._runtime.complete import completion_worker

        threading.Thread(
            target=completion_worker,
            args=(
                completion_queue,
                self.graph,
                self.globals,
                self._globals_lock,
                get_context().stream,
            ),
            daemon=True,
        ).start()
        self._completion_worker_started = True

    @kernel_tracer.start_as_current_span("code_completion")
    def code_completion(
        self, request: CodeCompletionRequest, docstrings_limit: int
    ) -> None:
        from marimo._runtime.complete import complete

        complete(
            request,
            self.graph,
            self.globals,
            self._globals_lock,
            get_context().stream,
            docstrings_limit,
        )

    @contextlib.contextmanager
    def _install_execution_context(
        self, cell_id: CellId_t, setting_element_value: bool = False
    ) -> Iterator[ExecutionContext]:
        ctx = get_context()
        assert isinstance(ctx, KernelRuntimeContext)
        ctx.execution_context = (
            exec_ctx := ExecutionContext(cell_id, setting_element_value)
        )
        with (
            get_context().provide_ui_ids(str(cell_id)),
            redirect_streams(
                cell_id,
                stream=self.stream,
                stdout=self.stdout,
                stderr=self.stderr,
                stdin=self.stdin,
            ),
        ):
            modules = None
            try:
                if self.module_reloader is not None:
                    # Reload modules if they have changed
                    modules = set(sys.modules)
                    self.module_reloader.check(
                        modules=sys.modules, reload=True
                    )
                yield exec_ctx
            finally:
                ctx.execution_context = None
                if self.module_reloader is not None and modules is not None:
                    # Note timestamps for newly loaded modules
                    new_modules = set(sys.modules) - modules
                    self.module_reloader.check(
                        modules={m: sys.modules[m] for m in new_modules},
                        reload=False,
                    )

    def _register_cell(self, cell_id: CellId_t, cell: CellImpl) -> None:
        if cell_id in self.cell_metadata:
            # If we already have a config for this cell id, restore it
            # This can happen when a cell was previously deactivated (due to a
            # syntax error or multiple definition error, for example) and then
            # re-registered
            cell.configure(self.cell_metadata[cell_id].config)
        elif cell_id not in self.cell_metadata:
            self.cell_metadata[cell_id] = CellMetadata()

        self.graph.register_cell(cell_id, cell)
        # leaky abstraction: the graph doesn't know about stale modules, so
        # we have to check for them here.
        module_reloader = self.module_reloader
        if (
            module_reloader is not None
            and module_reloader.cell_uses_stale_modules(cell)
        ):
            self.graph.set_stale(set([cell.cell_id]), prune_imports=True)
        LOGGER.debug("registered cell %s", cell_id)
        LOGGER.debug("parents: %s", self.graph.parents[cell_id])
        LOGGER.debug("children: %s", self.graph.children[cell_id])

    def _try_compiling_cell(
        self, cell_id: CellId_t, code: str, carried_imports: list[ImportData]
    ) -> tuple[Optional[CellImpl], Optional[Error]]:
        error: Optional[Error] = None
        try:
            cell = compile_cell(
                code, cell_id=cell_id, carried_imports=carried_imports
            )
        except Exception as e:
            cell = None
            if isinstance(e, SyntaxError):
                tmpio = io.StringIO()
                traceback.print_exc(file=tmpio, limit=0)
                tmpio.seek(0)
                syntax_error = tmpio.read().split("\n")
                # first line has the form File XXX, line XXX
                syntax_error[0] = syntax_error[0][
                    syntax_error[0].find("line") :
                ]
                error = MarimoSyntaxError(msg="\n".join(syntax_error))
            else:
                tmpio = io.StringIO()
                traceback.print_exc(file=tmpio)
                tmpio.seek(0)
                error = UnknownError(msg=tmpio.read())
        return cell, error

    def _try_registering_cell(
        self, cell_id: CellId_t, code: str, carried_imports: list[ImportData]
    ) -> Optional[Error]:
        """Attempt to register a cell with given id and code.

        Precondition: a cell with the supplied id must not already exist in the
        graph.

        If cell was unable to be registered, returns an Error object.
        """
        cell, error = self._try_compiling_cell(cell_id, code, carried_imports)

        if cell is not None:
            self._register_cell(cell_id, cell)

        return error

    def _maybe_register_cell(
        self, cell_id: CellId_t, code: str
    ) -> tuple[set[CellId_t], Optional[Error]]:
        """Register a cell (given by id, code) if not already registered.

        If a cell with id `cell_id` is already registered but with different
        code, that cell is deleted from the graph and a new cell with the
        same id but different code is registered.

        Returns:
        - a set of ids for cells that were previously children of `cell_id`
          but are no longer children of `cell_id` after registration;
          only non-empty when `cell-id` was already registered but with
          different code.
        - an `Error` if the cell couldn't be registered, `None` otherwise
        """
        previous_children: set[CellId_t] = set()
        error = None
        if not self.graph.is_cell_cached(cell_id, code):
            previous_cell = self.graph.cells.get(cell_id, None)

            if (
                previous_cell is not None
                and previous_cell.import_workspace.is_import_block
            ):
                # If the previous is being replaced by another import block,
                # then the new cell should try to carry over the previous
                # cell's imports to prevent unnecessary re-runs.
                carried_imports = [
                    import_data
                    for import_data in previous_cell.imports
                    if import_data.definition in self.globals
                    and import_data.definition
                    in previous_cell.import_workspace.imported_defs
                ]
            else:
                carried_imports = []

            if previous_cell is not None:
                LOGGER.debug("Deleting cell %s", cell_id)
                previous_children = self._deactivate_cell(cell_id)

            error = self._try_registering_cell(
                cell_id, code, carried_imports=carried_imports
            )
            if error is not None and previous_cell is not None:
                # The frontend keeps the cell around in the case of a
                # registration error; let the FE know the cell is no longer
                # stale.
                previous_cell.set_stale(False)

            # For any newly imported namespaces, add them to the metadata
            #
            # TODO(akshayka): Consider using the module watcher to discover
            # packages used by a notebook; that would have the benefit of
            # discovering transitive dependencies, ie if a notebook used a
            # local module that in turn used packages available on PyPI.
            if self._should_update_script_metadata():
                cell = self.graph.cells.get(cell_id, None)
                if cell:
                    prev_imports: set[Name] = (
                        set([im.namespace for im in previous_cell.imports])
                        if previous_cell
                        else set()
                    )
                    to_add = cell.imported_namespaces - prev_imports
                    self._update_script_metadata(
                        import_namespaces_to_add=list(to_add)
                    )

        LOGGER.debug(
            "graph:\n\tcell id %s\n\tparents %s\n\tchildren %s\n\tsiblings %s",
            cell_id,
            self.graph.parents,
            self.graph.children,
            self.graph.siblings,
        )

        # We only return cells that were previously children of cell_id
        # but are no longer children of the newly registered cell; these
        # returned cells are stale.
        children = self.graph.children.get(cell_id, set())
        return previous_children - children, error

    def _delete_variables(
        self,
        variables: dict[Name, list[VariableData]],
        exclude_defs: set[Name],
    ) -> None:
        """Delete `names` from kernel, except for `exclude_defs`"""
        for name, variable_data in variables.items():
            # Take the last definition of the variable
            variable = variable_data[-1]
            if name in exclude_defs:
                continue

            if variable.kind == "table" and DependencyManager.duckdb.has():
                import duckdb

                # We only drop in-memory tables: we don't want to drop tables
                # on databases!
                try:
                    duckdb.execute(f"DROP TABLE IF EXISTS memory.main.{name}")
                except Exception as e:
                    LOGGER.warning("Failed to drop table %s: %s", name, str(e))
            elif variable.kind == "view" and DependencyManager.duckdb.has():
                import duckdb

                # We only drop in-memory views for the same reason.
                try:
                    duckdb.execute(f"DROP VIEW IF EXISTS memory.main.{name}")
                except Exception as e:
                    LOGGER.warning("Failed to drop view %s: %s", name, str(e))
            elif variable.kind == "catalog" and DependencyManager.duckdb.has():
                import duckdb

                try:
                    duckdb.execute(f"DETACH DATABASE IF EXISTS {name}")
                except Exception as e:
                    LOGGER.warning(
                        "Failed to detach catalog %s: %s", name, str(e)
                    )
            else:
                if name in self.globals:
                    del self.globals[name]

                if (
                    "__annotations__" in self.globals
                    and name in self.globals["__annotations__"]
                ):
                    del self.globals["__annotations__"][name]

    def _invalidate_cell_state(
        self,
        cell_id: CellId_t,
        exclude_defs: Optional[set[Name]] = None,
        deletion: bool = False,
    ) -> None:
        """Cleanup state associated with this cell.

        Deletes a cell's defs from the kernel state, except for the names in
        `exclude_defs`, and instructs the frontend to invalidate its UI
        elements.
        """
        cell = self.graph.cells[cell_id]
        cell.import_workspace.imported_defs = set()
        missing_modules_before_deletion = (
            self.module_registry.missing_modules()
        )

        temporaries = {
            name: [VariableData(kind="variable")] for name in cell.temporaries
        }
        self._delete_variables(
            {**cell.variable_data, **temporaries},
            exclude_defs if exclude_defs is not None else set(),
        )

        missing_modules_after_deletion = (
            missing_modules_before_deletion & self.module_registry.modules()
        )
        if (
            self.package_manager is not None
            and missing_modules_after_deletion
            != missing_modules_before_deletion
        ):
            packages = list(
                sorted(
                    pkg
                    for mod in missing_modules_after_deletion
                    if not self.package_manager.attempted_to_install(
                        pkg := self.package_manager.module_to_package(mod)
                    )
                )
            )
            # Deleting a cell can make the set of missing packages smaller
            MissingPackageAlert(
                packages=packages,
                isolated=is_python_isolated(),
            ).broadcast()

        cell.set_output(None)
        get_context().cell_lifecycle_registry.dispose(
            cell_id, deletion=deletion
        )
        for descendent in self.graph.descendants(cell_id):
            get_context().cell_lifecycle_registry.dispose(
                descendent, deletion=deletion
            )
        RemoveUIElements(cell_id=cell_id).broadcast()

    def _deactivate_cell(self, cell_id: CellId_t) -> set[CellId_t]:
        """Deactivate: remove from graph, invalidate state, but keep metadata

        Keeps the cell's config, in case we see the same cell again.

        In contrast to deleting a cell, which fully scrubs the cell
        from the kernel and graph.
        """
        if cell_id not in self.errors:
            self._invalidate_cell_state(cell_id, deletion=True)
            return self.graph.delete_cell(cell_id)
        else:
            # An errored cell can be thought of as a cell that's in the graph
            # but that has no state in the kernel (because it was never run).
            # Its defs may overlap with defs of a non-errored cell, so we MUST
            # NOT delete/cleanup its defs from the kernel (i.e., an errored
            # cell shouldn't invalidate state of another cell).
            self.graph.delete_cell(cell_id)
            return set()

    def _delete_cell(self, cell_id: CellId_t) -> set[CellId_t]:
        """Delete a cell from the kernel and the graph.

        Deletion from the kernel involves removing cell's defs and
        de-registering its UI Elements.

        Deletion from graph is forwarded to graph object.
        """
        del self.cell_metadata[cell_id]
        cell = self.graph.cells[cell_id]
        cell.import_workspace.imported_defs = set()
        if self._should_update_script_metadata():
            self._update_script_metadata(
                import_namespaces_to_add=[],
            )

        return self._deactivate_cell(cell_id)

    def mutate_graph(
        self,
        execution_requests: Sequence[ExecutionRequest],
        deletion_requests: Sequence[DeleteCellRequest],
    ) -> set[CellId_t]:
        """Add and remove cells to/from the graph.

        This method adds the cells in `execution_requests` to the kernel's
        graph (deleting old versions of these cells, if any), and removes the
        cells in `deletion_requests` from the kernel's graph.

        The mutations that this method makes to the graph renders the
        kernel inconsistent (stale).

        This method does not register errors for cells that were previously
        valid and are not descendants of any of the newly registered cells.
        This is important for multiple definition errors, since a user may
        absent-mindedly redefine an existing name when creating a new cell:
        such a mistake shouldn't invalidate the program state.

        Returns:
        - set of cells that must be run to return kernel to consistent state
        """
        LOGGER.debug("Mutating graph.")
        LOGGER.debug("Current set of errors: %s", self.errors)
        cells_before_mutation = set(self.graph.cells.keys())
        cells_with_errors_before_mutation = set(self.errors.keys())
        edges_before = deepcopy(self.graph.children)
        definitions_before = set(self.graph.definitions.keys())

        # The set of cells that were successfully registered
        registered_cell_ids: set[CellId_t] = set()

        # The set of cells that need to be re-run due to cells being
        # deleted/re-registered.
        cells_that_were_children_of_mutated_cells: set[CellId_t] = set()

        # Cells that were unable to be added to the graph due to syntax errors
        syntax_errors: dict[CellId_t, Error] = {}

        # Register and delete cells
        for er in execution_requests:
            old_children, error = self._maybe_register_cell(
                er.cell_id, er.code
            )
            cells_that_were_children_of_mutated_cells |= old_children
            if error is None:
                registered_cell_ids.add(er.cell_id)
            else:
                syntax_errors[er.cell_id] = error

        for dr in deletion_requests:
            if dr.cell_id not in cells_before_mutation:
                continue
            cells_that_were_children_of_mutated_cells |= self._delete_cell(
                dr.cell_id
            )
        cells_in_graph = set(self.graph.cells.keys())

        # Check for semantic errors, like multiple definition errors, cycle
        # errors, and delete nonlocal errors.
        semantic_errors = check_for_errors(self.graph)
        LOGGER.debug("After mutation, syntax errors %s", syntax_errors)
        LOGGER.debug("Semantic errors %s", semantic_errors)

        # Prune semantic errors: we won't invalidate cells that were previously
        # valid, except for cells we just tried to register
        #
        # We don't want "action at a distance": running
        # a cell shouldn't invalidate cells that were previously valid
        # and weren't requested for execution
        previously_valid_cell_ids = (
            cells_in_graph
            # cells successfully registered
            - registered_cell_ids
            # cells that already had errors
            - cells_with_errors_before_mutation
        )

        # defs that we shouldn't remove from the graph
        keep_alive_defs: set[Name] = set()
        for cid in list(semantic_errors.keys()):
            # If a cell was previously valid, don't invalidate it unless
            # we have to, ie, unless it is a descendant of a just-registered
            # cell that has an error
            #
            # Handles the introduction of a multiple definition error, eg
            #
            # cell 1: x = 0
            # cell 2 (requested for execution): x = 1
            #
            # cell 1 won't be invalidated because cell 1 was previously valid
            # and there's no path from cell 2 to cell 1
            if cid in previously_valid_cell_ids and not any(
                self.graph.get_path(other_cid, cid)
                for other_cid in registered_cell_ids
            ):
                del semantic_errors[cid]
                keep_alive_defs |= self.graph.cells[cid].defs

        all_errors = {**semantic_errors}
        for cid, error in syntax_errors.items():
            # No chance of collision because cells with syntax errors are not
            # in the graph, so can't be in semantic errors
            assert cid not in all_errors
            all_errors[cid] = (error,)

        LOGGER.debug(
            "Final set of errors, after pruning valid cells: %s", all_errors
        )
        cells_with_errors_after_mutation = set(all_errors.keys())

        # Construct sets of cells that will need to be re-run.

        # Cells that previously had errors (eg, multiple definition or cycle)
        # that no longer have errors need to be refreshed.
        cells_that_no_longer_have_errors = (
            cells_with_errors_before_mutation
            - cells_with_errors_after_mutation
        ) & cells_in_graph

        if self.reactive_execution_mode == "autorun":
            for cid in cells_that_no_longer_have_errors:
                # clear error outputs before running
                CellOp.broadcast_output(
                    channel=CellChannel.OUTPUT,
                    mimetype="text/plain",
                    data="",
                    cell_id=cid,
                    status=None,
                )

        # Cells that were successfully registered need to be run
        cells_registered_without_error = (
            registered_cell_ids - cells_with_errors_after_mutation
        )

        # Cells that didn't have errors associated with them before the
        # run request but now have errors; these cells' descendants
        # will need to be run. Handles the case where a cell was cached (cell's
        # code didn't change), so its previous children were not added to
        # cells_that_were_children_of_mutated_cells
        cells_transitioned_to_error = (
            cells_with_errors_after_mutation
            - cells_with_errors_before_mutation
        ) & cells_before_mutation

        # Invalidate state defined by error-ed cells, with the exception of
        # names that were defined by valid cells (relevant for multiple
        # definition errors)
        for cid in all_errors:
            if cid not in self.graph.cells:
                # error is a registration error
                continue
            self.graph.cells[cid].set_run_result_status("marimo-error")
            self._invalidate_cell_state(cid, exclude_defs=keep_alive_defs)

        self.errors = all_errors
        for cid in self.errors:
            cell = self.graph.cells[cid] if cid in self.graph.cells else None
            if (
                cell is not None
                and not cell.config.disabled
                and self.graph.is_disabled(cid)
            ):
                # this may be the first time we're seeing the cell: set its
                # status
                cell.set_runtime_state("disabled-transitively")

            # The error is up-to-date, since we just processed the graph
            if cell is not None:
                cell.set_stale(False)
            else:
                # On syntax errors, we don't have a cell object.
                CellOp.broadcast_stale(cell_id=cid, stale=False)

            CellOp.broadcast_error(
                data=self.errors[cid],
                clear_console=True,
                cell_id=cid,
            )

        # Only broadcast Variables message if definitions changed
        edges_after = self.graph.children
        definitions_after = set(self.graph.definitions.keys())
        if (
            edges_before != edges_after
            or definitions_before != definitions_after
        ):
            Variables(
                variables=[
                    VariableDeclaration(
                        name=variable,
                        declared_by=list(declared_by),
                        used_by=list(
                            self.graph.get_referring_cells(
                                variable, language="python"
                            )
                        ),
                    )
                    for variable, declared_by in self.graph.definitions.items()
                ]
            ).broadcast()

        stale_cells = (
            set(
                itertools.chain(
                    cells_that_were_children_of_mutated_cells,
                    set().union(
                        *[
                            self.graph.children[cid]
                            for cid in cells_transitioned_to_error
                            if cid in self.graph.children
                        ]
                    )
                    - cells_with_errors_after_mutation,
                    cells_that_no_longer_have_errors,
                )
            )
            - cells_registered_without_error
        ) & cells_in_graph

        if self.reactive_execution_mode == "lazy":
            self.graph.set_stale(stale_cells, prune_imports=True)
            return cells_registered_without_error
        else:
            return cells_registered_without_error.union(stale_cells)

    async def _run_cells(self, cell_ids: set[CellId_t]) -> None:
        """Run cells and any state updates they trigger"""

        with run_id_context():
            # This patch is an attempt to mitigate problems caused by the fact
            # that in run mode, kernels run in threads and share the same
            # sys.modules. Races can still happen, but this should help in most
            # common cases. We could also be more aggressive and run this before
            # every cell, or even before pickle.dump/pickle.dumps()
            with patches.patch_main_module_context(self._module):
                while cell_ids := await self._run_cells_internal(cell_ids):
                    LOGGER.debug("Running state updates ...")
                    if self.lazy() and cell_ids:
                        self.graph.set_stale(cell_ids, prune_imports=True)
                        break
                LOGGER.debug("Finished run.")

    async def _if_autorun_then_run_cells(
        self, cell_ids: set[CellId_t]
    ) -> None:
        if self.reactive_execution_mode == "autorun":
            await self._run_cells(cell_ids)
        else:
            # We prune imports so that only cells depending on unimported
            # modules are marked as stale
            self.graph.set_stale(cell_ids, prune_imports=True)

    def _broadcast_missing_packages(self, runner: cell_runner.Runner) -> None:
        module_not_found_errors = [
            e
            for e in runner.exceptions.values()
            if isinstance(e, ModuleNotFoundError)
        ]
        if (
            len(module_not_found_errors) > 0
            and self.package_manager is not None
        ):
            # Grab missing modules from module registry and from module not found errors
            missing_modules = self.module_registry.missing_modules() | set(
                e.name for e in module_not_found_errors if e.name is not None
            )

            missing_packages = [
                pkg
                for mod in missing_modules
                # filter out packages that we already attempted to install
                # to prevent an infinite loop
                if not self.package_manager.attempted_to_install(
                    pkg := self.package_manager.module_to_package(mod)
                )
            ]

            if missing_packages:
                packages = list(sorted(missing_packages))
                if self.package_manager.should_auto_install():
                    self._execute_install_missing_packages_callback(
                        self.package_manager.name, packages
                    )
                else:
                    MissingPackageAlert(
                        packages=packages,
                        isolated=is_python_isolated(),
                    ).broadcast()

    def _propagate_kernel_errors(
        self,
        runner: cell_runner.Runner,
    ) -> None:
        for cell_id, error in runner.exceptions.items():
            if isinstance(error, MarimoStrictExecutionError):
                self.errors[cell_id] = (error,)

    async def _run_cells_internal(self, roots: set[CellId_t]) -> set[CellId_t]:
        """Run cells, send outputs to frontends

        Returns set of cells that need to be re-run due to state updates.
        """

        # Some hooks that are leaky and require the kernel
        # Free cell state ahead of running to relieve memory pressure
        #
        # NB: lazy kernels don't invalidate state of cancelled cells
        # descendants (cancelled == cells that raise exceptions), whereas
        # eager kernels do (since we clear all state ahead of time, and
        # have the closure of the roots in cells to run)
        def invalidate_state(runner: cell_runner.Runner) -> None:
            for cid in runner.cells_to_run:
                self._invalidate_cell_state(cid)

        def note_time_of_interruption(
            cell_impl: CellImpl,
            runner: cell_runner.Runner,
            run_result: cell_runner.RunResult,
        ) -> None:
            del cell_impl
            del runner
            if isinstance(run_result.exception, MarimoInterrupt):
                self.last_interrupt_timestamp = time.time()

        runner = cell_runner.Runner(
            roots=roots,
            graph=self.graph,
            glbls=self.globals,
            excluded_cells=set(self.errors.keys()),
            debugger=self.debugger,
            execution_mode=self.reactive_execution_mode,
            execution_type=self.execution_type,
            execution_context=self._install_execution_context,
            preparation_hooks=self._preparation_hooks + [invalidate_state],
            pre_execution_hooks=self._pre_execution_hooks,
            post_execution_hooks=self._post_execution_hooks
            + [note_time_of_interruption],
            on_finish_hooks=(
                self._on_finish_hooks
                + [
                    self._broadcast_missing_packages,
                    self._propagate_kernel_errors,
                ]
            ),
        )

        # I/O
        #
        # TODO(akshayka): when no logger is configured, log output is not
        #                 redirected to frontend (it's printed to console),
        #                 which is incorrect
        await runner.run_all()
        cells_with_stale_state = runner.resolve_state_updates(
            self.state_updates
        )
        self.state_updates.clear()
        return cells_with_stale_state

    def register_state_update(self, state: State[Any]) -> None:
        """Register a state object as having been updated.

        Should be called when a state's setter is called.
        """
        # store the state and the currently executing cell
        ctx = get_context()
        assert ctx.execution_context is not None
        self.state_updates[state] = ctx.execution_context.cell_id
        # TODO(akshayka): Send VariableValues message for any globals
        # bound to this state object (just like UI elements)

    @kernel_tracer.start_as_current_span("delete_cell")
    async def delete_cell(self, request: DeleteCellRequest) -> None:
        """Delete a cell from kernel and graph."""
        cell_id = request.cell_id
        if cell_id in self._uninstantiated_execution_requests:
            del self._uninstantiated_execution_requests[cell_id]

        if cell_id in self.graph.cells:
            await self._run_cells(
                self.mutate_graph(
                    execution_requests=[], deletion_requests=[request]
                )
            )

    @kernel_tracer.start_as_current_span("run")
    async def run(
        self, execution_requests: Sequence[ExecutionRequest]
    ) -> None:
        """Run cells and their descendants.


        The cells may be cells already existing in the graph or new cells.
        Adds the cells in `execution_requests` to the graph before running
        them. If the cells have uninstantiated ancestors, these are also run,
        allowing frontends to incrementally instantiate a notebook.

        Cells may use top-level await, which is why this function is async.
        """

        async def _run_with_uninstantiated_requests(
            execution_requests: Sequence[ExecutionRequest],
        ) -> None:
            if not self._uninstantiated_execution_requests:
                await self._run_cells(
                    self.mutate_graph(execution_requests, deletion_requests=[])
                )
                return

            execution_requests_cell_ids = {
                er.cell_id for er in execution_requests
            }
            graph = dataflow.DirectedGraph()
            for cid, er in list(
                self._uninstantiated_execution_requests.items()
            ):
                if cid in execution_requests_cell_ids:
                    # Running a previously uninstantiated cell; just remove
                    # it from our cache of uninstantiated execution requests.
                    del self._uninstantiated_execution_requests[cid]
                    continue

                try:
                    cell = compile_cell(er.code, cell_id=er.cell_id)
                except Exception:
                    # The cell was not parsable.
                    continue
                graph.register_cell(cell_id=cid, cell=cell)

            # Collect uninstantiated ancestors
            ancestors: set[CellId_t] = set()
            for er in execution_requests:
                try:
                    cell = compile_cell(er.code, cell_id=er.cell_id)
                except Exception:
                    continue
                graph.register_cell(cell_id=er.cell_id, cell=cell)
                ancestors |= graph.ancestors(er.cell_id)

            # We run all uninstantiated ancestors of the requested cells
            previously_uninstantiated_requests: list[ExecutionRequest] = []
            for ancestor_cid in ancestors:
                if ancestor_cid in self._uninstantiated_execution_requests:
                    previously_uninstantiated_requests.append(
                        self._uninstantiated_execution_requests[ancestor_cid]
                    )
                    del self._uninstantiated_execution_requests[ancestor_cid]

            await self._run_cells(
                self.mutate_graph(
                    list(execution_requests)
                    + previously_uninstantiated_requests,
                    deletion_requests=[],
                )
            )

        if self.last_interrupt_timestamp is None:
            # No interruption has occurred, so we can run all requests
            await _run_with_uninstantiated_requests(execution_requests)
            return

        # Filter out requests that were created before the last interruption
        filtered_requests: list[ExecutionRequest] = []
        for request in execution_requests:
            if request.timestamp < self.last_interrupt_timestamp:
                CellOp.broadcast_error(
                    data=[MarimoInterruptionError()],
                    clear_console=False,
                    cell_id=request.cell_id,
                )

                # TODO(akshayka): This hack is needed because the FE
                # sets status to queued when a cell is manually run; remove
                # once setting status to queued on receipt of request is moved
                # to backend (which will require moving execution requests to
                # a dedicated multiprocessing queue and processing execution
                # requests in a background thread).
                CellOp.broadcast_status(
                    cell_id=request.cell_id,
                    status="idle",
                )

            else:
                filtered_requests.append(request)

        await _run_with_uninstantiated_requests(filtered_requests)

    @kernel_tracer.start_as_current_span("rename_file")
    async def rename_file(self, filename: str) -> None:
        self.globals["__file__"] = filename
        self.app_metadata.filename = filename
        roots = set()
        for cell in self.graph.cells.values():
            if "__file__" in cell.refs:
                roots.add(cell.cell_id)
        await self._if_autorun_then_run_cells(roots)

    @kernel_tracer.start_as_current_span("run_scratchpad")
    async def run_scratchpad(self, code: str) -> None:
        roots = {SCRATCH_CELL_ID}

        # If cannot compile, don't run
        cell, error = self._try_compiling_cell(SCRATCH_CELL_ID, code, [])
        if error:
            CellOp.broadcast_error(
                data=[error],
                clear_console=True,
                cell_id=SCRATCH_CELL_ID,
            )
            return
        elif not cell:
            return
        cell.defs.clear()  # remove definitions
        cell.refs.clear()  # remove references

        # Create graph of just the scratchpad cell
        graph = dataflow.DirectedGraph()
        graph.register_cell(SCRATCH_CELL_ID, cell)

        runner = cell_runner.Runner(
            roots=roots,
            graph=graph,
            glbls=copy(self.globals),
            excluded_cells=set(),
            debugger=self.debugger,
            execution_mode=self.reactive_execution_mode,
            execution_type=self.execution_type,
            execution_context=self._install_execution_context,
            preparation_hooks=self._preparation_hooks,
            pre_execution_hooks=self._pre_execution_hooks,
            post_execution_hooks=self._post_execution_hooks,
            on_finish_hooks=(
                self._on_finish_hooks + [self._broadcast_missing_packages]
            ),
        )

        await runner.run_all()

    @kernel_tracer.start_as_current_span("run_stale_cells")
    async def run_stale_cells(self) -> None:
        cells_to_run: set[CellId_t] = set()
        if self._uninstantiated_execution_requests:
            cells_to_run |= set(self._uninstantiated_execution_requests.keys())
            self.mutate_graph(
                list(self._uninstantiated_execution_requests.values()),
                deletion_requests=[],
            )
            self._uninstantiated_execution_requests = {}

        for cid, cell_impl in self.graph.cells.items():
            if cell_impl.stale and not self.graph.is_disabled(cid):
                cells_to_run.add(cid)

        await self._run_cells(
            dataflow.transitive_closure(
                self.graph,
                cells_to_run,
                relatives=dataflow.import_block_relatives,
            )
        )
        if self.module_watcher is not None:
            self.module_watcher.run_is_processed.set()

    @kernel_tracer.start_as_current_span("set_cell_config")
    async def set_cell_config(self, request: SetCellConfigRequest) -> None:
        """Update cell configs.

        Cells that are enabled (via config) but stale are run as a side-effect.
        """
        # Stale cells that are enabled will need to be run.
        stale_cells: set[CellId_t] = set()
        for cell_id, config in request.configs.items():
            # store the config, regardless of whether we've seen the cell yet
            self.cell_metadata[cell_id] = CellMetadata(
                config=CellConfig.from_dict(config)
            )
            cell = self.graph.cells.get(cell_id)
            if cell is None:
                continue
            cell.configure(config)
            if not cell.config.disabled:
                stale_cells = self.graph.enable_cell(cell_id)
            elif cell.config.disabled:
                self.graph.disable_cell(cell_id)

        if stale_cells and self.reactive_execution_mode == "autorun":
            await self._run_cells(stale_cells)

    @kernel_tracer.start_as_current_span("set_user_config")
    def set_user_config(self, request: SetUserConfigRequest) -> None:
        self._update_runtime_from_user_config(request.config)

    @kernel_tracer.start_as_current_span("set_ui_element_value")
    async def set_ui_element_value(
        self, request: SetUIElementValueRequest
    ) -> bool:
        """Set the value of a UI element bound to a global variable.

        Runs cells that reference the UI element by name.

        Returns True if any ui elements were set, False otherwise
        """
        updated_components: list[UIElement[Any, Any]] = []

        # Resolve lenses on request, if any: any element that is a view
        # of another parent element is resolved to its parent. In particular,
        # interacting with a view triggers reactive execution through the
        # source (parent).
        ctx = get_context()
        resolved_requests: dict[UIElementId, Any] = {}
        referring_cells: set[CellId_t] = set()
        ui_element_registry = ctx.ui_element_registry
        for object_id, value in request.ids_and_values:
            try:
                resolved_id, resolved_value = ui_element_registry.resolve_lens(
                    object_id, value
                )
            except (KeyError, RuntimeError):
                # Attempt to set the UI element in a child context (app).
                for child_context in ctx.children:
                    if (
                        child_context.app is not None
                        and await child_context.app.set_ui_element_value(
                            SetUIElementValueRequest(
                                object_ids=[object_id],
                                values=[value],
                                request=request.request,
                            )
                        )
                    ):
                        bindings = [
                            name
                            for name, value in self.globals.items()
                            # child_context.app is an InternalApp object
                            if value is child_context.app._app
                        ]
                        for binding in bindings:
                            referring_cells.update(
                                self.graph.get_referring_cells(
                                    binding, language="python"
                                )
                            )

                # KeyError: Trying to access an unnamed UIElement
                # RuntimeError: UIElement was deleted somehow
                LOGGER.debug(
                    "Could not resolve UIElement with id%s", object_id
                )
                continue
            resolved_requests[resolved_id] = resolved_value
        del request

        for object_id, value in resolved_requests.items():
            try:
                component = ui_element_registry.get_object(object_id)
                LOGGER.debug(
                    "Setting value on UIElement with id %s, value %s",
                    object_id,
                    value,
                )
            except KeyError:
                # KeyError: A UI element may go out of scope if it was not
                # assigned to a global variable
                LOGGER.debug("Could not find UIElement with id %s", object_id)
                continue

            with self._install_execution_context(
                ui_element_registry.get_cell(object_id),
                setting_element_value=True,
            ):
                try:
                    component._update(value)
                except MarimoConvertValueException:
                    # Internal marimo error
                    sys.stderr.write(
                        "An exception was raised when updating a UIElement's "
                        "value. This is a bug in marimo. Please copy "
                        "the below traceback and paste it in an "
                        "issue: https://github.com/marimo-team/marimo/issues\n"
                    )
                    tmpio = io.StringIO()
                    traceback.print_exc(file=tmpio)
                    tmpio.seek(0)
                    write_traceback(tmpio.read())
                    # Don't run referring elements of this UI element
                    continue
                except Exception:
                    # User's on_change handler an exception ...
                    sys.stderr.write(
                        "An exception was raised by a "
                        "UIElement's on_change handler:\n"
                    )

                    tmpio = io.StringIO()
                    traceback.print_exc(file=tmpio)
                    tmpio.seek(0)
                    write_traceback(tmpio.read())
                else:
                    updated_components.append(component)

            bound_names = (
                name
                for name in ctx.ui_element_registry.bound_names(object_id)
                if not is_local(name)
            )

            variable_values: list[VariableValue] = []
            for name in bound_names:
                # TODO update variable values even for namespaces? lenses? etc
                variable_values.append(
                    VariableValue(name=name, value=component)
                )
                try:
                    # subtracting self.graph.definitions[name]: never rerun the
                    # cell that created the name
                    referring_cells.update(
                        self.graph.get_referring_cells(name, language="python")
                        - self.graph.get_defining_cells(name)
                    )
                except Exception:
                    # Internal marimo error
                    sys.stderr.write(
                        "An exception was raised when finding cells that "
                        f"refer to a UIElement value, for bound name {name}. "
                        "This is a bug in marimo. "
                        "Please copy the below traceback and paste it in an "
                        "issue: https://github.com/marimo-team/marimo/issues\n"
                    )
                    tmpio = io.StringIO()
                    traceback.print_exc(file=tmpio)
                    tmpio.seek(0)
                    write_traceback(tmpio.read())
                    # Entering undefined behavior territory ...
                    continue

            if variable_values:
                VariableValues(variables=variable_values).broadcast()

        if self.reactive_execution_mode == "autorun":
            await self._run_cells(referring_cells)
        else:
            # Any cells referring to a UI element cannot be import cells,
            # so not necessary to specify `prune_imports`.
            self.graph.set_stale(referring_cells)
            # Process any state updates that may have been queued by the
            # on_change handlers.
            await self._run_cells(set())

        for component in updated_components:
            try:
                component._on_update_completion()
            except Exception:
                # Internal marimo error
                sys.stderr.write(
                    "An exception was raised when completing a UIElement's"
                    "update. This is a bug in marimo. "
                    "Please copy the below traceback and paste it in an "
                    "issue: https://github.com/marimo-team/marimo/issues\n"
                )
                tmpio = io.StringIO()
                traceback.print_exc(file=tmpio)
                tmpio.seek(0)
                write_traceback(tmpio.read())

        return bool(updated_components) or bool(referring_cells)

    def get_ui_initial_value(self, object_id: str) -> Any:
        """Get an initial value for a UIElement, if any.

        Initial values are optionally populated during instantiation.

        Args:
            object_id (str): ID of UIElement.

        Returns:
            Any: Initial value of UI element, if any.

        Raises:
            KeyError: If object_id not found.
        """
        return self.ui_initializers[object_id]

    def reset_ui_initializers(self) -> None:
        self.ui_initializers = {}

    @kernel_tracer.start_as_current_span("function_call_request")
    async def function_call_request(
        self, request: FunctionCallRequest
    ) -> tuple[HumanReadableStatus, JSONType, bool]:
        """Execute a function call.

        If the function is not found, children contexts are also searched.

        Args:
            request (FunctionCallRequest): The function call request.

        Returns:
            tuple[HumanReadableStatus, JSONType, bool]: A tuple containing:
                - status: Human readable status of the function call
                - payload: The function return value
                - found: True if the function was found, False otherwise
        """
        ctx = get_context()
        function = ctx.function_registry.get_function(
            request.namespace, request.function_name
        )
        error_title, error_message = "", ""

        def debug(title: str, message: str) -> None:
            LOGGER.debug("%s: %s", title, message)

        if function is None:
            for child_context in ctx.children:
                if child_context.app is not None:
                    (
                        status,
                        response,
                        found,
                    ) = await child_context.app.function_call(request)
                    if found:
                        return status, response, found
            found = False
            error_title = "Function not found"
            error_message = (
                "Could not find function given request: %s" % request
            )
            debug(error_title, error_message)
        elif function.cell_id is None:
            found = True
            error_title = "Function not associated with cell"
            error_message = (
                "Attempted to call a function without a cell id: %s" % request
            )
            debug(error_title, error_message)
        else:
            found = True
            LOGGER.debug("Executing RPC %s", request)
            with (
                self._install_execution_context(cell_id=function.cell_id),
                ctx.provide_ui_ids(str(uuid4())),
            ):
                # Usually UI element IDs are deterministic, based on
                # cell id, so that element values can be matched up
                # with objects on notebook/app re-connection.
                #
                # We're using a non-deterministic ID prefix so that
                # UI elements created in an RPC shouldn't evict UI
                # elements associated with its owning cell. But that
                # means we won't be able to restore their values
                # on reconnection.
                #
                # TODO(akshayka): Do UI elements created in function calls
                # get cleared from the FE registry? This could be a leak.
                try:
                    response = cast(JSONType, function(request.args))
                    if asyncio.iscoroutine(response):
                        response = await response
                        return HumanReadableStatus(code="ok"), response, found
                    return (
                        HumanReadableStatus(code="ok"),
                        response,
                        found,
                    )
                except MarimoInterrupt:
                    error_title = "Interrupted"
                    error_message = (
                        "Function call (%s) was interrupted by the user"
                        % request.function_name
                    )
                    debug(error_title, error_message)
                except Exception as e:
                    error_title = "Exception"
                    error_message = (
                        "Function call (name: %s, args: %s) failed with exception %s"  # noqa: E501
                        % (request.function_name, request.args, str(e))
                    )
                    debug(error_title, error_message)

        # Couldn't call function, or function call failed
        return (
            HumanReadableStatus(
                code="error", title=error_title, message=error_message
            ),
            None,
            found,
        )

    @kernel_tracer.start_as_current_span("instantiate")
    async def instantiate(self, request: CreationRequest) -> None:
        """Instantiate the kernel with cells and UIElement initial values

        During instantiation, UIElements can check for an initial value
        with `get_initial_value`
        """
        if self.graph.cells:
            del request
            LOGGER.debug("App already instantiated.")
        elif request.auto_run:
            self.reset_ui_initializers()
            for (
                object_id,
                initial_value,
            ) in request.set_ui_element_value_request.ids_and_values:
                self.ui_initializers[object_id] = initial_value
            await self.run(request.execution_requests)
            self.reset_ui_initializers()
        else:
            self._uninstantiated_execution_requests = {
                er.cell_id: er for er in request.execution_requests
            }
            for cid in self._uninstantiated_execution_requests:
                CellOp.broadcast_stale(cell_id=cid, stale=True)

    async def install_missing_packages(
        self, request: InstallMissingPackagesRequest
    ) -> None:
        """Attempts to install packages for modules that cannot be imported

        Runs cells affected by successful installation.
        """
        assert self.package_manager is not None
        if request.manager != self.package_manager.name:
            # Swap out the package manager
            self.package_manager = create_package_manager(request.manager)

        if not self.package_manager.is_manager_installed():
            self.package_manager.alert_not_installed()
            return

        missing_packages_set = set(request.versions.keys())
        # Append all other missing packages from the notebook; the missing
        # package request only contains the packages from the cell the user
        # executed.
        missing_packages_set.update(
            [
                self.package_manager.module_to_package(module)
                for module in self.module_registry.missing_modules()
            ]
        )
        missing_packages = list(sorted(missing_packages_set))

        # Frontend shows package names, not module names
        package_statuses: PackageStatusType = {
            pkg: "queued" for pkg in missing_packages
        }
        InstallingPackageAlert(packages=package_statuses).broadcast()

        for pkg in missing_packages:
            if self.package_manager.attempted_to_install(package=pkg):
                # Already attempted an installation; it must have failed.
                # Skip the installation.
                continue
            package_statuses[pkg] = "installing"
            InstallingPackageAlert(packages=package_statuses).broadcast()
            version = request.versions.get(pkg)
            if await self.package_manager.install(pkg, version=version):
                package_statuses[pkg] = "installed"
                InstallingPackageAlert(packages=package_statuses).broadcast()
            else:
                package_statuses[pkg] = "failed"
                mod = self.package_manager.package_to_module(pkg)
                self.module_registry.excluded_modules.add(mod)
                InstallingPackageAlert(packages=package_statuses).broadcast()

        installed_modules = [
            self.package_manager.package_to_module(pkg)
            for pkg in package_statuses
            if package_statuses[pkg] == "installed"
        ]

        # If a package was not installed at cell registration time, it won't
        # yet be in the script metadata.
        if self._should_update_script_metadata():
            self._update_script_metadata(installed_modules)

        cells_to_run = set(
            cid
            for module in installed_modules
            if (cid := self.module_registry.defining_cell(module)) is not None
        )
        if cells_to_run:
            await self._if_autorun_then_run_cells(cells_to_run)

    def _should_update_script_metadata(self) -> bool:
        return (
            GLOBAL_SETTINGS.MANAGE_SCRIPT_METADATA is True
            and self.app_metadata.filename is not None
            and self.package_manager is not None
        )

    def _update_script_metadata(
        self, import_namespaces_to_add: List[str]
    ) -> None:
        filename = self.app_metadata.filename

        if not filename or not self.package_manager:
            return

        try:
            LOGGER.debug(
                "Updating script metadata: %s. Adding namespaces: %s.",
                filename,
                import_namespaces_to_add,
            )
            self.package_manager.update_notebook_script_metadata(
                filepath=filename,
                import_namespaces_to_add=import_namespaces_to_add,
            )
        except Exception as e:
            LOGGER.error("Failed to add script metadata to notebook: %s", e)

    @kernel_tracer.start_as_current_span("preview_dataset_column")
    async def preview_dataset_column(
        self, request: PreviewDatasetColumnRequest
    ) -> None:
        """Preview a column of a dataset.

        The dataset is loaded, and the column is displayed in the frontend.

        Args:
            request (PreviewDatasetColumnRequest): The preview request containing:
                - table_name: Name of the table
                - column_name: Name of the column
                - source_type: Type of data source ("duckdb" or "local")
        """
        table_name = request.table_name
        column_name = request.column_name
        source_type = request.source_type

        try:
            if source_type == "duckdb":
                column_preview = get_column_preview_for_sql(
                    table_name=table_name,
                    column_name=column_name,
                )
            elif source_type == "local":
                dataset = self.globals[table_name]
                column_preview = get_column_preview_dataframe(dataset, request)
            elif source_type == "connection":
                DataColumnPreview(
                    error="Column preview for connection data sources is not supported",
                    column_name=column_name,
                    table_name=table_name,
                ).broadcast()
                return
            else:
                assert_never(source_type)

            if column_preview is None:
                DataColumnPreview(
                    error=f"Column {column_name} not found",
                    column_name=column_name,
                    table_name=table_name,
                ).broadcast()
            else:
                column_preview.broadcast()
        except Exception as e:
            LOGGER.warning(
                "Failed to get preview for column %s in table %s",
                column_name,
                table_name,
                exc_info=e,
            )
            DataColumnPreview(
                error=str(e),
                column_name=column_name,
                table_name=table_name,
            ).broadcast()
        return

    @kernel_tracer.start_as_current_span("preview_sql_table")
    async def preview_sql_table(self, request: PreviewSQLTableRequest) -> None:
        """Get table details for an SQL table.

        Args:
            request (PreviewSQLTableRequest): The request containing:
                - engine: Name of the SQL engine / connection
                - database: Name of the database
                - schema: Name of the schema
                - table_name: Name of the table
        """
        engine_name = cast(VariableName, request.engine)
        _database_name = request.database
        schema_name = request.schema
        table_name = request.table_name

        # TODO: Can we find the existing engine
        engine_val = self.globals.get(engine_name)
        try:
            engines = get_engines_from_variables([(engine_name, engine_val)])
            if engines is None or len(engines) == 0:
                LOGGER.warning("Engine %s not found", engine_name)
                SQLTablePreview(
                    request_id=request.request_id,
                    table=None,
                    error="Engine not found",
                ).broadcast()
            engine = engines[0][1]
        except Exception as e:
            LOGGER.warning("Failed to get engine %s", engine_name, exc_info=e)
            SQLTablePreview(
                request_id=request.request_id, table=None, error=str(e)
            ).broadcast()
            return

        if isinstance(engine, SQLAlchemyEngine):
            table = engine.get_table_details(table_name, schema_name)
            if table is None:
                LOGGER.warning("Table %s not found", table_name)
            SQLTablePreview(
                request_id=request.request_id, table=table
            ).broadcast()
        else:
            SQLTablePreview(
                request_id=request.request_id,
                table=None,
                error="Not an SQLAlchemyEngine",
            ).broadcast()

    async def handle_message(self, request: ControlRequest) -> None:
        """Handle a message from the client.

        The message is dispatched to the appropriate method based on its type.

        Coarsely locks globals to avoid race conditions with code completion.
        """
        # acquiring and releasing an RLock takes ~100ns; the overhead is
        # negligible because the lock is coarse.
        LOGGER.debug("Acquiring globals lock to handle request %s", request)
        with self.lock_globals():
            LOGGER.debug("Handling control request: %s", request)
            if isinstance(request, CreationRequest):
                with http_request_context(request.request):
                    await self.instantiate(request)
                CompletedRun().broadcast()
            elif isinstance(request, ExecuteMultipleRequest):
                with http_request_context(request.request):
                    await self.run(request.execution_requests)
                CompletedRun().broadcast()
            elif isinstance(request, ExecuteScratchpadRequest):
                with http_request_context(request.request):
                    await self.run_scratchpad(request.code)
            elif isinstance(request, ExecuteStaleRequest):
                await self.run_stale_cells()
            elif isinstance(request, RenameRequest):
                await self.rename_file(request.filename)
            elif isinstance(request, SetCellConfigRequest):
                await self.set_cell_config(request)
            elif isinstance(request, SetUserConfigRequest):
                self.set_user_config(request)
            elif isinstance(request, SetUIElementValueRequest):
                with http_request_context(request.request):
                    await self.set_ui_element_value(request)
                CompletedRun().broadcast()
            elif isinstance(request, FunctionCallRequest):
                status, ret, _ = await self.function_call_request(request)
                LOGGER.debug("Function returned with status %s", status)
                FunctionCallResult(
                    function_call_id=request.function_call_id,
                    return_value=ret,
                    status=status,
                ).broadcast()
                CompletedRun().broadcast()
            elif isinstance(request, DeleteCellRequest):
                await self.delete_cell(request)
            elif isinstance(request, InstallMissingPackagesRequest):
                await self.install_missing_packages(request)
                CompletedRun().broadcast()
            elif isinstance(request, PreviewDatasetColumnRequest):
                await self.preview_dataset_column(request)
            elif isinstance(request, PreviewSQLTableRequest):
                await self.preview_sql_table(request)
            elif isinstance(request, StopRequest):
                return None
            else:
                raise ValueError(f"Unknown request {request}")
            LOGGER.debug("Handled control request: %s", request)


def launch_kernel(
    control_queue: QueueType[ControlRequest],
    set_ui_element_queue: QueueType[SetUIElementValueRequest],
    completion_queue: QueueType[CodeCompletionRequest],
    input_queue: QueueType[str],
    stream_queue: queue.Queue[KernelMessage] | None,
    socket_addr: tuple[str, int] | None,
    is_edit_mode: bool,
    configs: dict[CellId_t, CellConfig],
    app_metadata: AppMetadata,
    user_config: MarimoConfig,
    virtual_files_supported: bool,
    redirect_console_to_browser: bool,
    interrupt_queue: QueueType[bool] | None = None,
    profile_path: Optional[str] = None,
    log_level: int | None = None,
) -> None:
    if log_level is not None:
        _loggers.set_level(log_level)
    LOGGER.debug("Launching kernel")
    if is_edit_mode:
        restore_signals()

    profiler = None
    if profile_path is not None:
        import cProfile

        profiler = cProfile.Profile()
        profiler.enable()

    should_redirect_stdio = is_edit_mode or redirect_console_to_browser

    # Create communication channels
    pipe: Optional[TypedConnection[KernelMessage]] = None
    if socket_addr is not None:
        n_tries = 0
        while n_tries < 100:
            try:
                pipe = TypedConnection[KernelMessage].of(
                    connection.Client(socket_addr)
                )
                break
            except Exception:
                n_tries += 1
                time.sleep(0.01)

        if n_tries == 100 or pipe is None:
            LOGGER.debug("Failed to connect to socket.")
            return

        stream = ThreadSafeStream(
            pipe=pipe,
            input_queue=input_queue,
            redirect_console=should_redirect_stdio,
        )
    elif stream_queue is not None:
        stream = ThreadSafeStream(
            pipe=QueuePipe(stream_queue),
            input_queue=input_queue,
            redirect_console=should_redirect_stdio,
        )
    else:
        raise RuntimeError(
            "One of queue_pipe and socket_addr must be non None"
        )

    # Console output is hidden in run mode, so no need to redirect
    # (redirection of console outputs is not thread-safe anyway)
    stdout = ThreadSafeStdout(stream) if should_redirect_stdio else None
    stderr = ThreadSafeStderr(stream) if should_redirect_stdio else None
    # TODO(akshayka): stdin in run mode? input(prompt) uses stdout, which
    # isn't currently available in run mode.
    stdin = ThreadSafeStdin(stream) if is_edit_mode else None
    debugger = (
        marimo_pdb.MarimoPdb(stdout=stdout, stdin=stdin)
        if is_edit_mode
        else None
    )

    # In run mode, the kernel should always be in autorun, and the module
    # autoreloader is disabled
    if not is_edit_mode:
        user_config = user_config.copy()
        user_config["runtime"]["on_cell_change"] = "autorun"
        user_config["runtime"]["auto_reload"] = "off"

    def _enqueue_control_request(req: ControlRequest) -> None:
        control_queue.put_nowait(req)
        if isinstance(req, SetUIElementValueRequest):
            set_ui_element_queue.put_nowait(req)

    kernel = Kernel(
        cell_configs=configs,
        app_metadata=app_metadata,
        stream=stream,
        stdout=stdout,
        stderr=stderr,
        stdin=stdin,
        module=patches.patch_main_module(
            file=app_metadata.filename,
            input_override=input_override,
            print_override=print_override,
        ),
        debugger_override=debugger,
        user_config=user_config,
        enqueue_control_request=_enqueue_control_request,
    )
    ctx = initialize_kernel_context(
        kernel=kernel,
        stream=stream,
        stdout=stdout,
        stderr=stderr,
        virtual_files_supported=virtual_files_supported,
        mode=SessionMode.EDIT if is_edit_mode else SessionMode.RUN,
    )

    if is_edit_mode:
        # completions only provided in edit mode
        kernel.start_completion_worker(completion_queue)

        # In edit mode, kernel runs in its own process so it's interruptible.
        from marimo._output.formatters.formatters import register_formatters

        # TODO: Windows workaround -- find a way to make the process
        # its group leader
        if sys.platform != "win32":
            # Make this process group leader to prevent it from receiving
            # signals intended for the parent (server) process,
            # Ctrl+C in particular.
            os.setsid()

        # kernels are processes in edit mode, and each process needs to
        # install the formatter import hooks
        register_formatters(theme=user_config["display"]["theme"])

        signal.signal(signal.SIGINT, handlers.construct_interrupt_handler(ctx))

        if sys.platform == "win32":
            if interrupt_queue is not None:
                Win32InterruptHandler(interrupt_queue).start()
            # windows doesn't handle SIGTERM
            signal.signal(
                signal.SIGBREAK, handlers.construct_sigterm_handler(kernel)
            )
        else:
            signal.signal(
                signal.SIGTERM, handlers.construct_sigterm_handler(kernel)
            )

    ui_element_request_mgr = SetUIElementRequestManager(set_ui_element_queue)

    async def control_loop(kernel: Kernel) -> None:
        while True:
            try:
                request: ControlRequest | None = control_queue.get()
            except Exception as e:
                # triggered on Windows when quit with Ctrl+C
                LOGGER.debug("kernel queue.get() failed %s", e)
                break
            LOGGER.debug("Received control request: %s", request)
            if isinstance(request, StopRequest):
                break
            elif isinstance(request, SetUIElementValueRequest):
                request = ui_element_request_mgr.process_request(request)

            if request is not None:
                await kernel.handle_message(request)

    # The control loop is asynchronous only because we allow user code to use
    # top-level await; nothing else is awaited. Don't introduce async
    # primitives anywhere else in the runtime unless there is a *very* good
    # reason; prefer using threads (for performance and clarity).
    asyncio.run(control_loop(kernel))

    if profiler is not None and profile_path is not None:
        profiler.disable()
        profiler.dump_stats(profile_path)

    # Defensively clear context data structures, in case a leak prevents
    # the context from being destroyed.
    #
    # TODO(akshayka): define ownership semantics for contexts, so the
    # context knows how to shut itself down. The virtual file registry
    # is shared between the main thread and mo.Thread's right now ...
    get_context().virtual_file_registry.shutdown()
    get_context().app_kernel_runner_registry.shutdown()
    teardown_context()
    kernel.teardown()
    if isinstance(pipe, connection.Connection):
        pipe.close()
