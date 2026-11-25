from collections import Counter
from io import BytesIO

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

from .importer import (
    DeviceRow, InterfaceRow, ImportProblem, interface_addresses, interface_is_primary, missing_required_values,
)


COLUMNS = ["name", "tenant", "site", "manufacturer", "device_type", "role", "status", "serial", "asset_tag", "description"]
ALIASES = {"device_name": "name", "device_role": "role", "device_status": "status", "model": "device_type"}
INTERFACE_COLUMNS = ["device_name", "tenant", "name", "type", "ip_addresses", "is_primary", "vrf"]


def template_bytes():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Devices"
    sheet.append([*COLUMNS, "cf_PID"])
    sheet.append(["SW-VLN-001", "Pavyzdys", "Vilnius", "Cisco", "C9200L-24T-4G", "Access Switch", "active", "", "", "", "PID-001"])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="146B55")
    for column in sheet.columns:
        sheet.column_dimensions[column[0].column_letter].width = 24
    interfaces = workbook.create_sheet("Interfaces")
    interfaces.append(INTERFACE_COLUMNS)
    interfaces.freeze_panes = "A2"
    for column in interfaces.columns:
        interfaces.column_dimensions[column[0].column_letter].width = 26
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def parse_excel(content, allow_duplicate_ips=False):
    if len(content) > 10 * 1024 * 1024:
        raise ImportProblem("Failas per didelis. Riba: 10 MB.")
    try:
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=False)
    except Exception as error:
        raise ImportProblem("Nepavyko perskaityti .xlsx failo. Patikrinkite Excel formata.") from error
    try:
        sheet = workbook.worksheets[0]
        if sheet.max_column > 30 or sheet.max_row > 5001:
            raise ImportProblem("Vienu kartu galima ikelti iki 5000 eiluciu ir 30 stulpeliu.")
        iterator = sheet.iter_rows()
        first = next(iterator, ())
        headers = []
        for cell in first:
            raw_header = str(cell.value or "").strip().replace(" ", "_")
            header = "cf_" + raw_header[3:] if raw_header.lower().startswith("cf_") else raw_header.lower()
            headers.append(ALIASES.get(header, header))
        named_headers = [header for header in headers if header]
        if len(named_headers) != len(set(named_headers)):
            raise ImportProblem("Excel yra pasikartojanciu stulpeliu pavadinimu.")
        if sum(header.casefold() == "cf_pid" for header in named_headers) > 1:
            raise ImportProblem("Pateikite tik viena PID stulpeli: cf_PID arba cf_pid, ne abu.")
        missing = missing_required_values(dict.fromkeys(named_headers, "present"))
        if missing:
            raise ImportProblem("Truksta stulpeliu: " + ", ".join(sorted(missing)))
        unknown = {header for header in named_headers
               if header not in COLUMNS and not (header.startswith("cf_") and len(header) > 3)}
        if unknown:
            raise ImportProblem("Neatpazinti stulpeliai: " + ", ".join(sorted(unknown)))
        rows = []
        for number, cells in enumerate(iterator, start=2):
            if all(cell.value is None for cell in cells):
                continue
            values = {header: str(cell.value).strip() if cell.value is not None else ""
                      for header, cell in zip(headers, cells) if header}
            row = DeviceRow(number, values)
            errors = []
            if any(cell.data_type == "f" for cell in cells):
                errors.append("Formules neleidziamos. Naudokite paprastas reiksmes.")
            if any(not header and cell.value is not None for header, cell in zip(headers, cells)):
                errors.append("Yra duomenu stulpelyje be pavadinimo.")
            missing_values = missing_required_values(values)
            if missing_values:
                errors.append("Truksta reiksmiu: " + ", ".join(missing_values))
            if errors:
                row.state = "Failo klaida"
                row.error = " ".join(errors)
            rows.append(row)
        duplicates = Counter((row.values.get("site"), row.values.get("name")) for row in rows)
        tags = Counter(row.values.get("asset_tag") for row in rows if row.values.get("asset_tag"))
        pids = [next((value for name, value in row.values.items() if name.casefold() == "cf_pid"), "") for row in rows]
        pid_counts = Counter(pid for pid in pids if pid)
        for row, pid in zip(rows, pids):
            if duplicates[(row.values.get("site"), row.values.get("name"))] > 1:
                row.state = "Failo klaida"
                row.error += " Pasikartojantis name ir site siame faile."
            if row.values.get("asset_tag") and tags[row.values["asset_tag"]] > 1:
                row.state = "Failo klaida"
                row.error += " Pasikartojantis asset_tag siame faile."
            if pid and pid_counts[pid] > 1:
                row.state = "Failo klaida"
                row.error += " Pasikartojantis cf_PID siame faile. PID turi buti unikalus."
        if not rows:
            raise ImportProblem("Faile nera irenginiu eiluciu.")
        if "Interfaces" in workbook.sheetnames:
            parse_interfaces(workbook["Interfaces"], rows, allow_duplicate_ips)
        return rows
    finally:
        workbook.close()


