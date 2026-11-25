import unittest
from io import BytesIO
from threading import Event
from unittest.mock import Mock

from openpyxl import Workbook, load_workbook

from netbox_device_importer.excel_io import parse_excel, report_bytes, template_bytes
from netbox_device_importer.importer import (
    DeviceRow,
    ImportJob,
    ImportProblem,
    InterfaceRow,
    UncertainResult,
    import_row,
    rows_from_list,
    rows_to_list,
    set_delete_choice,
    validate_row,
)


class FakeAPI:
    def __init__(self):
        self.created = []

    def prepare(self, values, interfaces=None):
        if values["name"] == "bad":
            raise ImportProblem("Nerastas site")
        return dict(values)

    def create(self, payload):
        if payload["name"] == "timeout":
            raise UncertainResult("Patikrinkite NetBox")
        self.created.append(payload)
        return {"id": len(self.created)}


class ImportTests(unittest.TestCase):
    def test_checkbox_cannot_revive_uncertain_or_completed_operations(self):
        for state in ("Neaiškus rezultatas", "Sukurta", "Klaida", "Failo klaida"):
            with self.subTest(state=state):
                row = DeviceRow(
                    2,
                    {"name": "New"},
                    state=state,
                    error="Original error",
                    pid_matches=[{"id": 91}],
                    delete_old=True,
                )
                set_delete_choice(row, True)
                self.assertEqual(row.state, state)
                self.assertEqual(row.error, "Original error")
                self.assertFalse(row.delete_old)

    def test_pid_conflict_requires_explicit_choice_for_one_device(self):
        api = FakeAPI()
        api.prepare = Mock(return_value={"name": "New", "_pid_matches": [{"id": 91}]})
        row = DeviceRow(2, {"name": "New"})
        validate_row(row, api)
        self.assertEqual(row.state, "PID konfliktas")
        self.assertFalse(row.delete_old)
        import_row(row, api)
        self.assertEqual(api.created, [])
        self.assertTrue(set_delete_choice(row, True))
        self.assertEqual(row.state, "Paruosta")
        self.assertTrue(set_delete_choice(row, False))
        self.assertEqual(row.state, "PID konfliktas")

    def test_multiple_matches_cannot_be_approved(self):
        row = DeviceRow(2, {"name": "New"}, state="PID konfliktas", pid_matches=[{"id": 91}, {"id": 92}])
        set_delete_choice(row, True)
        self.assertFalse(row.delete_old)
        self.assertEqual(row.state, "PID konfliktas")

    def test_error_is_local_and_success_is_not_repeated(self):
        api = FakeAPI()
        rows = [DeviceRow(2, {"name": "bad"}), DeviceRow(3, {"name": "good"})]
        for row in rows:
            validate_row(row, api)
            import_row(row, api)
        self.assertEqual(rows[0].state, "Klaida")
        self.assertEqual(rows[1].state, "Sukurta")
        self.assertEqual(rows[0].excel_row, 2)
        validate_row(rows[1], api)
        import_row(rows[1], api)
        self.assertEqual(len(api.created), 1)

    def test_uncertain_result_cannot_be_retried(self):
        api = FakeAPI()
        row = DeviceRow(2, {"name": "timeout"})
        validate_row(row, api)
        import_row(row, api)
        self.assertEqual(row.state, "Neaiškus rezultatas")
        validate_row(row, api)
        import_row(row, api)
        self.assertEqual(row.state, "Neaiškus rezultatas")
        self.assertEqual(api.created, [])

    def test_file_error_cannot_be_cleared_by_validation(self):
        row = DeviceRow(2, {"name": "good"}, state="Failo klaida", error="Formula")
        validate_row(row, FakeAPI())
        self.assertEqual(row.state, "Failo klaida")

    def test_rows_serialization_roundtrip(self):
        row = DeviceRow(
            2,
            {"name": "SW-1", "cf_PID": "P1"},
            state="Paruosta",
            interfaces=[InterfaceRow(2, {"device_name": "SW-1", "name": "eth0", "type": "1000base-t"})],
        )
        restored = rows_from_list(rows_to_list([row]))
        self.assertEqual(restored, [row])


