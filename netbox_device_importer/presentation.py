from collections import Counter

from .importer import DeviceRow

LABELS = {
    "Laukia patikros": "Laukia patikros",
    "Paruosta": "Paruošta importavimui",
    "Paruošta": "Paruošta importavimui",
    "Sukurta": "Sukurta",
    "Klaida": "Klaida",
    "Failo klaida": "Failo klaida",
    "Neaiskus rezultatas": "Neaiškus rezultatas",
    "Neaiškus rezultatas": "Neaiškus rezultatas",
    "PID konfliktas": "PID konfliktas",
}

FILTER_OPTIONS = ["Visos", "Su klaidomis", "Paruošta importavimui", "Sukurta"]


def demo_rows() -> list[DeviceRow]:
    result = []
    for number, (name, site, model, state, error) in enumerate(
        [
            ("SW-VLN-001", "Vilnius", "C9200L-24T-4G", "Paruosta", ""),
            ("SW-VLN-002", "Vilnius", "C9200L-24T-4G", "Paruosta", ""),
            ("SW-KNS-001", "Kaunas", "C9300-48P", "Klaida", "Device type 'C9300-48P': nerastas."),
            ("RTR-KLP-001", "Klaipėda", "ISR4331", "Paruosta", ""),
            ("SW-SIA-001", "Šiauliai", "C9200L-24T-4G", "Klaida", "Toks device name siame site jau egzistuoja."),
            ("SW-PNV-001", "Panevėžys", "C9200L-24T-4G", "Paruosta", ""),
        ],
        start=2,
    ):
        result.append(
            DeviceRow(
                number,
                {
                    "name": name,
                    "tenant": "Pavyzdys",
                    "site": site,
                    "manufacturer": "Cisco",
                    "device_type": model,
                    "role": "Access Switch",
                    "status": "active",
                    "cf_PID": f"PID-{number - 1:03d}",
                },
                state=state,
                error=error,
                payload={"_site_name": site} if site == "Klaipėda" else {},
            )
        )
    result[1].pid_matches = [
        {
            "id": 9002,
            "name": "SW-OLD-002",
            "site": {"id": 1, "name": "Vilnius"},
            "tenant": {"id": 1, "name": "Pavyzdys"},
            "last_updated": "2026-10-26T10:00:00Z",
            "pid_name": "PID",
            "pid_value": "PID-002",
        }
    ]
    result[1].state = "PID konfliktas"
    result[1].error = "PID jau naudojamas. Nepatvirtintas seno device istrynimas."
    for row in result:
        row.payload["_pid_matches"] = row.pid_matches
    return result


def row_metrics(rows: list[DeviceRow]) -> dict[str, int]:
    counts = Counter(row.state for row in rows)
    return {
        "total": len(rows),
        "ready": counts["Paruosta"] + counts["Paruošta"],
        "errors": (
            counts["Klaida"]
            + counts["Failo klaida"]
            + counts["Neaiskus rezultatas"]
            + counts["Neaiškus rezultatas"]
            + counts["PID konfliktas"]
        ),
        "created": counts["Sukurta"],
        "uncertain": counts["Neaiskus rezultatas"] + counts["Neaiškus rezultatas"],
        "delete_count": sum(1 for row in rows if row.delete_old),
    }


def table_data(rows: list[DeviceRow], selected_filter: str = "Visos", search: str = "") -> list[dict]:
    data = []
    for row in rows:
        if selected_filter == "Su klaidomis" and row.state not in {
            "Klaida",
            "Failo klaida",
            "Neaiskus rezultatas",
            "Neaiškus rezultatas",
            "PID konfliktas",
        }:
            continue
        if selected_filter == "Paruošta importavimui" and row.state not in {"Paruosta", "Paruošta"}:
            continue
        if selected_filter == "Sukurta" and row.state != "Sukurta":
            continue
        if search and search.casefold() not in " ".join(row.values.values()).casefold():
            continue

        site_state = "Site NetBox jau yra" if row.payload.get("site") else ""
        if row.payload.get("_site_name"):
            site_state = (
                "Reikia patikrinti"
                if row.state in {"Neaiskus rezultatas", "Neaiškus rezultatas"}
                else "Bus sukurtas"
            )
        pid_state = "Nepatikrintas"
        if "_pid_matches" in row.payload:
            pid_state = "PID NetBox jau yra" if row.pid_matches else "PID NetBox nėra"
            if len(row.pid_matches) > 1:
                pid_state = "Rasti keli device"
        elif "custom_fields" in row.payload:
            pid_state = "Patikros klaida"
        if row.state == "Sukurta":
            pid_state = "Importuotas"

        old_devices = "; ".join(device["name"] for device in row.pid_matches)
        existing_devices = row.payload.get("_existing_devices")
        netbox_id = row.device_id
        display_state = LABELS.get(row.state, row.state)
        if netbox_id is None and existing_devices is not None and len(existing_devices) == 1:
            netbox_id = existing_devices[0]["id"]
            if row.state not in {"Neaiskus rezultatas", "Neaiškus rezultatas"}:
                display_state = "Įrenginys Netbox jau yra"

        can_delete = row.state in {"Paruosta", "Paruošta", "PID konfliktas"} and len(row.pid_matches) == 1
        custom_fields = {
            name: value
            for name, value in row.values.items()
            if name.startswith("cf_") and name.casefold() != "cf_pid"
        }
        interfaces_str = "; ".join(
            f"{interface.values.get('name', '')}: {interface.values.get('ip_addresses', '')}"
            + (f" (VRF {interface.values['vrf']})" if interface.values.get("vrf") else "")
            for interface in row.interfaces
        )
        data.append(
            {
                "excel_row": row.excel_row,
                "netbox_id": netbox_id,
                "state": row.state,
                "display_state": display_state,
                "name": row.values.get("name", ""),
                "delete_old": row.delete_old,
                "can_delete": can_delete,
                "old_devices": old_devices,
                "pid_state": pid_state,
                "site": row.values.get("site", ""),
                "site_state": site_state,
                "tenant": row.values.get("tenant", ""),
                "manufacturer": row.values.get("manufacturer", ""),
                "device_type": row.values.get("device_type", ""),
                "role": row.values.get("role", ""),
                "status": row.values.get("status") or "active",
                "description": row.values.get("description", ""),
                "custom_fields": custom_fields,
                "interfaces": interfaces_str,
                "error": row.error,
            }
        )
    return data
