# Copyright 2024 Marimo. All rights reserved.
from __future__ import annotations

import sys
import textwrap
from typing import TYPE_CHECKING, Sequence

import pytest

from marimo._config.config import DEFAULT_CONFIG
from marimo._messaging.errors import (
    CycleError,
    DeleteNonlocalError,
    Error,
    MarimoStrictExecutionError,
    MultipleDefinitionError,
)
from marimo._messaging.ops import CellOp
from marimo._messaging.types import NoopStream
from marimo._plugins.ui._core.ids import IDProvider
from marimo._plugins.ui._core.ui_element import UIElement
from marimo._runtime.dataflow import EdgeWithVar
from marimo._runtime.patches import create_main_module
from marimo._runtime.requests import (
    AppMetadata,
    CreationRequest,
    DeleteCellRequest,
    ExecutionRequest,
    SetCellConfigRequest,
    SetUIElementValueRequest,
)
from marimo._runtime.runtime import Kernel
from marimo._runtime.scratch import SCRATCH_CELL_ID
from marimo._utils.parse_dataclass import parse_raw
from tests.conftest import ExecReqProvider, MockedKernel

if TYPE_CHECKING:
    import pathlib


def _check_edges(error: Error, expected_edges: Sequence[EdgeWithVar]) -> None:
    assert isinstance(error, CycleError)
    assert len(error.edges_with_vars) == len(expected_edges)
    for edge in expected_edges:
        assert edge in error.edges_with_vars