class ImportJobTests(unittest.TestCase):
    def make_api(self):
        api = FakeAPI()
        api.connect = Mock(return_value=["active"])
        api.close = Mock()
        return api

    def test_stop_finishes_current_device_and_resume_skips_completed_rows(self):
        api = self.make_api()
        entered = Event()
        release = Event()
        original_create = api.create

        def create(payload):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release device operation")
            return original_create(payload)

        api.create = create
        rows = [
            DeviceRow(2, {"name": "first"}, state="Paruosta"),
            DeviceRow(3, {"name": "second"}, state="Paruosta"),
        ]
        job = ImportJob(rows)
        job.start(lambda: api)
        try:
            self.assertTrue(entered.wait(5))
            job.stop()
            progress = job.snapshot()
            self.assertTrue(progress.stopping)
            self.assertFalse(progress.finished)
            self.assertEqual(progress.current_name, "first")
            self.assertEqual(progress.completed, 0)
        finally:
            release.set()
        self.assertTrue(job.wait(5))
        progress = job.snapshot()
        self.assertEqual(progress.completed, 1)
        self.assertEqual([row.state for row in progress.rows], ["Sukurta", "Paruosta"])
        self.assertEqual([item["name"] for item in api.created], ["first"])
        self.assertTrue(all(row.state == "Paruosta" for row in rows))
        api.close.assert_called_once()
        resumed = ImportJob(progress.rows)
        resumed.start(lambda: api)
        self.assertTrue(resumed.wait(5))
        self.assertEqual([item["name"] for item in api.created], ["first", "second"])

    def test_stop_before_start_does_not_connect_or_write(self):
        factory = Mock()
        job = ImportJob([DeviceRow(2, {"name": "first"}, state="Paruosta")])
        job.stop()
        job.start(factory)
        self.assertTrue(job.wait(5))
        factory.assert_not_called()
        self.assertEqual(job.snapshot().completed, 0)

    def test_row_error_does_not_stop_following_devices(self):
        api = self.make_api()
        job = ImportJob(
            [
                DeviceRow(2, {"name": "bad"}, state="Paruosta"),
                DeviceRow(3, {"name": "good"}, state="Paruosta"),
            ]
        )
        job.start(lambda: api)
        self.assertTrue(job.wait(5))
        self.assertEqual([row.state for row in job.snapshot().rows], ["Klaida", "Sukurta"])
        self.assertEqual(job.snapshot().completed, 2)
        self.assertFalse(job.snapshot().stopping)

    def test_connection_error_preserves_unstarted_rows_and_closes_client(self):
        api = self.make_api()
        api.connect.side_effect = ImportProblem("No connection")
        job = ImportJob([DeviceRow(2, {"name": "first"}, state="Paruosta")])
        job.start(lambda: api)
        self.assertTrue(job.wait(5))
        progress = job.snapshot()
        self.assertEqual(progress.rows[0].state, "Paruosta")
        self.assertEqual(progress.completed, 0)
        self.assertEqual(progress.error, "No connection")
        api.close.assert_called_once()

    def test_resume_never_retries_uncertain_device(self):
        api = self.make_api()
        first = ImportJob([DeviceRow(2, {"name": "timeout"}, state="Paruosta")])
        first.start(lambda: api)
        self.assertTrue(first.wait(5))
        progress = first.snapshot()
        self.assertEqual(progress.rows[0].state, "Neaiškus rezultatas")
        resumed = ImportJob([*progress.rows, DeviceRow(3, {"name": "good"}, state="Paruosta")])
        resumed.start(lambda: api)
        self.assertTrue(resumed.wait(5))
        self.assertEqual(resumed.snapshot().total, 1)
        self.assertEqual([item["name"] for item in api.created], ["good"])


