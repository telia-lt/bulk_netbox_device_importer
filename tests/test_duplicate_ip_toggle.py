from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        SECRET_KEY="tests-only",
        USE_I18N=False,
        INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes"],
    )
django.setup()

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache
from django.core.exceptions import SuspiciousOperation
from django.http import HttpResponseRedirect
from django.test import RequestFactory
from openpyxl import Workbook

from netbox_device_importer.excel_io import COLUMNS, INTERFACE_COLUMNS
from netbox_device_importer.views import (
    DeviceImportView, ImportStatusView, ImportStopView, _ACTIVE_JOBS, _load_state, _save_state, _state_key,
)


def duplicate_ip_workbook(formula=False):
    workbook = Workbook()
    devices = workbook.active
    devices.title = "Devices"
    devices.append([*COLUMNS, "cf_PID"])
    for index in (1, 2):
        devices.append([
            f"SW-{index}", "Tenant", "Site", "Cisco", "Model", "Role", "active",
            "=1+1" if formula and index == 2 else "", "", "", f"PID-{index}",
        ])
    interfaces = workbook.create_sheet("Interfaces")
    interfaces.append(INTERFACE_COLUMNS)
    for index in (1, 2):
        interfaces.append([
            f"SW-{index}", "Tenant", "eth0", "1000base-t", "192.0.2.1/24", "", "VRF",
        ])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