def parse_interfaces(sheet, rows, allow_duplicate_ips=False):
    if sheet.max_column > 30 or sheet.max_row > 5001:
        raise ImportProblem("Interfaces: riba 5000 eiluciu ir 30 stulpeliu.")
    iterator = sheet.iter_rows()
    headers = ["_".join(str(cell.value or "").split()).casefold() for cell in next(iterator, ())]
    named = [header for header in headers if header]
    duplicates = sorted(header for header, count in Counter(named).items() if count > 1)
    missing = sorted(set(INTERFACE_COLUMNS) - set(named))
    unknown = sorted(set(named) - set(INTERFACE_COLUMNS))
    if duplicates or missing or unknown:
        details = []
        if missing:
            details.append("truksta: " + ", ".join(missing))
        if duplicates:
            details.append("pasikartoja: " + ", ".join(duplicates))
        if unknown:
            details.append("neatpazinti stulpeliai: " + ", ".join(unknown))
        raise ImportProblem("Interfaces: netinkamos antrastes (" + "; ".join(details) + "). "
                            "Reikalingi unikalus stulpeliai: " + ", ".join(INTERFACE_COLUMNS))
    devices = {}
    for row in rows:
        devices.setdefault((row.values.get("name"), row.values.get("tenant")), []).append(row)
    names = set()
    addresses = {}
    primary_interfaces = {}
    for number, cells in enumerate(iterator, start=2):
        if all(cell.value is None for cell in cells):
            continue
        values = {header: str(cell.value).strip() if cell.value is not None else ""
                  for header, cell in zip(headers, cells) if header}
        key = (values.get("device_name"), values.get("tenant"))
        matches = devices.get(key, [])
        if len(matches) != 1:
            raise ImportProblem(f"Interfaces eil. {number}: device_name ir tenant neatitinka vieno Devices iraso.")
        row = matches[0]
        row.interfaces.append(InterfaceRow(number, values))
        try:
            if any(cell.data_type == "f" for cell in cells):
                raise ImportProblem("Formules neleidziamos.")
            if any(not header and cell.value is not None for header, cell in zip(headers, cells)):
                raise ImportProblem("Yra duomenu stulpelyje be pavadinimo.")
            required = ["device_name", "tenant", "name", "type"]
            if values.get("ip_addresses"):
                required.append("vrf")
            missing = [name for name in required if not values.get(name)]
            if missing:
                raise ImportProblem("Truksta reiksmiu: " + ", ".join(missing))
            is_primary = interface_is_primary(values.get("is_primary", ""))
            parsed_addresses = interface_addresses(values.get("ip_addresses", ""))
            if is_primary:
                if len(parsed_addresses) != 1 or parsed_addresses[0].version != 4:
                    raise ImportProblem("is_primary=TRUE reikalauja vieno IPv4 adreso ip_addresses langelyje.")
                previous_line = primary_interfaces.get(id(row))
                if previous_line:
                    raise ImportProblem(
                        f"Device turi daugiau nei viena primary IPv4 (Interfaces eil. {previous_line}).")
                primary_interfaces[id(row)] = number
            interface_key = (*key, values["name"])
            if interface_key in names:
                raise ImportProblem("Pasikartojantis interface tam paciam device.")
            names.add(interface_key)
            device_key = (row.values.get("name"), row.values.get("tenant"))
            for address in parsed_addresses:
                address_key = (row.values.get("tenant"), values.get("vrf"), str(address.ip))
                previous_device = addresses.get(address_key)
                if previous_device is not None and previous_device != device_key and not allow_duplicate_ips:
                    raise ImportProblem(f"Pasikartojantis IP {address.ip} tame paciame VRF faile.")
                addresses.setdefault(address_key, device_key)
        except ImportProblem as error:
            row.state = "Failo klaida"
            row.error += f" Interfaces eil. {number}: {error}"


def report_bytes(rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Rezultatai"
    custom_columns = sorted({name for row in rows for name in row.values if name.startswith("cf_")})
    columns = [*COLUMNS, *custom_columns]
    sheet.append(["excel_row", *columns, "result", "error", "netbox_id", "old_pid_devices", "delete_old_device", "deleted_device_id"])
    for row in rows:
        old_devices = "; ".join(f"{device['id']}: {device['name']}" for device in row.pid_matches)
        state = "Paruošta importavimui" if row.state in {"Paruosta", "Paruošta"} else row.state
        values = [row.excel_row, *(row.values.get(name, "") for name in columns), state, row.error, row.device_id,
                  old_devices, row.delete_old, row.deleted_device_id]
        sheet.append(values)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    sheet.freeze_panes = "A2"
    interfaces = workbook.create_sheet("Interfaces")
    interfaces.append(INTERFACE_COLUMNS)
    for row in rows:
        for interface in row.interfaces:
            interfaces.append([interface.values.get(name, "") for name in INTERFACE_COLUMNS])
            for cell in interfaces[interfaces.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
