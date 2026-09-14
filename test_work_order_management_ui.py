"""Exercise actual Dash callbacks with a temporary database, without production data."""
import ast
import hmac
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import numpy as np
import pandas as pd
from dash import html, dash_table, no_update
from dash.exceptions import PreventUpdate
from flask import Flask, session

from work_order_management import exclusions, exclude_work_order


class ManagementUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "exclusions.sqlite3"
        self.flask = Flask(__name__)
        self.flask.secret_key = "isolated-test-only"
        self.context = self.flask.test_request_context("/", method="POST")
        self.context.push()
        self.addCleanup(self.context.pop)
        self.callback_context = SimpleNamespace(triggered=[])
        self.snapshot = {"rows": [
            {"工单号": "5001727589", "Qdrant状态": "已加入"},
            {"工单号": "5001725262", "Qdrant状态": "待处理"},
        ]}
        self.bundle = {"vectors": np.array([[1., 0.], [0., 1.]]),
                       "metadata": pd.DataFrame({"work_order_no": [5001727589, 5001725262]}),
                       "node_index": {"a": 0, "b": 1}}
        self.ns = dict(html=html, dash_table=dash_table, no_update=no_update,
                       PreventUpdate=PreventUpdate, session=session, hmac=hmac, secrets=secrets,
                       time=time, callback_context=self.callback_context,
                       exclusions=exclusions, exclude_work_order=exclude_work_order,
                       WORK_ORDER_MANAGEMENT_PATH=self.path,
                       _SESSION_QUERIES_LOCK=RLock(), _SESSION_QUERIES={"old": {}},
                       load_all_work_order_statuses=lambda: self.snapshot,
                       _load_qdrant_bundle=lambda: self.bundle)
        source = ast.parse(Path(__file__).with_name("app_visualize.py").read_text(encoding="utf-8"))
        names = {"render_work_order_status_list", "manage_work_order_deletion", "load_library", "toggle_work_order_edit_mode"}
        functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
        for function in functions:
            function.decorator_list = []
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + functions, type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "actual_callbacks", "exec"), self.ns)

    def call(self, trigger, ids=None, scope="list", token=None):
        self.callback_context.triggered = [{"prop_id": trigger}]
        return self.ns["manage_work_order_deletion"](1, 1, 1, ids, scope, token)

    def test_auth_cancel_and_invalid_confirmation(self):
        result = self.call("request_work_order_delete.n_clicks", ["5001727589"])
        self.assertFalse(result[0])
        session["fault_ai_authenticated"] = True
        result = self.call("request_work_order_delete.n_clicks", ["5001727589"])
        self.assertTrue(result[0])
        self.assertIn("5001727589", result[1])
        self.assertFalse(self.path.exists())
        self.call("work_order_delete_confirm.cancel_n_clicks")
        self.assertFalse(self.path.exists())
        self.call("work_order_delete_confirm.submit_n_clicks", token=result[2])
        self.assertFalse(self.path.exists())

    def test_view_and_edit_modes(self):
        render = self.ns["render_work_order_status_list"]
        for editing in (False, True):
            table = next(child for child in render(self.snapshot, editing).children
                         if isinstance(child, dash_table.DataTable))
            self.assertEqual(table.row_selectable, "single" if editing else False)
            self.assertEqual(table.selected_row_ids, [])
        toggle = self.ns["toggle_work_order_edit_mode"]
        self.callback_context.triggered = [{"prop_id": "edit_work_order_status_list.n_clicks"}]
        enabled = toggle(1, 1, 0, 0, False)
        self.assertEqual(enabled[:2], (True, "完成"))
        self.assertEqual(enabled[2]["display"], "flex")
        self.assertEqual(toggle(2, 1, 0, 0, True)[0], False)
        self.callback_context.triggered = [{"prop_id": "open_work_order_status_list.n_clicks"}]
        reset = toggle(2, 2, 1, 0, True)
        self.assertEqual(reset[:2], (False, "编辑"))
        self.assertEqual(reset[2]["display"], "none")

    def test_selected_identity_persistence_and_scope(self):
        session["fault_ai_authenticated"] = True
        result = self.call("request_work_order_delete.n_clicks", ["5001725262"], "list")
        # Confirmation uses the frozen server-side identity, not a changed selection.
        self.call("work_order_delete_confirm.submit_n_clicks", ["5001727589"], "recommendation", result[2])
        self.assertEqual(exclusions(self.path), {"5001725262"})
        self.assertEqual(len(self.ns["load_library"]()[1]), 2)
        result = self.call("request_work_order_delete.n_clicks", ["5001727589"], "recommendation")
        self.call("work_order_delete_confirm.submit_n_clicks", token=result[2])
        vectors, metadata, _ = self.ns["load_library"]()
        self.assertEqual(metadata["work_order_no"].tolist(), [5001725262])
        np.testing.assert_array_equal(vectors, [[0., 1.]])
        self.assertEqual(metadata.index.tolist(), [0])
        self.assertEqual(len(self.bundle["metadata"]), 2)
        rendered = self.ns["render_work_order_status_list"](self.snapshot)
        table = next(child for child in rendered.children if isinstance(child, dash_table.DataTable))
        self.assertEqual(table.data, [])


if __name__ == "__main__":
    unittest.main()