class DuplicateIPToggleTests(unittest.TestCase):
    def setUp(self):
        self.state = {"rows": [], "allow_duplicate_ips": False}
        self.factory = RequestFactory()
        self.backend = Mock()
        self.backend.prepare.return_value = {}
        self.warning = Mock()
        patches = [
            patch("netbox_device_importer.views._state_key", return_value="test"),
            patch("netbox_device_importer.views._load_state", side_effect=lambda key: deepcopy(self.state)),
            patch("netbox_device_importer.views._save_state", side_effect=self.save_state),
            patch("netbox_device_importer.views._sync_job", return_value=(False, None)),
            patch("netbox_device_importer.views.redirect", side_effect=HttpResponseRedirect),
            patch("netbox_device_importer.views.messages.success"),
            patch("netbox_device_importer.views.messages.warning", self.warning),
            patch("netbox_device_importer.views.NetBoxORMBackend", return_value=self.backend),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def save_state(self, key, state):
        self.state = deepcopy(state)

    def post(self, action, **data):
        request = self.factory.post("/import/", {"action": action, **data})
        request.user = SimpleNamespace(pk=1)
        request.import_session = "a" * 32
        return DeviceImportView().post(request)

    def upload(self, formula=False):
        self.post(
            "upload",
            excel_file=SimpleUploadedFile("devices.xlsx", duplicate_ip_workbook(formula)),
            allow_duplicate_ips="false",
        )

    def test_toggle_rechecks_duplicates_and_allows_netbox_validation(self):
        self.upload()
        self.assertEqual(self.state["rows"][1]["state"], "Failo klaida")
        self.post("set_duplicate_ips", allow_duplicate_ips="on")
        self.assertEqual(self.state["rows"][1]["state"], "Laukia patikros")
        self.assertEqual(self.state["rows"][1]["error"], "")
        self.post("validate")
        self.assertEqual([row["state"] for row in self.state["rows"]], ["Paruosta", "Paruosta"])
        self.assertEqual(self.backend.prepare.call_count, 2)
        self.state["confirm_import"] = True
        self.post("set_duplicate_ips")
        self.assertEqual(self.state["rows"][1]["state"], "Failo klaida")
        self.assertIn("Pasikartojantis IP 192.0.2.1", self.state["rows"][1]["error"])
        self.assertEqual(self.state["rows"][0]["state"], "Laukia patikros")
        self.assertFalse(self.state["confirm_import"])

    def test_toggle_preserves_formula_errors(self):
        self.upload(formula=True)
        self.post("set_duplicate_ips", allow_duplicate_ips="on")
        self.assertEqual(self.state["rows"][1]["state"], "Failo klaida")
        self.assertIn("Formules neleidziamos", self.state["rows"][1]["error"])
        self.assertNotIn("Pasikartojantis IP", self.state["rows"][1]["error"])
        self.post("validate")
        self.assertEqual(self.backend.prepare.call_count, 1)

    def test_toggle_does_not_reset_created_or_uncertain_results(self):
        self.upload()
        for row, state in zip(self.state["rows"], ["Sukurta", "Neaiškus rezultatas"]):
            row.update(state=state, device_id=42, error="Original result")
        original = deepcopy(self.state["rows"])
        self.post("set_duplicate_ips", allow_duplicate_ips="on")
        self.assertEqual(self.state["rows"], original)
        self.post("set_duplicate_ips")
        self.assertEqual(self.state["rows"], original)

    def test_older_upload_without_source_requires_reupload(self):
        self.upload()
        self.state.pop("excel_content", None)
        original = deepcopy(self.state)
        self.post("set_duplicate_ips", allow_duplicate_ips="on")
        self.warning.assert_called_once()
        self.assertEqual(self.state, original)


class TabSessionTests(unittest.TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.session = {}

    def request(self, token=None, method="get", path="/import/"):
        data = {} if token is None else {"import_session": token}
        request = getattr(self.factory, method)(path, data)
        request.user = SimpleNamespace(pk=1)
        request.session = self.session
        return request

    def test_separate_tabs_and_fresh_window_do_not_share_state(self):
        first_key = _state_key(self.request("a" * 32))
        second_key = _state_key(self.request("b" * 32))
        self.assertNotEqual(first_key, second_key)
        _save_state(first_key, {"rows": [], "filename": "first.xlsx", "last_import": {"completed": 2}})
        self.addCleanup(cache.delete, first_key)
        self.assertEqual(_load_state(first_key)["filename"], "first.xlsx")
        self.assertEqual(_load_state(second_key)["filename"], "")
        fresh_key = _state_key(self.request())
        self.assertNotEqual(first_key, fresh_key)
        self.assertEqual(_load_state(fresh_key)["filename"], "")
        self.assertEqual(_load_state(first_key)["filename"], "first.xlsx")

    def test_same_tab_retains_key_after_navigation_and_post(self):
        initial = _state_key(self.request("a" * 32))
        self.assertEqual(initial, _state_key(self.request("a" * 32)))
        self.assertEqual(initial, _state_key(self.request("a" * 32, method="post")))

    def test_action_redirect_retains_tab_identity(self):
        request = self.request("a" * 32, method="post")
        request.POST = request.POST.copy()
        request.POST["action"] = "demo"
        with patch("netbox_device_importer.views.redirect", side_effect=HttpResponseRedirect):
            response = DeviceImportView().post(request)
        self.assertEqual(parse_qs(urlparse(response.url).query)["import_session"], ["a" * 32])
        key = _state_key(request)
        self.addCleanup(cache.delete, key)
        self.assertTrue(_load_state(key)["is_demo"])

    def test_invalid_tab_identity_is_rejected(self):
        with self.assertRaises(SuspiciousOperation):
            _state_key(self.request("invalid"))

    def test_stop_and_status_are_scoped_to_the_correct_tab(self):
        first_key = _state_key(self.request("a" * 32))
        job = Mock()
        job.snapshot.return_value = SimpleNamespace(
            rows=[], completed=0, total=1, current_name="", stopping=False, finished=False, error="",
        )
        _ACTIVE_JOBS[first_key] = job
        self.addCleanup(_ACTIVE_JOBS.pop, first_key, None)
        response = ImportStatusView().get(self.request("b" * 32, path="/status/"))
        self.assertIn(b'"active": false', response.content)
        ImportStopView().post(self.request("b" * 32, method="post", path="/stop/"))
        job.stop.assert_not_called()
        ImportStopView().post(self.request("a" * 32, method="post", path="/stop/"))
        job.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