class TestExecution:
    async def test_expected_gloals(self, any_kernel: Kernel):
        k = any_kernel
        await k.run(
            [
                ExecutionRequest(
                    cell_id="0", code="assert __file__; success = 1"
                )
            ]
        )
        expected_globals = {
            "__builtin__",
            "__doc__",
            "__file__",
            "__marimo__",
            "__name__",
            "__package__",
            "__loader__",
            "__spec__",
        }
        assert not (expected_globals - set(k.globals.keys()))
        assert k.globals["success"] == 1

    async def test_triangle(self, any_kernel: Kernel) -> None:
        k = any_kernel
        # x
        # x --> y
        # x, y --> z
        await k.run(
            [
                ExecutionRequest(cell_id="0", code="x = 1"),
                er1 := ExecutionRequest(cell_id="1", code="y = x + 1"),
                er2 := ExecutionRequest(cell_id="2", code="z = x + y"),
            ]
        )

        assert not k.errors
        assert k.globals["x"] == 1
        assert k.globals["y"] == 2
        assert k.globals["z"] == 3

        await k.run([ExecutionRequest(cell_id="0", code="x = 2")])
        assert not k.graph.cells["0"].stale
        assert k.globals["x"] == 2
        if k.lazy():
            assert k.graph.cells["1"].stale
            assert k.graph.cells["2"].stale
            await k.run([er1])

        assert not k.graph.cells["0"].stale
        assert not k.graph.cells["1"].stale
        assert k.globals["x"] == 2
        assert k.globals["y"] == 3
        if k.lazy():
            assert k.graph.cells["2"].stale
            await k.run([er2])
        assert k.globals["z"] == 5
        assert not k.graph.cells["0"].stale
        assert not k.graph.cells["1"].stale
        assert not k.graph.cells["2"].stale

        await k.run([ExecutionRequest(cell_id="1", code="y = 0")])
        assert not k.graph.cells["0"].stale
        assert not k.graph.cells["1"].stale
        assert k.globals["x"] == 2
        assert k.globals["y"] == 0
        if k.lazy():
            assert k.graph.cells["2"].stale
            await k.run([er2])
        assert k.globals["z"] == 2

        await k.delete(DeleteCellRequest(cell_id="1"))
        assert k.globals["x"] == 2
        assert "y" not in k.globals
        if k.lazy():
            assert k.graph.cells["2"].stale
            await k.run([er2])
        assert "z" not in k.globals

        await k.delete(DeleteCellRequest(cell_id="0"))
        assert "x" not in k.globals
        assert "y" not in k.globals
        assert "z" not in k.globals

    async def test_run_referrers_not_stale(self, any_kernel: Kernel) -> None:
        k = any_kernel
        graph = k.graph

        # Tests that running cells doesn't spuriously mark other cells
        # as stale
        await k.run(
            [
                ExecutionRequest(cell_id="0", code="x = 1"),
                er1 := ExecutionRequest(cell_id="1", code="x"),
                er2 := ExecutionRequest(cell_id="2", code="x"),
            ]
        )
        assert not graph.get_stale()

        await k.run([er1])
        assert not graph.get_stale()

        await k.run([er2])
        assert not graph.get_stale()

    async def test_set_ui_element_value(self, any_kernel: Kernel) -> None:
        k = any_kernel
        await k.run(
            [ExecutionRequest(cell_id="0", code="import marimo as mo")]
        )
        await k.run(
            [
                ExecutionRequest(
                    cell_id="1", code="s = mo.ui.slider(0, 10, value=1); s"
                )
            ]
        )
        await k.run(
            [er2 := ExecutionRequest(cell_id="2", code="x = s.value + 1")]
        )
        assert k.globals["x"] == 2

        element_id = k.globals["s"]._id
        await k.set_ui_element_value(
            SetUIElementValueRequest.from_ids_and_values([(element_id, 5)])
        )
        assert k.globals["s"].value == 5

        if k.reactive_execution_mode == "lazy":
            assert k.graph.cells["2"].stale
            await k.run([er2])

        assert k.globals["x"] == 6
        assert not k.graph.cells["2"].stale

    async def test_set_ui_element_value_lensed(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        """Test setting the value of a lensed element.

        Make sure reactivity flows through its parent, and that its on_change
        handler is called exactly once.
        """
        k = any_kernel
        await k.run([exec_req.get(code="import marimo as mo")])

        # Create an array and output it ...
        cell_one_code = """
        data = []
        def on_change(v):
            data.append(v)

        array = mo.ui.array(
            [mo.ui.slider(0, 10, value=1, on_change=on_change)]);
        array
        """
        await k.run([exec_req.get(code=cell_one_code)])
        assert not k.errors

        # Reference the array's value
        await k.run([er := exec_req.get(code="x = array.value[0] + 1")])
        assert not k.errors
        assert k.globals["x"] == 2

        # Set a child of the array to 5 ...
        child_id = k.globals["array"][0]._id
        await k.set_ui_element_value(
            SetUIElementValueRequest.from_ids_and_values([(child_id, 5)])
        )

        # Make sure the array and its child are updated
        assert k.globals["array"].value == [5]
        assert k.globals["array"][0].value == 5

        # Make sure the on_change handler got called exactly once
        assert k.globals["data"] == [5]

        if k.lazy():
            assert k.graph.cells[er.cell_id].stale
            await k.run([er])

        # Make sure setting array's child triggered execution of the second
        # cell, which references `array`
        assert k.globals["x"] == 6

    async def test_set_ui_element_value_lensed_bound_child(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        """Test setting the value of a lensed element.

        Make sure reactivity flows through its parent and also to names bound
        to children.
        """
        k = any_kernel
        await k.run([exec_req.get(code="import marimo as mo")])

        cell_one_code = """
        array = mo.ui.array([mo.ui.slider(0, 10, value=1)])
        child = array[0]
        """
        await k.run([exec_req.get(code=cell_one_code)])
        await k.run([er := exec_req.get(code="x = child.value + 1")])

        array_id = k.globals["array"]._id
        await k.set_ui_element_value(
            SetUIElementValueRequest.from_ids_and_values(
                [(array_id, {"0": 5})]
            )
        )
        assert k.globals["array"].value == [5]
        if k.lazy():
            assert k.graph.cells[er.cell_id].stale
            await k.run([er])
        assert k.globals["x"] == 6

    async def test_set_ui_element_value_lensed_with_state(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        """Test setting value of a lensed element with on_change set_state"""
        k = any_kernel
        await k.run([exec_req.get(code="import marimo as mo")])

        # Create an array and output it ...
        cell_one_code = """
        get_state, set_state = mo.state(None)

        array = mo.ui.array(
            [mo.ui.slider(0, 10, value=1, on_change=set_state),
            mo.ui.slider(0, 10, value=1, on_change=set_state)
        ]);
        """
        await k.run([exec_req.get(code=cell_one_code)])
        await k.run([er := exec_req.get(code="state = get_state()")])

        # Set a child of the array and make sure its on_change handler is
        # called
        child_id = k.globals["array"][0]._id
        await k.set_ui_element_value(
            SetUIElementValueRequest.from_ids_and_values([(child_id, 5)])
        )
        if k.lazy():
            assert k.graph.cells[er.cell_id].stale
            await k.run([er])

        # Make sure the array and its child are updated
        assert k.globals["state"] == 5

    async def test_set_local_var_ui_element_value(
        self, any_kernel: Kernel
    ) -> None:
        k = any_kernel
        await k.run([ExecutionRequest("0", "import marimo as mo")])
        await k.run(
            [ExecutionRequest("1", "_s = mo.ui.slider(0, 10, value=1); _s")]
        )
        # _s's name is mangled to _cell_1_s because it is local
        assert k.globals["_cell_1_s"].value == 1

        element_id = k.globals["_cell_1_s"]._id
        # This shouldn't crash the kernel, and s's value should still be
        # updated
        await k.set_ui_element_value(
            SetUIElementValueRequest.from_ids_and_values([(element_id, 5)])
        )
        assert k.globals["_cell_1_s"].value == 5

    async def test_creation_with_ui_element_value(
        self, any_kernel: Kernel
    ) -> None:
        k = any_kernel
        id_provider = IDProvider(prefix="1")
        await k.instantiate(
            CreationRequest(
                execution_requests=(
                    ExecutionRequest(cell_id="0", code="import marimo as mo"),
                    ExecutionRequest(
                        cell_id="1", code="s = mo.ui.slider(0, 10, value=1)"
                    ),
                ),
                set_ui_element_value_request=SetUIElementValueRequest.from_ids_and_values(
                    [(id_provider.take_id(), 2)]
                ),
            )
        )
        assert k.globals["s"].value == 2

    # Test errors in marimo semantics
    async def test_kernel_simultaneous_multiple_definition_error(
        self,
        any_kernel: Kernel,
    ) -> None:
        k = any_kernel
        await k.run(
            [
                ExecutionRequest(cell_id="0", code="x=0"),
                ExecutionRequest(cell_id="1", code="x=1"),
            ]
        )

        assert "x" not in k.globals
        assert set(k.errors.keys()) == {"0", "1"}
        assert k.errors["0"] == (MultipleDefinitionError("x", ("1",)),)
        assert k.errors["1"] == (MultipleDefinitionError("x", ("0",)),)

    async def test_kernel_new_multiple_definition_does_not_invalidate(
        self,
        any_kernel: Kernel,
    ) -> None:
        k = any_kernel
        await k.run([ExecutionRequest(cell_id="0", code="x=0")])
        assert k.globals["x"] == 0
        assert not k.errors

        # cell 0 should not be invalidated by the introduction of cell 1
        await k.run([ExecutionRequest(cell_id="1", code="x=0")])
        assert k.globals["x"] == 0
        assert set(k.errors.keys()) == {"1"}
        assert k.errors["1"] == (MultipleDefinitionError("x", ("0",)),)

        # re-running cell 0 should invalidate it
        await k.run([ExecutionRequest(cell_id="0", code="x=0")])
        assert "x" not in k.globals
        assert set(k.errors.keys()) == {"0", "1"}
        assert k.errors["0"] == (MultipleDefinitionError("x", ("1",)),)
        assert k.errors["1"] == (MultipleDefinitionError("x", ("0",)),)

    async def test_clear_multiple_definition_error(
        self, any_kernel: Kernel
    ) -> None:
        k = any_kernel
        await k.run(
            [
                er := ExecutionRequest(cell_id="0", code="x=0"),
                ExecutionRequest(cell_id="1", code="x=1"),
            ]
        )
        assert "x" not in k.globals
        assert set(k.errors.keys()) == {"0", "1"}
        assert k.errors["0"] == (MultipleDefinitionError("x", ("1",)),)
        assert k.errors["1"] == (MultipleDefinitionError("x", ("0",)),)

        # Rename second occurrence of x to y; should eliminate error and run
        # both cells
        await k.run([ExecutionRequest(cell_id="1", code="y=1")])
        assert k.globals["y"] == 1
        if k.lazy():
            assert k.graph.cells["0"].stale
            await k.run([er])
        assert not k.graph.cells["0"].stale
        assert k.globals["x"] == 0
        assert not k.errors

    async def test_clear_multiple_definition_error_with_delete(
        self, any_kernel: Kernel
    ) -> None:
        k = any_kernel
        await k.run(
            [
                er := ExecutionRequest(cell_id="0", code="x=0"),
                ExecutionRequest(cell_id="1", code="x=1"),
            ]
        )
        assert "x" not in k.globals
        assert set(k.errors.keys()) == {"0", "1"}
        assert k.errors["0"] == (MultipleDefinitionError("x", ("1",)),)
        assert k.errors["1"] == (MultipleDefinitionError("x", ("0",)),)

        # issue delete request for cell 1 to clear error and run cell 0
        await k.delete(DeleteCellRequest(cell_id="1"))
        if k.lazy():
            assert k.graph.cells[er.cell_id].stale
            await k.run([er])
        assert not k.graph.cells[er.cell_id].stale
        assert k.globals["x"] == 0
        assert not k.errors

    async def test_new_errors_update_old_ones(
        self, any_kernel: Kernel
    ) -> None:
        k = any_kernel
        await k.run([ExecutionRequest(cell_id="0", code="x=0")])
        await k.run([ExecutionRequest(cell_id="1", code="x, y = 1, 2")])
        assert set(k.errors.keys()) == {"1"}
        assert k.errors["1"] == (MultipleDefinitionError("x", ("0",)),)

        # errors propagated back to cell 1, even though we are not running it
        await k.run([ExecutionRequest(cell_id="2", code="x, y = 3, 4")])
        assert set(k.errors.keys()) == {"1", "2"}
        assert k.errors["1"] == (
            MultipleDefinitionError("x", ("0", "2")),
            MultipleDefinitionError("y", ("2",)),
        )
        assert k.errors["2"] == (
            MultipleDefinitionError("x", ("0", "1")),
            MultipleDefinitionError("y", ("1",)),
        )

        assert k.globals["x"] == 0

    async def test_cycle_error(self, any_kernel: Kernel) -> None:
        k = any_kernel
        await k.run(
            [
                ExecutionRequest(cell_id="0", code="x=y"),
                ExecutionRequest(cell_id="1", code="y=x"),
                er := ExecutionRequest(cell_id="2", code="z = x + 1"),
            ]
        )
        assert "x" not in k.globals
        assert "y" not in k.globals
        if k.execution_type == "strict":
            # 2 isn't valid in strict mode because it will refuse to execute
            # due to missing x.
            assert set(k.errors.keys()) == {"0", "1", "2"}
        else:
            assert set(k.errors.keys()) == {"0", "1"}
        assert len(k.errors["0"]) == 1
        assert len(k.errors["1"]) == 1
        if k.execution_type == "strict":
            assert len(k.errors["2"]) == 1
        _check_edges(k.errors["0"][0], [("0", ["x"], "1"), ("1", ["y"], "0")])
        _check_edges(k.errors["1"][0], [("0", ["x"], "1"), ("1", ["y"], "0")])

        # break cycle by modifying cell
        await k.run([ExecutionRequest(cell_id="1", code="y=1")])
        if k.lazy():
            assert k.graph.cells["0"].stale
            assert k.graph.cells["2"].stale
            await k.run([er])

        assert not k.graph.cells["0"].stale
        assert not k.graph.cells["2"].stale
        assert k.globals["x"] == 1
        assert k.globals["y"] == 1
        assert k.globals["z"] == 2
        assert not k.errors

    async def test_break_cycle_error_with_delete(
        self, any_kernel: Kernel
    ) -> None:
        k = any_kernel
        await k.run(
            [
                ExecutionRequest(cell_id="0", code="x=y"),
                ExecutionRequest(cell_id="1", code="y=x"),
            ]
        )
        assert "x" not in k.globals
        assert "y" not in k.globals
        assert set(k.errors.keys()) == {"0", "1"}
        assert len(k.errors["0"]) == 1
        assert len(k.errors["1"]) == 1
        _check_edges(k.errors["0"][0], [("0", ["x"], "1"), ("1", ["y"], "0")])

        # break cycle by deleting cell
        await k.delete(DeleteCellRequest(cell_id="1"))
        if k.execution_type == "strict":
            # Still invalid in strict mode because y is missing.
            assert set(k.errors.keys()) == {"0"}
            assert isinstance(k.errors["0"][0], MarimoStrictExecutionError)
        else:
            assert not k.errors

    async def test_delete_nonlocal_error(self, any_kernel: Kernel) -> None:
        k = any_kernel
        await k.run(
            [
                ExecutionRequest(cell_id="0", code="x=0"),
                ExecutionRequest(cell_id="1", code="del x; y = 1"),
                er := ExecutionRequest(cell_id="2", code="z = y + 1"),
            ]
        )
        assert "y" not in k.globals
        assert "z" not in k.globals
        if k.execution_type == "strict":
            assert set(k.errors.keys()) == {"1", "2"}
        else:
            assert set(k.errors.keys()) == {"1"}
        assert k.errors["1"] == (DeleteNonlocalError("x", ("0",)),)

        # fix cell 1, should run cell 1 and 2
        await k.run([ExecutionRequest(cell_id="1", code="y=1")])
        if k.lazy():
            assert k.graph.cells[er.cell_id].stale
            await k.run([er])

        assert not k.graph.cells[er.cell_id].stale
        assert k.globals["y"] == 1
        assert k.globals["z"] == 2
        assert not k.errors

    async def test_defs_with_no_definers_are_removed_from_cell(
        self, any_kernel: Kernel
    ) -> None:
        k = any_kernel
        await k.run(
            [
                ExecutionRequest(cell_id="0", code="x=0"),
                ExecutionRequest(cell_id="1", code="del x"),
            ]
        )
        assert set(k.errors.keys()) == {"1"}
        assert k.errors["1"] == (DeleteNonlocalError("x", ("0",)),)

        # Delete the cell that defines x. There shouldn't be any more errors
        # because x no longer exists.
        await k.delete(DeleteCellRequest(cell_id="0"))
        if k.execution_type != "strict":
            assert not k.errors

        # Add x back in.
        await k.run([ExecutionRequest(cell_id="2", code="x=0")])
        assert set(k.errors.keys()) == {"1"}
        assert k.errors["1"] == (DeleteNonlocalError("x", ("2",)),)

        # Repair graph
        await k.run([er := ExecutionRequest(cell_id="1", code="y = x + 1")])
        assert not k.errors
        assert k.globals["y"] == 1

        # Make sure graph is tracking x again and update propagates
        await k.run([ExecutionRequest(cell_id="2", code="x = 1")])
        assert not k.errors
        if k.lazy():
            assert k.graph.cells[er.cell_id].stale
            await k.run([er])
        assert not k.graph.cells[er.cell_id].stale
        assert k.globals["y"] == 2

    async def test_syntax_error(self, any_kernel: Kernel) -> None:
        k = any_kernel
        await k.run(
            [
                ExecutionRequest(cell_id="0", code="x=0"),
                er := ExecutionRequest(cell_id="1", code="x; y = 1"),
            ]
        )
        assert not k.graph.get_stale()
        assert k.globals["x"] == 0
        assert k.globals["y"] == 1
        assert not k.errors

        await k.run([ExecutionRequest(cell_id="0", code="x=")])
        assert "0" not in k.graph.cells
        assert "1" in k.graph.cells
        assert "x" not in k.globals
        if k.lazy():
            assert k.graph.get_stale() == set([er.cell_id])
            await k.run([er])
        assert not k.graph.get_stale()
        assert "y" not in k.globals

        # fix syntax error
        await k.run([ExecutionRequest(cell_id="0", code="x=0")])
        assert k.globals["x"] == 0
        assert "0" in k.graph.cells
        assert "1" in k.graph.cells
        assert not k.errors
        if k.lazy():
            assert k.graph.get_stale() == set([er.cell_id])
            await k.run([er])
        assert not k.graph.get_stale()
        assert k.globals["y"] == 1

    async def test_cell_state_invalidated(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        graph = k.graph
        await k.run(
            [
                er_1 := exec_req.get("x = 0"),
                er_2 := exec_req.get("x; y = 1"),
            ]
        )
        assert k.globals["y"] == 1
        assert not graph.get_stale()

        # "y" should not be computed, and its global state should have been
        # invalidated
        await k.run(
            [ExecutionRequest(er_1.cell_id, "x = 0; raise RuntimeError")]
        )
        if k.lazy():
            assert graph.get_stale() == set([er_2.cell_id])
            # running er_2 will redefine y; this is different from the
            # behavior of a non-lazy kernel, which doesn't run er_2
            # but instead invalidates it on exception raised
        else:
            assert not graph.get_stale()
            assert "y" not in k.globals

    async def test_set_ui_element_value_with_cell_run(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        graph = k.graph
        # This test imports a cell from another notebook that defines a UI
        # element It then sets a value on the UI element, and makes sure that
        # reactivity flows through the defs mapping that is returned
        await k.run(
            [
                exec_req.get(
                    "from runtime_data.cell_ui_element import make_slider"
                ),
                exec_req.get("import weakref"),
                exec_req.get("_, defs = make_slider.run()"),
                exec_req.get("_, second_defs = make_slider.run()"),
                exec_req.get(
                    """
                        class namespace:
                            ...
                        ns = namespace()
                        ns.count = 0
                        ref = weakref.ref(ns)
                        """
                ),
                er := exec_req.get("slider_value = defs['slider'].value + 1"),
                exec_req.get("second_defs; ref().count += 1"),
            ]
        )
        assert k.globals["defs"]["slider"].value == 0
        assert k.globals["ns"].count == 1
        assert k.globals["slider_value"] == 1
        element_id = k.globals["defs"]["slider"]._id

        await k.set_ui_element_value(
            SetUIElementValueRequest.from_ids_and_values([(element_id, 5)])
        )
        assert k.globals["defs"]["slider"].value == 5
        if k.lazy():
            assert graph.get_stale() == set([er.cell_id])
            await k.run([er])
        assert not graph.get_stale()
        assert k.globals["slider_value"] == 6
        # reactive execution on the slider in `defs` shouldn't trigger reactive
        # execution on `second_defs`
        assert k.globals["ns"].count == 1

    async def test_set_ui_element_value_not_found_doesnt_fail(
        self,
        any_kernel: Kernel,
    ) -> None:
        # smoke test -- this shouldn't raise an exception
        k = any_kernel
        await k.set_ui_element_value(
            SetUIElementValueRequest.from_ids_and_values(
                [("does not exist", None)]
            )
        )

    async def test_interrupt(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        er = exec_req.get(
            """
            from marimo._runtime.control_flow import MarimoInterrupt

            tries = 0
            while tries < 5:
                try:
                    raise MarimoInterrupt
                except Exception:
                    ...
                tries += 1
            """
        )
        await k.run([er])
        # make sure the interrupt wasn't caught by the try/except
        assert k.globals["tries"] == 0

    async def test_interrupt_cancels_old_run_requests(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        er_interrupt = exec_req.get(
            """
            from marimo._runtime.control_flow import MarimoInterrupt

            tries = 0
            while tries < 5:
                try:
                    raise MarimoInterrupt
                except Exception:
                    ...
                tries += 1
            """
        )
        er_other = exec_req.get("x = 0")
        # set a timestamp that's guaranteed to be less than the time
        # of the interrupt -- so er_other shouldn't run
        er_other.timestamp = -1
        await k.run([er_interrupt])
        # make sure the interrupt wasn't caught by the try/except
        assert k.globals["tries"] == 0
        await k.run([er_other])
        assert er_other.cell_id not in k.graph.cells
        assert "x" not in k.globals

    async def test_running_in_notebook(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run(
            [
                exec_req.get(
                    "import marimo as mo; in_nb = mo.running_in_notebook()"
                )
            ]
        )
        assert k.globals["in_nb"]

    async def test_file_path(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run(
            [
                exec_req.get("import marimo as mo"),
                exec_req.get("x = __file__"),
            ]
        )

        assert "pytest" in k.globals["x"]

    async def test_pickle(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run(
            [
                exec_req.get("import pickle"),
                exec_req.get(
                    """
                    def foo():
                        ...

                    pickle_output = None
                    pickle_output = pickle.dumps(foo)
                    post_pickle_var = 1
                    """
                ),
            ]
        )
        assert not k.errors
        assert "post_pickle_var" in k.globals
        assert k.globals["post_pickle_var"] == 1
        assert k.globals["pickle_output"] is not None

    def test_sys_path_updated(self, tmp_path: pathlib.Path) -> None:
        try:
            filename = str(tmp_path / "notebook.py")
            Kernel(
                stream=NoopStream(),
                stdout=None,
                stderr=None,
                stdin=None,
                cell_configs={},
                user_config=DEFAULT_CONFIG,
                app_metadata=AppMetadata(
                    query_params={}, filename=filename, cli_args={}
                ),
                enqueue_control_request=lambda _: None,
                module=create_main_module(None, None),
            )
            assert str(tmp_path) in sys.path
            assert str(tmp_path) == sys.path[0]
        finally:
            if str(tmp_path) in sys.path:
                sys.path.remove(str(tmp_path))

    async def test_set_config_before_registering_cell(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        er_1 = exec_req.get("x = 0")
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_1.cell_id: {"disabled": True}})
        )
        await k.run([er_1])
        assert k.graph.cells[er_1.cell_id].config.disabled
        assert "x" not in k.globals

    async def test_run_code_with_nbsp(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        # u00A0 is a non-breaking space (nbsp), which gets inserted on some
        # platforms/browsers; marimo converts these characters to spaces ...
        code = "x \u00a0 = 10"
        await k.run([exec_req.get(code)])
        assert not k.errors
        assert k.globals["x"] == 10

    @staticmethod
    async def test_exception_not_captured(
        any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel

        await k.run(
            [
                er_1 := exec_req.get(
                    """
                    exc = None
                    try:
                        1 / 0
                    except ZeroDivisionError as exc:
                        e = exc
                    """
                ),
            ]
        )
        assert not k.errors
        assert "exc" not in k.globals
        assert "exc" not in k.graph.cells[er_1.cell_id].refs
        assert "exc" in k.graph.cells[er_1.cell_id].defs
        assert "e" in k.globals
        assert isinstance(k.globals["e"], ZeroDivisionError)

    @staticmethod
    async def test_exception_scope_not_captured(
        any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel

        await k.run(
            [
                er_1 := exec_req.get(
                    """
                    try:
                        1 / 0
                    except ZeroDivisionError as exc:
                        e = exc
                        exc = e
                    """
                ),
            ]
        )
        assert not k.errors
        assert "exc" not in k.globals
        assert "exc" not in k.graph.cells[er_1.cell_id].refs
        assert "exc" not in k.graph.cells[er_1.cell_id].defs
        assert "e" in k.globals

    @staticmethod
    async def test_runtime_name_error_reference_caught(
        execution_kernel: Kernel,
    ) -> None:
        k = execution_kernel
        await k.run(
            [
                ExecutionRequest(
                    cell_id="0",
                    code=textwrap.dedent(
                        """
                    try:
                        R = R # Causes error since no def
                        C = 0 # Unaccessible
                    except:
                        pass
                    """
                    ),
                ),
                ExecutionRequest(
                    cell_id="1",
                    code=textwrap.dedent(
                        """
                    C
                    """
                    ),
                ),
            ]
        )
        # Runtime error expected- since not a kernel error check stderr
        assert "C" not in k.globals
        if k.execution_type == "strict":
            assert (
                "name `R` is referenced before definition."
                in k.stream.messages[-4][1]["output"]["data"][0]["msg"]
            )
            assert (
                "This cell wasn't run"
                in k.stream.messages[-1][1]["output"]["data"][0]["msg"]
            )
        else:
            assert (
                "marimo came across the undefined variable `C` during runtime."
                in k.stream.messages[-2][1]["output"]["data"][0]["msg"]
            )
            assert "NameError" in k.stderr.messages[0]
            assert "NameError" in k.stderr.messages[-1]

    @staticmethod
    async def test_run_scratch(mocked_kernel: MockedKernel) -> None:
        k = mocked_kernel.k
        await k.run_scratchpad("x = 1; x")
        # Has no errors
        assert not k.errors
        messages = mocked_kernel.stream.messages
        (m1, m2, m3, m4) = messages
        assert all(m[0] == "cell-op" for m in messages)
        assert all(m[1]["cell_id"] == SCRATCH_CELL_ID for m in messages)
        assert m1[1]["status"] == "queued"
        assert m2[1]["status"] == "running"
        assert m3[1]["status"] is None
        assert (
            m3[1]["output"]["data"] == "<pre style='font-size: 12px'>1</pre>"
        )
        assert m4[1]["status"] == "idle"
        # Does not pollute globals
        assert "x" not in k.globals

    @staticmethod
    async def test_run_scratch_with_other_globals(
        mocked_kernel: MockedKernel, exec_req: ExecReqProvider
    ) -> None:
        k = mocked_kernel.k
        await k.run(
            [
                exec_req.get(
                    """
                    z = 10
                    """
                ),
            ]
        )

        await k.run_scratchpad("y = z * 2; y")
        # Has no errors
        assert not k.errors
        messages = mocked_kernel.stream.messages
        output_message = messages[-2]
        assert (
            output_message[1]["output"]["data"]
            == "<pre style='font-size: 12px'>20</pre>"
        )
        assert "z" in k.globals
        # Does not pollute globals
        assert "y" not in k.globals

    @staticmethod
    async def test_run_scratch_can_temporarily_overwrite_globals(
        mocked_kernel: MockedKernel, exec_req: ExecReqProvider
    ) -> None:
        k = mocked_kernel.k
        await k.run(
            [
                exec_req.get(
                    """
                    z = 10
                    """
                ),
            ]
        )

        await k.run_scratchpad("z = 20; z")
        # Has no errors
        assert not k.errors
        messages = mocked_kernel.stream.messages
        output_message = messages[-2]
        assert (
            output_message[1]["output"]["data"]
            == "<pre style='font-size: 12px'>20</pre>"
        )
        assert "z" in k.globals
        # Does not pollute globals, reverts back to 10
        assert k.globals["z"] == 10

    async def test_rename(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run([er := exec_req.get("x = __file__")])
        assert "pytest" in k.globals["x"]
        await k.rename_file("foo")
        if k.lazy():
            assert "pytest" in k.globals["x"]
            assert k.graph.get_stale() == set([er.cell_id])
            await k.run([er])
        assert k.globals["x"] == "foo"


class TestStrictExecution:
    @staticmethod
    async def test_cell_lambda(
        strict_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = strict_kernel
        await k.run(
            [
                exec_req.get("""Y = 1"""),
                exec_req.get(
                    """
                  _x = 1
                  X = 1
                  L = lambda x: x + _x + X + Y
                  """
                ),
                exec_req.get(
                    """
                V = L(1)
                V
                """
                ),
            ]
        )
        assert not k.errors
        assert "X" in k.globals
        assert "Y" in k.globals
        assert "L" in k.globals
        assert "V" in k.globals
        assert k.globals["V"] == 4

    @staticmethod
    async def test_cell_indirect_lambda(
        strict_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = strict_kernel
        await k.run(
            [
                exec_req.get("""Y = 1"""),
                exec_req.get(
                    """
                  _x = 1
                  X = 1
                  L = [lambda x: x + _x + X + Y]
                  """
                ),
                exec_req.get("V = L[0](1)"),
            ]
        )
        assert not k.errors
        assert "X" in k.globals
        assert "Y" in k.globals
        assert "L" in k.globals
        assert "V" in k.globals
        assert k.globals["V"] == 4

    @staticmethod
    async def test_cell_indirect_private(
        strict_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = strict_kernel
        await k.run(
            [
                exec_req.get(
                    """
                             Y = 1
                             _y = 1
                             def f(x):
                                return x + _y
                             """
                ),
                exec_req.get(
                    """
                  _x = 1
                  X = 1
                  L = [lambda x: f(x + _x + X + Y)]
                  """
                ),
                exec_req.get("V = L[0](1)"),
            ]
        )
        assert not k.errors
        assert "X" in k.globals
        assert "Y" in k.globals
        assert "L" in k.globals
        assert "V" in k.globals
        assert "f" in k.globals
        assert k.globals["V"] == 5

    @staticmethod
    async def test_cell_copy_works(
        strict_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = strict_kernel
        await k.run(
            [
                exec_req.get(
                    """
                             class namespace:
                                ...
                             X = namespace()
                             X.count = 1
                             """
                ),
                exec_req.get(
                    """
                  X.count += 1
                  V0 = X.count
                  """
                ),
                exec_req.get(
                    """
                  X.count += 10
                  V1 = X.count
                  """
                ),
            ]
        )
        assert not k.errors
        assert "X" in k.globals
        assert "V0" in k.globals
        assert "V1" in k.globals
        assert k.globals["X"].count == 1
        assert k.globals["V0"] == 2
        assert k.globals["V1"] == 11

    @staticmethod
    async def test_cell_zero_copy_works(
        strict_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = strict_kernel
        await k.run(
            [
                exec_req.get(
                    """
                             import marimo as mo
                             class namespace:
                                ...
                             X = namespace()
                             X.count = 1
                             X = mo._runtime.copy.zero_copy(X)
                             """
                ),
                exec_req.get(
                    """
                  mo._runtime.copy.unwrap_copy(X).count += 1
                  V0 = X.count
                  """
                ),
                exec_req.get(
                    """
                  mo._runtime.copy.unwrap_copy(X).count += 10
                  V1 = X.count
                  """
                ),
            ]
        )
        assert not k.errors
        assert "X" in k.globals
        assert "V0" in k.globals
        assert "V1" in k.globals
        assert k.globals["X"].count == 12
        assert k.globals["V0"] in (2, 12)
        if k.globals["V0"] == 2:
            assert k.globals["V1"] == 12
        else:
            assert k.globals["V1"] == 11

    @staticmethod
    async def test_wont_execute_bad_ref(execution_kernel: Kernel) -> None:
        k = execution_kernel
        await k.run(
            [
                ExecutionRequest(
                    cell_id="0",
                    code=textwrap.dedent(
                        """
                    try:
                        missing
                    except:
                        pass
                    x = 1
                    """
                    ),
                ),
            ]
        )

        if k.execution_type == "strict":
            assert "x" not in k.globals
            assert set(k.errors.keys()) == {"0"}
            assert len(k.errors["0"]) == 1
            assert isinstance(k.errors["0"][0], MarimoStrictExecutionError)
            assert k.errors["0"][0].ref == "missing"
        # Check that normal execution still runs the block
        else:
            assert "x" in k.globals
            assert not k.errors

    @staticmethod
    async def test_runtime_failure(strict_kernel: Kernel) -> None:
        k = strict_kernel
        # We keep variable data for reassignments, so static analysis should
        # succeed
        await k.run(
            [
                ExecutionRequest(
                    cell_id="0",
                    code=textwrap.dedent(
                        """
                    X = 1
                    Y = 2
                    l = lambda x: x + X
                    L = l
                    l = lambda x: x + Y
                    """
                    ),
                ),
                ExecutionRequest(
                    cell_id="1",
                    code=textwrap.dedent(
                        """
                    x = L(1)
                    """
                    ),
                ),
            ]
        )
        assert "x" in k.globals
        assert k.globals["x"] == 2

    @staticmethod
    async def test_runtime_resolution_private(
        strict_kernel: Kernel,
    ) -> None:
        k = strict_kernel
        # We keep variable data for reassignments, so static analysis should
        # succeed
        await k.run(
            [
                ExecutionRequest(
                    cell_id="0",
                    code=textwrap.dedent(
                        """
                    _X = 1
                    Y = 2
                    l = lambda x: x + _X
                    L = l
                    l = lambda x: x + Y
                    """
                    ),
                ),
                ExecutionRequest(
                    cell_id="1",
                    code=textwrap.dedent(
                        """
                    x = L(1)
                    """
                    ),
                ),
            ]
        )
        assert "x" in k.globals
        assert k.globals["x"] == 2


class TestImports:
    async def test_import_triggers_execution(
        self, k: Kernel, exec_req: ExecReqProvider
    ) -> None:
        await k.run([exec_req.get("random; x = 0")])
        assert "x" not in k.globals

        await k.run([exec_req.get("import random")])
        assert k.globals["x"] == 0

    async def test_reimport_doesnt_trigger_execution(
        self, k: Kernel, exec_req: ExecReqProvider
    ) -> None:
        await k.run([er := exec_req.get("import random")])

        await k.run([exec_req.get("x = random.randint(0, 100000)")])
        x = k.globals["x"]

        # re-running an import shouldn't retrigger execution
        await k.run([er])
        assert k.globals["x"] == x

    async def test_incremental_import_doesnt_trigger_execution(
        self, k: Kernel, exec_req: ExecReqProvider
    ) -> None:
        await k.run([er := exec_req.get("import random")])

        await k.run([exec_req.get("x = random.randint(0, 100000)")])
        x = k.globals["x"]

        # adding another import to the cell shouldn't rerun dependents
        # of already imported modules
        await k.run(
            [
                ExecutionRequest(
                    cell_id=er.cell_id, code="import random; import time"
                )
            ]
        )
        assert k.globals["x"] == x

    async def test_transition_out_of_error_triggers_run(
        self, k: Kernel, exec_req: ExecReqProvider
    ) -> None:
        await k.run(
            [
                exec_req.get("import random"),
                er := exec_req.get("import random"),
                exec_req.get("random; x = 0"),
            ]
        )
        assert "x" not in k.globals

        await k.delete(DeleteCellRequest(cell_id=er.cell_id))
        assert "x" in k.globals

    async def test_different_import_same_def(
        self, k: Kernel, exec_req: ExecReqProvider
    ) -> None:
        await k.run([er := exec_req.get("import random")])

        await k.run([exec_req.get("x = random.randint(0, 100000)")])
        assert "x" in k.globals

        # er.cell_id is still an import block, still defines random,
        # but brings random from another place; descendant should run
        await k.run(
            [ExecutionRequest(er.cell_id, code="from random import random")]
        )
        assert "random" in k.globals
        # randint is on toplevel random, not random.random
        assert "x" not in k.globals

    async def test_after_import_error(
        self, k: Kernel, exec_req: ExecReqProvider
    ) -> None:
        await k.run(
            [
                er := exec_req.get("import time; import fake_module"),
                exec_req.get("time; x = 1"),
            ]
        )
        assert "x" not in k.globals

        await k.run([ExecutionRequest(er.cell_id, code="import time")])
        assert k.globals["x"] == 1


class TestStoredOutput:
    async def test_ui_element_in_output_stored(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel

        await k.run(
            [
                er := exec_req.get(
                    """
                    import marimo as mo

                    mo.ui.checkbox()
                    """
                )
            ]
        )
        cell = k.graph.cells[er.cell_id]
        assert isinstance(cell.output, UIElement)

    async def test_ui_element_in_nested_output_stored(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel

        await k.run(
            [
                er := exec_req.get(
                    """
                    import marimo as mo

                    [mo.ui.checkbox()]
                    """
                )
            ]
        )
        cell = k.graph.cells[er.cell_id]
        assert isinstance(cell.output[0], UIElement)

    async def test_non_ui_elements_not_stored(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel

        await k.run(
            [
                er := exec_req.get(
                    """
                    import marimo as mo

                    'an output'
                    """
                )
            ]
        )
        cell = k.graph.cells[er.cell_id]
        assert cell.output is None

    async def test_cell_output_cleared_on_rerun(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel

        await k.run(
            [
                er := exec_req.get(
                    """
                    import marimo as mo

                    mo.ui.checkbox()
                    """
                )
            ]
        )
        cell = k.graph.cells[er.cell_id]
        assert isinstance(cell.output, UIElement)

        await k.run(
            [
                exec_req.get_with_id(
                    er.cell_id,
                    """
                    import marimo as mo

                    raise ValueError
                    """,
                )
            ]
        )
        cell = k.graph.cells[er.cell_id]
        assert cell.output is None


class TestDisable:
    async def test_disable_and_reenable_not_stale(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        graph = k.graph
        await k.run(
            [
                exec_req.get(
                    """
                        import weakref
                        class namespace:
                            ...
                        ns = namespace()
                        ns.count = 0
                        ref = weakref.ref(ns)
                        """
                ),
                er_2 := exec_req.get("ref().count += 1"),
            ]
        )
        assert k.globals["ns"].count == 1
        assert not graph.get_stale()

        # disable and re-enable cell 2: cell 2 should not re-run because it
        # shouldn't have passed through stale status
        # disable cell 2
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_2.cell_id: {"disabled": True}})
        )
        assert k.globals["ns"].count == 1
        assert not graph.get_stale()

        # re-enable cell 2
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_2.cell_id: {"disabled": False}})
        )
        # cell 2 should **not** have re-run
        assert k.globals["ns"].count == 1
        assert not graph.get_stale()

    async def test_disable_and_reenable_stale(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        graph = k.graph
        await k.run(
            [
                er_1 := exec_req.get(
                    """
                        class namespace:
                            ...
                        ns = namespace()
                        ns.count = 0
                        import weakref
                        ref = weakref.ref(ns)
                        """
                ),
                er_2 := exec_req.get("ref().count += 1"),
            ]
        )
        assert k.globals["ns"].count == 1
        assert not graph.get_stale()

        # disable and re-enable cell 2, making it stale in between;
        # cell 2 should re-run on enable
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_2.cell_id: {"disabled": True}})
        )
        await k.run(
            [
                exec_req.get_with_id(
                    er_1.cell_id,
                    """
                    class namespace:
                        ...
                    ns = namespace()
                    ns.count = 10
                    import weakref
                    ref = weakref.ref(ns)
                    """,
                )
            ]
        )

        assert k.globals["ns"].count == 10
        assert k.graph.get_stale() == set([er_2.cell_id])

        # re-enable cell 2
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_2.cell_id: {"disabled": False}})
        )
        if k.lazy():
            assert k.graph.get_stale() == set([er_2.cell_id])
            await k.run([er_2])
        assert not k.graph.get_stale()
        # cell 2 should have re-run
        assert k.globals["ns"].count == 11

    async def test_disable_and_reenable_tree(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        # x
        # x --> y
        # x, y --> z
        k = any_kernel
        graph = k.graph
        await k.run(
            [
                (er_1 := exec_req.get("x = 1")),
                (er_2 := exec_req.get("y = x + 1")),
                (er_3 := exec_req.get("z = x + y")),
                (er_4 := exec_req.get("zz = z + 1")),
                (er_5 := exec_req.get("zzz = x + 1")),
            ]
        )
        assert k.globals["x"] == 1
        assert k.globals["y"] == 2
        assert k.globals["z"] == 3
        assert k.globals["zz"] == 4
        assert k.globals["zzz"] == 2
        assert not graph.get_stale()

        # disable cell 2
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_2.cell_id: {"disabled": True}})
        )

        await k.run([ExecutionRequest(cell_id=er_1.cell_id, code="x = 2")])
        assert k.globals["x"] == 2
        if k.lazy():
            assert graph.get_stale() == set(
                [er_2.cell_id, er_3.cell_id, er_4.cell_id, er_5.cell_id]
            )
            await k.run([er_5])
        assert graph.get_stale() == set(
            [er_2.cell_id, er_3.cell_id, er_4.cell_id]
        )
        assert k.globals["zzz"] == 3

        # enable cell 2: should run stale cells as a side-effect
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_2.cell_id: {"disabled": False}})
        )
        assert k.globals["x"] == 2
        assert k.globals["zzz"] == 3
        if k.lazy():
            assert graph.get_stale() == set(
                [er_2.cell_id, er_3.cell_id, er_4.cell_id]
            )
            # runs er_3 and er_2, which are stale ancestors
            await k.run([er_4])
        # stale cells **should have** updated
        assert not graph.get_stale()
        assert k.globals["y"] == 3
        assert k.globals["z"] == 5
        assert k.globals["zz"] == 6

    async def test_disable_consecutive(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        graph = k.graph
        await k.run(
            [
                (er_1 := exec_req.get("x = 1")),
                (er_2 := exec_req.get("y = x + 1")),
            ]
        )
        assert k.globals["x"] == 1
        assert k.globals["y"] == 2
        assert not graph.get_stale()

        # disable both cells
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_1.cell_id: {"disabled": True}})
        )
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_2.cell_id: {"disabled": True}})
        )
        # update the code of cell 1 -- both cells stale
        await k.run([er_1 := exec_req.get_with_id(er_1.cell_id, "x = 2")])
        assert graph.get_stale() == set([er_1.cell_id, er_2.cell_id])

        # enable cell 1, but 2 still disabled
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_1.cell_id: {"disabled": False}})
        )
        if k.lazy():
            assert graph.get_stale() == set([er_1.cell_id, er_2.cell_id])
            await k.run([er_1])

        assert k.globals["x"] == 2
        assert graph.get_stale() == set([er_2.cell_id])

        # enable cell 2
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_2.cell_id: {"disabled": False}})
        )
        if k.lazy():
            assert graph.get_stale() == set([er_2.cell_id])
            await k.run([er_2])

        assert not graph.get_stale()
        assert k.globals["x"] == 2
        assert k.globals["y"] == 3

    async def test_disable_syntax_error(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        graph = k.graph
        await k.run(
            [
                er_1 := exec_req.get("x = 1"),
            ]
        )
        assert k.globals["x"] == 1

        # disable cell
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_1.cell_id: {"disabled": True}})
        )

        # add a syntax error
        await k.run([exec_req.get_with_id(er_1.cell_id, "x 2")])
        assert "x" not in k.globals

        # repair syntax, cell should still be disabled
        await k.run([er_1 := exec_req.get_with_id(er_1.cell_id, "x = 2")])
        assert "x" not in k.globals
        assert graph.cells[er_1.cell_id].stale

        # enable: should run
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_1.cell_id: {"disabled": False}})
        )
        if k.lazy():
            assert graph.cells[er_1.cell_id].stale
            await k.run([er_1])

        assert not graph.cells[er_1.cell_id].stale
        assert k.globals["x"] == 2

    async def test_disable_cycle(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run(
            [
                (er_1 := exec_req.get("a = b")),
                (er_2 := exec_req.get("b = a")),
            ]
        )
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_1.cell_id: {"disabled": True}})
        )
        assert k.graph.cells[er_1.cell_id].config.disabled
        assert k.graph.cells[er_2.cell_id].disabled_transitively

        await k.set_cell_config(
            SetCellConfigRequest(configs={er_2.cell_id: {"disabled": True}})
        )
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_1.cell_id: {"disabled": False}})
        )
        assert k.graph.cells[er_1.cell_id].disabled_transitively
        assert k.graph.cells[er_2.cell_id].config.disabled

        # adding a new cell shouldn't toggle disabled states of either
        await k.run([exec_req.get("c = 0")])
        assert k.graph.cells[er_1.cell_id].disabled_transitively
        assert k.graph.cells[er_2.cell_id].config.disabled

        # breaking the cycle should re-enable
        await k.run([ExecutionRequest(cell_id=er_1.cell_id, code="a = 0")])

        assert not k.graph.cells[er_1.cell_id].stale
        assert not k.graph.cells[er_1.cell_id].disabled_transitively
        assert not k.graph.cells[er_1.cell_id].config.disabled

        assert k.graph.cells[er_2.cell_id].config.disabled
        assert k.graph.cells[er_2.cell_id].stale

    async def test_disable_cycle_incremental(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run([er_1 := exec_req.get("a = b")])
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_1.cell_id: {"disabled": True}})
        )
        assert k.graph.cells[er_1.cell_id].config.disabled

        await k.run([er_2 := exec_req.get("b = a")])
        assert k.graph.cells[er_2.cell_id].disabled_transitively

    async def test_enable_cycle_incremental(
        self, any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run(
            [
                (er_1 := exec_req.get("a = b")),
                (er_2 := exec_req.get("b = a")),
            ]
        )
        await k.set_cell_config(
            SetCellConfigRequest(configs={er_1.cell_id: {"disabled": True}})
        )
        assert k.graph.cells[er_1.cell_id].config.disabled
        assert k.graph.cells[er_2.cell_id].disabled_transitively

        await k.set_cell_config(
            SetCellConfigRequest(configs={er_1.cell_id: {"disabled": False}})
        )
        assert not k.graph.cells[er_1.cell_id].config.disabled
        assert not k.graph.cells[er_2.cell_id].disabled_transitively
        assert k.graph.cells[er_2.cell_id].runtime_state == "idle"


class TestAsyncIO:
    @staticmethod
    async def test_toplevel_await_allowed(
        any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run(
            [
                exec_req.get(
                    """
                    import asyncio
                    await asyncio.sleep(0)
                    ran = True
                    """
                ),
            ]
        )
        assert k.globals["ran"]

    @staticmethod
    async def test_toplevel_gather(
        any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run(
            [
                exec_req.get(
                    """
                    l = []
                    async def f():
                        l.append(1)
                        await asyncio.sleep(0.1)
                        l.append(2)

                    import asyncio
                    await asyncio.gather(f(), f())
                    """
                ),
            ]
        )
        assert k.globals["l"] == [1, 1, 2, 2]

    @staticmethod
    async def test_wait_for(
        any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        import asyncio

        k = any_kernel

        await k.run(
            [
                exec_req.get(
                    """
                    import asyncio
                    async def eternity():
                        await asyncio.sleep(3600)

                    e = None
                    try:
                        await asyncio.wait_for(eternity(), timeout=0)
                    except asyncio.exceptions.TimeoutError as exc:
                        e = exc
                    """
                ),
            ]
        )
        assert not k.errors
        assert isinstance(k.globals["e"], asyncio.exceptions.TimeoutError)

    @staticmethod
    async def test_await_future(
        any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run(
            [
                exec_req.get(
                    """
                    import asyncio
                    future = asyncio.Future()
                    future.set_result(1)
                    """
                ),
                exec_req.get(
                    """
                    result = await future
                    """
                ),
            ]
        )
        assert k.globals["result"] == 1
        assert k.globals["future"].done()

    @staticmethod
    async def test_await_future_complex(
        any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run(
            [
                exec_req.get(
                    """
                    import asyncio
                    """
                ),
                exec_req.get(
                    """
                    async def set_after(fut, delay, value):
                        await asyncio.sleep(delay)
                        fut.set_result(value)
                    """
                ),
                exec_req.get(
                    """
                    fut = asyncio.Future()
                    asyncio.create_task(set_after(fut, 0.01, "done"))
                    result = await fut
                    """
                ),
            ]
        )
        assert k.globals["result"] == "done"

    @staticmethod
    async def test_run_in_default_executor(
        any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run(
            [
                exec_req.get(
                    """
                    import asyncio
                    """
                ),
                exec_req.get(
                    """
                    def blocking():
                        return "done"
                    """
                ),
                exec_req.get(
                    """
                    res = await asyncio.get_running_loop().run_in_executor(
                        None, blocking)
                    """
                ),
            ]
        )
        assert k.globals["res"] == "done"

    @staticmethod
    async def test_run_in_threadpool_executor(
        any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run(
            [
                exec_req.get(
                    """
                    import asyncio
                    import concurrent.futures
                    """
                ),
                exec_req.get(
                    """
                    def blocking():
                        return "done"
                    """
                ),
                exec_req.get(
                    """
                    loop = asyncio.get_running_loop()
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        res = await loop.run_in_executor(pool, blocking)
                    """
                ),
            ]
        )
        assert k.globals["res"] == "done"

    @staticmethod
    @pytest.mark.xfail(
        condition=sys.platform == "win32" or sys.platform == "darwin",
        reason=(
            "Bug in interaction with multiprocessing on Windows, macOS; "
            "doesn't work in Jupyter either."
        ),
    )
    async def test_run_in_processpool_executor(
        any_kernel: Kernel, exec_req: ExecReqProvider
    ) -> None:
        k = any_kernel
        await k.run(
            [
                exec_req.get(
                    """
                    import asyncio
                    import concurrent.futures
                    """
                ),
                exec_req.get(
                    """
                    def blocking():
                        return "done"
                    """
                ),
                exec_req.get(
                    """
                    loop = asyncio.get_running_loop()
                    with concurrent.futures.ProcessPoolExecutor() as pool:
                        res = await loop.run_in_executor(pool, blocking)
                    """
                ),
            ]
        )
        assert not k.errors
        assert k.globals["res"] == "done"


class TestStateTransitions:
    async def test_statuses_not_repeated_ok_run(
        self, mocked_kernel: MockedKernel, exec_req: ExecReqProvider
    ) -> None:
        k = mocked_kernel.k
        await k.run(
            [
                exec_req.get("x = 0"),
            ]
        )

        cell_ops = [
            parse_raw(op_data, CellOp)
            for op_name, op_data in mocked_kernel.stream.messages
            if op_name == "cell-op"
        ]

        n_queued = sum([1 for op in cell_ops if op.status == "queued"])
        assert n_queued == 1

        n_running = sum([1 for op in cell_ops if op.status == "running"])
        assert n_running == 1

        n_idle = sum([1 for op in cell_ops if op.status == "idle"])
        assert n_idle == 1

    async def test_statuses_not_repeated_on_stop(
        self, mocked_kernel: MockedKernel, exec_req: ExecReqProvider
    ) -> None:
        k = mocked_kernel.k
        await k.run(
            [
                exec_req.get("import marimo as mo; mo.stop(True)"),
            ]
        )

        cell_ops = [
            parse_raw(op_data, CellOp)
            for op_name, op_data in mocked_kernel.stream.messages
            if op_name == "cell-op"
        ]

        n_queued = sum([1 for op in cell_ops if op.status == "queued"])
        assert n_queued == 1

        n_running = sum([1 for op in cell_ops if op.status == "running"])
        assert n_running == 1

        n_idle = sum([1 for op in cell_ops if op.status == "idle"])
        assert n_idle == 1

    async def test_statuses_not_repeated_on_interruption(
        self, mocked_kernel: MockedKernel, exec_req: ExecReqProvider
    ) -> None:
        k = mocked_kernel.k
        await k.run(
            [
                exec_req.get(
                    "from marimo._runtime.control_flow import MarimoInterrupt; raise MarimoInterrupt()"  # noqa: E501
                ),
            ]
        )

        cell_ops = [
            parse_raw(op_data, CellOp)
            for op_name, op_data in mocked_kernel.stream.messages
            if op_name == "cell-op"
        ]

        n_queued = sum([1 for op in cell_ops if op.status == "queued"])
        assert n_queued == 1

        n_running = sum([1 for op in cell_ops if op.status == "running"])
        assert n_running == 1

        n_idle = sum([1 for op in cell_ops if op.status == "idle"])
        assert n_idle == 1

    async def test_statuses_not_repeated_on_exception(
        self, mocked_kernel: MockedKernel, exec_req: ExecReqProvider
    ) -> None:
        k = mocked_kernel.k
        await k.run(
            [
                exec_req.get("raise ValueError"),
            ]
        )

        cell_ops = [
            parse_raw(op_data, CellOp)
            for op_name, op_data in mocked_kernel.stream.messages
            if op_name == "cell-op"
        ]

        n_queued = sum([1 for op in cell_ops if op.status == "queued"])
        assert n_queued == 1

        n_running = sum([1 for op in cell_ops if op.status == "running"])
        assert n_running == 1

        n_idle = sum([1 for op in cell_ops if op.status == "idle"])
        assert n_idle == 1

    async def test_descendant_status_reset_to_idle_on_error(
        self, mocked_kernel: MockedKernel, exec_req: ExecReqProvider
    ) -> None:
        k = mocked_kernel.k
        await k.run(
            [
                er_1 := exec_req.get("x = 0; raise ValueError"),
                er_2 := exec_req.get("x"),
            ]
        )

        cell_ops = [
            parse_raw(op_data, CellOp)
            for op_name, op_data in mocked_kernel.stream.messages
            if op_name == "cell-op"
        ]

        # er_1 and er_2
        n_queued = sum([1 for op in cell_ops if op.status == "queued"])
        assert n_queued == 2

        # only er_1 runs
        n_running = sum([1 for op in cell_ops if op.status == "running"])
        assert n_running == 1

        # er_1 and er_2
        n_idle = sum([1 for op in cell_ops if op.status == "idle"])
        assert n_idle == 2

        assert k.graph.cells[er_1.cell_id].runtime_state == "idle"
        assert k.graph.cells[er_2.cell_id].runtime_state == "idle"

    async def test_descendant_status_reset_to_idle_on_interrupt(
        self, mocked_kernel: MockedKernel, exec_req: ExecReqProvider
    ) -> None:
        k = mocked_kernel.k
        await k.run(
            [
                er_1 := exec_req.get(
                    """
                    from marimo._runtime.control_flow import MarimoInterrupt

                    x = 0
                    raise MarimoInterrupt
                    """
                ),
                er_2 := exec_req.get("x"),
            ]
        )

        cell_ops = [
            parse_raw(op_data, CellOp)
            for op_name, op_data in mocked_kernel.stream.messages
            if op_name == "cell-op"
        ]

        # er_1 and er_2
        n_queued = sum([1 for op in cell_ops if op.status == "queued"])
        assert n_queued == 2

        # only er_1 runs
        n_running = sum([1 for op in cell_ops if op.status == "running"])
        assert n_running == 1

        # er_1 and er_2
        n_idle = sum([1 for op in cell_ops if op.status == "idle"])
        assert n_idle == 2

        assert k.graph.cells[er_1.cell_id].runtime_state == "idle"
        assert k.graph.cells[er_2.cell_id].runtime_state == "idle"
