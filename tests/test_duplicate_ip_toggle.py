from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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
from django.http import HttpResponseRedirect
from django.test import RequestFactory
from openpyxl import Workbook

from netbox_device_importer.excel_io import COLUMNS, INTERFACE_COLUMNS
from netbox_device_importer.views import DeviceImportView


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


if __name__ == "__main__":
    unittest.main()