class ExcelTests(unittest.TestCase):
    def test_report_displays_ready_for_import_label(self):
        for state in ("Paruosta", "Paruošta"):
            with self.subTest(state=state):
                workbook = load_workbook(BytesIO(report_bytes([DeviceRow(2, {}, state=state)])))
                headers = [cell.value for cell in workbook.active[1]]
                self.assertEqual(
                    workbook.active.cell(2, headers.index("result") + 1).value,
                    "Paruošta importavimui",
                )

    def interface_workbook(self, values=None):
        workbook = load_workbook(BytesIO(template_bytes()))
        workbook["Interfaces"].append(
            values
            or [
                "SW-VLN-001",
                "Pavyzdys",
                "Loopback100",
                "virtual",
                "192.0.2.1/32;2001:db8::1/128",
                "",
                "Customer-VRF",
            ]
        )
        return workbook

    def parse_workbook(self, workbook, allow_duplicate_ips=False):
        content = BytesIO()
        workbook.save(content)
        return parse_excel(content.getvalue(), allow_duplicate_ips)

    def test_interfaces_roundtrip_and_legacy_file(self):
        workbook = self.interface_workbook()
        rows = self.parse_workbook(workbook)
        self.assertEqual(rows[0].state, "Laukia patikros")
        self.assertEqual(rows[0].interfaces[0].excel_row, 2)
        report = load_workbook(BytesIO(report_bytes(rows)))
        self.assertEqual(report["Interfaces"]["E2"].value, "192.0.2.1/32;2001:db8::1/128")
        del workbook["Interfaces"]
        self.assertEqual(self.parse_workbook(workbook)[0].interfaces, [])

    def test_interface_template_includes_primary_flag(self):
        workbook = load_workbook(BytesIO(template_bytes()))
        self.assertEqual(
            [cell.value for cell in workbook["Interfaces"][1]],
            ["device_name", "tenant", "name", "type", "ip_addresses", "is_primary", "vrf"],
        )

    def test_primary_flag_requires_one_ipv4_and_is_unique_per_device(self):
        invalid_rows = [
            ["SW-VLN-001", "Pavyzdys", "Loopback100", "virtual", "", "TRUE", ""],
            ["SW-VLN-001", "Pavyzdys", "Loopback100", "virtual", "2001:db8::1/128", "TRUE", "Customer-VRF"],
            ["SW-VLN-001", "Pavyzdys", "Loopback100", "virtual", "192.0.2.1/32;192.0.2.2/32", "TRUE", "Customer-VRF"],
        ]
        for values in invalid_rows:
            with self.subTest(values=values):
                row = self.parse_workbook(self.interface_workbook(values))[0]
                self.assertEqual(row.state, "Failo klaida")
                self.assertIn("is_primary=TRUE", row.error)

        workbook = self.interface_workbook(
            ["SW-VLN-001", "Pavyzdys", "Loopback100", "virtual", "192.0.2.1/32", "TRUE", "Customer-VRF"]
        )
        workbook["Interfaces"].append(
            ["SW-VLN-001", "Pavyzdys", "Loopback200", "virtual", "192.0.2.2/32", "true", "Customer-VRF"]
        )
        row = self.parse_workbook(workbook)[0]
        self.assertEqual(row.state, "Failo klaida")
        self.assertIn("daugiau nei viena primary IPv4", row.error)

    def test_primary_flag_accepts_true_false_or_blank_only(self):
        for flag in ("TRUE", "FALSE", ""):
            with self.subTest(flag=flag):
                row = self.parse_workbook(
                    self.interface_workbook(
                        ["SW-VLN-001", "Pavyzdys", "Loopback100", "virtual", "192.0.2.1/32", flag, "Customer-VRF"]
                    )
                )[0]
                self.assertEqual(row.state, "Laukia patikros")
        row = self.parse_workbook(
            self.interface_workbook(
                ["SW-VLN-001", "Pavyzdys", "Loopback100", "virtual", "192.0.2.1/32", "yes", "Customer-VRF"]
            )
        )[0]
        self.assertEqual(row.state, "Failo klaida")
        self.assertIn("TRUE, FALSE arba tuscias", row.error)

    def test_invalid_interface_data_is_a_file_error(self):
        for column, value in [
            (3, ""),
            (4, ""),
            (5, "192.0.2.1"),
            (5, "bad/32"),
            (5, "192.0.2.1/32;192.0.2.1/24"),
            (6, "sometimes"),
            (3, "=1+1"),
        ]:
            with self.subTest(column=column, value=value):
                workbook = self.interface_workbook()
                workbook["Interfaces"].cell(2, column).value = value
                row = self.parse_workbook(workbook)[0]
                self.assertEqual(row.state, "Failo klaida")
                self.assertIn("Interfaces eil. 2", row.error)

    def test_unknown_device_and_bad_headers_are_rejected(self):
        for column, value, message in [
            (1, "Missing", "device_name"),
            (2, "Missing", "device_name ir tenant"),
        ]:
            workbook = self.interface_workbook()
            workbook["Interfaces"].cell(2, column, value)
            with self.assertRaisesRegex(ImportProblem, message):
                self.parse_workbook(workbook)
        workbook = self.interface_workbook()
        workbook["Interfaces"]["F1"] = "unknown"
        with self.assertRaisesRegex(ImportProblem, "neatpazinti stulpeliai: unknown"):
            self.parse_workbook(workbook)

    def test_interface_header_error_reports_missing_duplicate_and_unknown(self):
        workbook = self.interface_workbook()
        workbook["Interfaces"]["F1"] = "tenant"
        workbook["Interfaces"]["G1"] = "comment"
        with self.assertRaisesRegex(
            ImportProblem,
            "truksta: is_primary, vrf; pasikartoja: tenant; neatpazinti stulpeliai: comment",
        ):
            self.parse_workbook(workbook)

    def test_interface_headers_normalize_whitespace_and_case(self):
        workbook = self.interface_workbook()
        headers = [" Device\u00a0Name ", "TENANT", " Name ", "Type", "IP\nAddresses", "IS_PRIMARY", "VRF"]
        for column, value in enumerate(headers, start=1):
            workbook["Interfaces"].cell(1, column, value)
        rows = self.parse_workbook(workbook)
        self.assertEqual(rows[0].state, "Laukia patikros")
        self.assertEqual(rows[0].interfaces[0].values["device_name"], "SW-VLN-001")

    def test_duplicate_interface_is_rejected(self):
        workbook = self.interface_workbook()
        workbook["Interfaces"].append(
            ["SW-VLN-001", "Pavyzdys", "Loopback100", "virtual", "192.0.2.2/32", "", "Customer-VRF"]
        )
        row = self.parse_workbook(workbook)[0]
        self.assertEqual(row.state, "Failo klaida")
        self.assertIn("Pasikartojantis interface", row.error)

    def test_duplicate_ips_between_devices_in_same_vrf_require_permission(self):
        workbook = self.interface_workbook()
        duplicate_device = [cell.value for cell in workbook["Devices"][2]]
        duplicate_device[0] = "SW-VLN-002"
        duplicate_device[-1] = "PID-002"
        workbook["Devices"].append(duplicate_device)
        workbook["Interfaces"].append(
            ["SW-VLN-002", "Pavyzdys", "Loopback100", "virtual", "192.0.2.1/32", "", "Customer-VRF"]
        )
        rows = self.parse_workbook(workbook)
        self.assertEqual(rows[1].state, "Failo klaida")
        self.assertIn("Pasikartojantis IP", rows[1].error)

        rows = self.parse_workbook(workbook, allow_duplicate_ips=True)
        self.assertEqual([row.state for row in rows], ["Laukia patikros", "Laukia patikros"])

    def test_interface_without_ip_does_not_require_vrf(self):
        workbook = self.interface_workbook(
            ["SW-VLN-001", "Pavyzdys", "Ethernet1", "1000base-t", "", "", ""]
        )
        self.assertEqual(self.parse_workbook(workbook)[0].state, "Laukia patikros")

    def test_duplicate_pid_is_blocked_across_sites_and_tenants(self):
        workbook = load_workbook(BytesIO(template_bytes()))
        sheet = workbook.active
        sheet.append([cell.value for cell in sheet[2]])
        headers = [cell.value for cell in sheet[1]]
        sheet.cell(3, 1, "Other-device")
        sheet.cell(3, headers.index("site") + 1, "Other-site")
        sheet.cell(3, headers.index("tenant") + 1, "Other-tenant")
        content = BytesIO()
        workbook.save(content)
        rows = parse_excel(content.getvalue())
        self.assertTrue(all(row.state == "Failo klaida" and "cf_PID" in row.error for row in rows))

    def test_report_records_old_device_and_confirmed_deletion(self):
        row = DeviceRow(
            2,
            {"name": "New", "cf_PID": "TEST"},
            deleted_device_id=91,
            pid_matches=[{"id": 91, "name": "Old"}],
        )
        report = load_workbook(BytesIO(report_bytes([row])))
        headers = [cell.value for cell in report.active[1]]
        self.assertEqual(report.active.cell(2, headers.index("deleted_device_id") + 1).value, 91)
        self.assertEqual(report.active.cell(2, headers.index("old_pid_devices") + 1).value, "91: Old")

    def test_tenant_and_pid_are_required_per_row(self):
        workbook = load_workbook(BytesIO(template_bytes()))
        sheet = workbook.active
        headers = [cell.value for cell in sheet[1]]
        sheet.append([cell.value for cell in sheet[2]])
        sheet.cell(3, 1, "SW-VLN-002")
        sheet.cell(2, headers.index("tenant") + 1).value = "  "
        sheet.cell(2, headers.index("cf_PID") + 1).value = None
        content = BytesIO()
        workbook.save(content)
        rows = parse_excel(content.getvalue())
        self.assertEqual(rows[0].state, "Failo klaida")
        self.assertIn("tenant", rows[0].error)
        self.assertIn("cf_PID", rows[0].error)
        self.assertEqual(rows[1].state, "Laukia patikros")

    def test_tenant_and_pid_headers_are_required(self):
        for name in ("tenant", "cf_PID"):
            with self.subTest(name=name):
                workbook = load_workbook(BytesIO(template_bytes()))
                sheet = workbook.active
                headers = [cell.value for cell in sheet[1]]
                sheet.delete_cols(headers.index(name) + 1)
                content = BytesIO()
                workbook.save(content)
                with self.assertRaisesRegex(ImportProblem, name):
                    parse_excel(content.getvalue())

    def test_custom_field_columns_are_preserved(self):
        workbook = load_workbook(BytesIO(template_bytes()))
        sheet = workbook.active
        for column, (name, value) in enumerate(
            [("cf_bgp_peer_ip", "192.0.2.1"), ("cf_pid", "ABC-123"), ("cf_speed_mbps", 1000)],
            start=11,
        ):
            sheet.cell(1, column, name)
            sheet.cell(2, column, value)
        content = BytesIO()
        workbook.save(content)
        row = parse_excel(content.getvalue())[0]
        self.assertEqual(row.values["cf_bgp_peer_ip"], "192.0.2.1")
        self.assertEqual(row.values["cf_pid"], "ABC-123")
        self.assertEqual(row.values["cf_speed_mbps"], "1000")
        report = load_workbook(BytesIO(report_bytes([row])))
        headers = [cell.value for cell in report.active[1]]
        self.assertEqual(report.active.cell(2, headers.index("cf_speed_mbps") + 1).value, "1000")

    def test_template_roundtrip(self):
        rows = parse_excel(template_bytes())
        self.assertEqual(rows[0].excel_row, 2)
        self.assertEqual(rows[0].values["name"], "SW-VLN-001")
        self.assertTrue(rows[0].values["tenant"])
        self.assertTrue(rows[0].values["cf_PID"])
        self.assertEqual(rows[0].state, "Laukia patikros")

    def test_duplicate_rows_are_both_blocked(self):
        workbook = load_workbook(BytesIO(template_bytes()))
        sheet = workbook.active
        sheet.append([cell.value for cell in sheet[2]])
        content = BytesIO()
        workbook.save(content)
        rows = parse_excel(content.getvalue())
        self.assertTrue(all(row.state == "Failo klaida" for row in rows))

    def test_formula_is_blocked_and_report_is_not_executable(self):
        workbook = load_workbook(BytesIO(template_bytes()))
        workbook.active["A2"] = '=HYPERLINK("https://example.com")'
        content = BytesIO()
        workbook.save(content)
        rows = parse_excel(content.getvalue())
        self.assertEqual(rows[0].state, "Failo klaida")
        report = load_workbook(BytesIO(report_bytes(rows)))
        self.assertEqual(report.active["B2"].data_type, "s")

    def test_missing_header_is_actionable(self):
        workbook = Workbook()
        workbook.active.append(["name"])
        content = BytesIO()
        workbook.save(content)
        with self.assertRaisesRegex(ImportProblem, "Truksta stulpeliu"):
            parse_excel(content.getvalue())
