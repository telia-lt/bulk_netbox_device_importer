import json
import math
import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_interface
from typing import Any

from .importer import (
    ImportProblem,
    PreparationProblem,
    interface_addresses,
    interface_is_primary,
    missing_required_values,
)


@dataclass
class NetBoxModels:
    Device: Any
    Site: Any
    Manufacturer: Any
    DeviceType: Any
    DeviceRole: Any
    Interface: Any
    InterfaceTemplate: Any
    Tenant: Any
    VRF: Any
    IPAddress: Any
    CustomField: Any
    atomic: Any
    close_old_connections: Any
    ValidationError: Any


def load_netbox_models() -> NetBoxModels:
    from dcim.models import Device, DeviceRole, DeviceType, Interface, InterfaceTemplate, Manufacturer, Site
    from django.core.exceptions import ValidationError
    from django.db import close_old_connections, transaction
    from extras.models import CustomField
    from ipam.models import IPAddress, VRF
    from tenancy.models import Tenant

    return NetBoxModels(
        Device=Device,
        Site=Site,
        Manufacturer=Manufacturer,
        DeviceType=DeviceType,
        DeviceRole=DeviceRole,
        Interface=Interface,
        InterfaceTemplate=InterfaceTemplate,
        Tenant=Tenant,
        VRF=VRF,
        IPAddress=IPAddress,
        CustomField=CustomField,
        atomic=transaction.atomic,
        close_old_connections=close_old_connections,
        ValidationError=ValidationError,
    )


def _choice_values(choices) -> set[str]:
    values = set()
    for item in choices or ():
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            key, label = item[0], item[1]
            if isinstance(label, (list, tuple)):
                values.update(_choice_values(label))
            elif key not in (None, ""):
                values.add(str(key))
    return values


def _format_validation_error(error: Exception) -> str:
    if hasattr(error, "message_dict"):
        parts = []
        for field_name, messages in error.message_dict.items():
            joined = "; ".join(str(msg) for msg in messages)
            parts.append(f"{field_name}: {joined}")
        return " | ".join(parts)
    if hasattr(error, "messages"):
        return "; ".join(str(msg) for msg in error.messages)
    return str(error)


class NetBoxORMBackend:
    def __init__(self, allow_duplicate_ips=False, user=None, models: NetBoxModels | None = None):
        self.allow_duplicate_ips = allow_duplicate_ips
        self.user = user
        self._models = models
        self.cache: dict[Any, Any] = {}
        self.statuses: set[str] | None = None
        self.custom_field_definitions: dict[str, Any] | None = None
        self.interface_types: set[str] | None = None

    @property
    def models(self) -> NetBoxModels:
        if self._models is None:
            self._models = load_netbox_models()
        return self._models

    def _require_perm(self, perm: str, message: str) -> None:
        if self.user is not None and not self.user.has_perm(perm):
            raise ImportProblem(message)

    def close(self):
        if self._models is not None and self._models.close_old_connections is not None:
            self._models.close_old_connections()

    def connect(self):
        self._require_perm("dcim.add_device", "Neturite teisės kurti įrenginių (dcim.add_device).")
        status_field = self.models.Device._meta.get_field("status")
        self.statuses = _choice_values(status_field.choices)
        if not self.statuses:
            raise ImportProblem("Nepavyko gauti leistinu device status reiksmiu.")
        return sorted(self.statuses)

    def lookup(self, model, name: str, label: str, allow_missing=False, **filters):
        key = (model.__name__, name, tuple(sorted(filters.items())))
        if key not in self.cache:
            matches = [item for item in model.objects.filter(name=name, **filters) if getattr(item, "name", None) == name]
            if not matches and allow_missing:
                return None
            if len(matches) != 1:
                reason = "nerastas" if not matches else "rasti keli vienodi pavadinimai"
                raise ImportProblem(f"{label} '{name}': {reason}.")
            self.cache[key] = matches[0].id
        return self.cache[key]

    def custom_field_values(self, values):
        columns = {name[3:]: value for name, value in values.items() if name.startswith("cf_")}
        if not columns:
            return {}
        if self.custom_field_definitions is None:
            qs = self.models.CustomField.objects.filter(object_types__app_label="dcim", object_types__model="device")
            self.custom_field_definitions = {item.name: item for item in qs}
        result = {}
        for name, value in columns.items():
            definition = self.custom_field_definitions.get(name)
            if definition is None:
                raise ImportProblem(f"cf_{name}: custom field nerastas arba nepriskirtas dcim.device.")
            if not value:
                continue
            field_type = definition.type
            try:
                if field_type in {"integer", "object"}:
                    converted = int(value)
                elif field_type == "decimal":
                    converted = float(value)
                    if not math.isfinite(converted):
                        raise ValueError("not finite")
                elif field_type == "boolean":
                    choices = {"true": True, "false": False, "1": True, "0": False, "yes": True, "no": False}
                    converted = choices[value.lower()]
                elif field_type in {"json", "multiselect", "multiobject"}:
                    converted = json.loads(value)
                    if field_type == "multiselect" and (
                        not isinstance(converted, list) or any(not isinstance(item, str) for item in converted)
                    ):
                        raise ValueError("expected string list")
                    if field_type == "multiobject" and (
                        not isinstance(converted, list) or any(type(item) is not int for item in converted)
                    ):
                        raise ValueError("expected integer list")
                elif field_type in {"text", "longtext", "url", "select", "date", "datetime"}:
                    converted = value
                else:
                    raise ImportProblem(f"cf_{name}: nepalaikomas lauko tipas '{field_type}'.")
            except (ValueError, KeyError) as error:
                raise ImportProblem(f"cf_{name}: reiksme '{value}' netinka lauko tipui '{field_type}'.") from error
            result[name] = converted
        return result

    def pid_snapshot(self, device, pid_name: str):
        site = getattr(device, "site", None)
        tenant = getattr(device, "tenant", None)
        last_updated = getattr(device, "last_updated", None)
        if hasattr(last_updated, "isoformat"):
            last_updated = last_updated.isoformat()
        cf_data = getattr(device, "custom_field_data", None) or {}
        return {
            "id": device.id,
            "name": getattr(device, "name", "") or "",
            "site": {"id": site.id, "name": site.name} if site else None,
            "tenant": {"id": tenant.id, "name": tenant.name} if tenant else None,
            "last_updated": last_updated,
            "pid_name": pid_name,
            "pid_value": cf_data.get(pid_name),
        }

    def pid_devices(self, custom_fields):
        pid_fields = [(name, value) for name, value in custom_fields.items() if name.casefold() == "pid"]
        if len(pid_fields) != 1:
            raise ImportProblem("Turi buti pateiktas vienas cf_PID laukas.")
        pid_name, pid_value = pid_fields[0]
        candidates = self.models.Device.objects.filter(**{f"custom_field_data__{pid_name}": pid_value})
        matches = []
        for device in candidates:
            if (getattr(device, "custom_field_data", None) or {}).get(pid_name) != pid_value:
                continue
            matches.append(self.pid_snapshot(device, pid_name))
        return matches

    def check_device_name(self, name: str, site_id: int, excluded_ids=()):
        existing = self.models.Device.objects.filter(name=name, site_id=site_id)
        if any(
            getattr(item, "name", None) == name
            and getattr(item, "site_id", None) == site_id
            and item.id not in excluded_ids
            for item in existing
        ):
            raise ImportProblem("Toks device name siame site jau egzistuoja. Esamas irasas nekeiciamas.")

    def prepare_interfaces(self, interfaces, tenant_id: int, device_type_id: int):
        if self.interface_types is None:
            type_field = self.models.Interface._meta.get_field("type")
            self.interface_types = _choice_values(type_field.choices)
            if not self.interface_types:
                self.interface_types = None
                raise ImportProblem("Nepavyko gauti interface tipu.")
        template_key = ("interface_templates", device_type_id)
        if template_key not in self.cache:
            self.cache[template_key] = list(self.models.InterfaceTemplate.objects.filter(device_type_id=device_type_id))
        templates = self.cache[template_key]
        plans = []
        names = set()
        primary_count = 0
        for interface in interfaces:
            values = interface.values
            try:
                name, kind = values.get("name", ""), values.get("type", "")
                if not name or name in names:
                    raise ImportProblem("Interface pavadinimas tuscias arba pasikartoja.")
                names.add(name)
                if kind not in self.interface_types:
                    raise ImportProblem(f"Neleistinas interface type '{kind}'. Naudokite NetBox reiksme.")
                template_matches = [template for template in templates if template.name == name]
                if len(template_matches) > 1:
                    raise ImportProblem(f"NetBox device type sablone interface '{name}' pasikartoja.")
                if template_matches:
                    template_kind = template_matches[0].type
                    if kind != template_kind:
                        raise ImportProblem(
                            f"Interface '{name}': Excel tipas '{kind}' neatitinka NetBox device type sablone nurodyto "
                            f"interface type tipo '{template_kind}'."
                        )
                addresses = interface_addresses(values.get("ip_addresses", ""))
                is_primary = interface_is_primary(values.get("is_primary", ""))
                if is_primary:
                    primary_count += 1
                    if primary_count > 1:
                        raise ImportProblem("Device gali tureti tik viena primary IPv4 adresa.")
                    if len(addresses) != 1 or addresses[0].version != 4:
                        raise ImportProblem("is_primary=TRUE reikalauja vieno IPv4 adreso ip_addresses langelyje.")
                vrf_id = None
                if addresses or values.get("vrf"):
                    vrf_name = values.get("vrf", "")
                    if not vrf_name:
                        raise ImportProblem("IP adresams privalomas VRF.")
                    matches = [
                        item for item in self.models.VRF.objects.filter(name=vrf_name, tenant_id=tenant_id)
                        if item.name == vrf_name and getattr(item, "tenant_id", None) == tenant_id
                    ]
                    if len(matches) != 1:
                        raise ImportProblem(f"Nurodytas neteisingas VRF name: '{vrf_name}'.")
                    vrf_id = matches[0].id
                for address in addresses:
                    if not self.allow_duplicate_ips:
                        existing_ips = self.models.IPAddress.objects.filter(vrf_id=vrf_id)
                        if any(
                            ip_interface(str(ip_obj.address)).ip == address.ip
                            for ip_obj in existing_ips
                        ):
                            raise ImportProblem(
                                f"IP {address.ip} jau yra VRF '{values['vrf']}'. Esamas IP nekeiciamas."
                            )
                plans.append(
                    {
                        "name": name,
                        "type": kind,
                        "vrf": vrf_id,
                        "addresses": [str(address) for address in addresses],
                        "is_primary": is_primary,
                    }
                )
            except ImportProblem as error:
                raise ImportProblem(f"Interfaces eil. {interface.excel_row}: {error}") from error
        return plans

    def prepare(self, values, interfaces=None):
        missing = missing_required_values(values)
        if missing:
            raise ImportProblem("Truksta reiksmiu: " + ", ".join(missing))
        if self.statuses is None:
            self.connect()
        status = values.get("status") or "active"
        if status not in self.statuses:
            raise ImportProblem(f"Neleistinas status '{status}'. Galimi: {', '.join(sorted(self.statuses))}.")
        custom_fields = self.custom_field_values(values)
        payload: dict[str, Any] = {"name": values["name"], "status": status, "custom_fields": custom_fields}
        errors = []
        try:
            site_id = self.lookup(self.models.Site, values["site"], "Site", allow_missing=True)
            payload["site"] = site_id
            if site_id is None:
                payload["_site_name"] = values["site"]
        except ImportProblem as error:
            errors.append(str(error))
        try:
            payload["_pid_matches"] = self.pid_devices(custom_fields)
        except ImportProblem as error:
            errors.append(f"cf_PID: {error}")
        try:
            tenant_id = self.lookup(self.models.Tenant, values["tenant"], "Tenant")
            payload["tenant"] = tenant_id
            payload["_existing_devices"] = (
                [
                    {"id": device.id, "name": device.name}
                    for device in self.models.Device.objects.filter(
                        name=values["name"], site_id=site_id, tenant_id=tenant_id
                    )
                    if device.name == values["name"]
                    and device.site_id == site_id
                    and device.tenant_id == tenant_id
                ]
                if site_id is not None
                else []
            )
        except ImportProblem as error:
            errors.append(str(error))
        if errors:
            raise PreparationProblem(" ".join(errors), payload)
        try:
            manufacturer_id = self.lookup(self.models.Manufacturer, values["manufacturer"], "Manufacturer")
            model = values["device_type"]
            model_key = ("model", model, manufacturer_id)
            if model_key not in self.cache:
                matches = [
                    item
                    for item in self.models.DeviceType.objects.filter(model=model, manufacturer_id=manufacturer_id)
                    if item.model == model and item.manufacturer_id == manufacturer_id
                ]
                if len(matches) != 1:
                    raise ImportProblem(
                        f"Device type '{model}' gamintojui '{values['manufacturer']}': "
                        + ("nerastas." if not matches else "rasti keli atitikmenys.")
                    )
                self.cache[model_key] = matches[0].id
            payload["device_type"] = self.cache[model_key]
            payload["role"] = self.lookup(self.models.DeviceRole, values["role"], "Role")
            if interfaces:
                payload["_interfaces"] = self.prepare_interfaces(interfaces, payload["tenant"], payload["device_type"])
            for name in ("serial", "asset_tag", "description"):
                if values.get(name):
                    payload[name] = values[name]
            excluded_ids = {device["id"] for device in payload["_pid_matches"]}
            if site_id is not None:
                self.check_device_name(values["name"], site_id, excluded_ids)
            if values.get("asset_tag"):
                existing_tags = self.models.Device.objects.filter(asset_tag=values["asset_tag"])
                if any(item.asset_tag == values["asset_tag"] and item.id not in excluded_ids for item in existing_tags):
                    raise ImportProblem("Toks asset_tag jau naudojamas NetBox.")
        except ImportProblem as error:
            raise PreparationProblem(str(error), payload) from error
        return payload

    def create(self, payload):
        self._require_perm("dcim.add_device", "Neturite teisės kurti įrenginių (dcim.add_device).")
        device_payload = dict(payload)
        device_payload.pop("_existing_devices", None)
        interfaces = device_payload.pop("_interfaces", [])
        expected_matches = device_payload.pop("_pid_matches", [])
        approved = device_payload.pop("_delete_approved", None)
        fields = device_payload.get("custom_fields", {})
        if any(name.casefold() == "pid" for name in fields):
            current_matches = self.pid_devices(fields)
            if current_matches != expected_matches:
                raise ImportProblem("PID atitikmenys pasikeite. Is naujo patikrinkite eilute ir patvirtinkite trynima.")
        if expected_matches and (len(expected_matches) != 1 or approved != expected_matches[0]):
            raise ImportProblem("PID jau naudojamas. Butinas aiskus konkretaus seno device istrynimo patvirtinimas.")
        if approved and (not expected_matches or not approved.get("last_updated")):
            raise ImportProblem("Nepakanka duomenu saugiam trynimui. Is naujo patikrinkite PID ir device last_updated.")

        site_name = device_payload.pop("_site_name", None)
        if site_name:
            self._require_perm("dcim.add_site", f"Site '{site_name}': neturite teisės kurti naujų Site (dcim.add_site).")
        if approved:
            self._require_perm("dcim.delete_device", "Neturite teisės trinti įrenginių (dcim.delete_device).")
        if interfaces:
            self._require_perm("dcim.add_interface", "Neturite teisės kurti sąsajų (dcim.add_interface).")
            if any(plan["addresses"] for plan in interfaces):
                self._require_perm("ipam.add_ipaddress", "Neturite teisės kurti IP adresų (ipam.add_ipaddress).")

        try:
            with self.models.atomic():
                if site_name:
                    site_id = self.lookup(self.models.Site, site_name, "Site", allow_missing=True)
                    if site_id is None:
                        normalized = unicodedata.normalize("NFKD", site_name).encode("ascii", "ignore").decode().lower()
                        stem = re.sub(r"[^a-z0-9_-]+", "-", normalized).strip("-_") or "site"
                        slug = f"{stem[:90]}-{sha256(site_name.encode()).hexdigest()[:8]}"
                        site = self.models.Site(name=site_name, slug=slug, status="active")
                        site.full_clean()
                        site.save()
                        site_id = site.id
                        self.cache[("Site", site_name, ())] = site_id
                    device_payload["site"] = site_id
                    payload["site"] = site_id
                    payload.pop("_site_name", None)
                    self.check_device_name(device_payload["name"], site_id, {approved["id"]} if approved else ())

                if approved:
                    old_id = approved["id"]
                    old_device = self.models.Device.objects.filter(id=old_id).first()
                    if old_device is None:
                        raise ImportProblem("Senas device neberastas. Trynimas atšauktas; patikrinkite eilute is naujo.")
                    if self.pid_snapshot(old_device, approved["pid_name"]) != approved:
                        raise ImportProblem("Senas device pasikeite po patikros. Trynimas atšauktas; patikrinkite eilute is naujo.")
                    payload["_delete_attempted_id"] = old_id
                    old_device.delete()
                    payload["_deleted_device_id"] = old_id
                    if self.pid_devices(fields):
                        raise ImportProblem("PID vel uzimtas. Naujas device nekuriamas; patikrinkite NetBox.")
                    self.check_device_name(device_payload["name"], device_payload["site"])

                device = self.models.Device(
                    name=device_payload["name"],
                    status=device_payload["status"],
                    site_id=device_payload["site"],
                    tenant_id=device_payload["tenant"],
                    device_type_id=device_payload["device_type"],
                    role_id=device_payload["role"],
                    serial=device_payload.get("serial", ""),
                    asset_tag=device_payload.get("asset_tag") or None,
                    description=device_payload.get("description", ""),
                    custom_field_data=dict(fields),
                )
                device.full_clean()
                device.save()
                payload["_created_device_id"] = device.id
                if interfaces:
                    self.create_interfaces(device, device_payload["tenant"], interfaces)
                return {"id": device.id, "name": device.name}
        except self.models.ValidationError as error:
            payload.pop("_created_device_id", None)
            payload.pop("_deleted_device_id", None)
            raise ImportProblem(_format_validation_error(error)) from error
        except ImportProblem:
            payload.pop("_created_device_id", None)
            payload.pop("_deleted_device_id", None)
            raise
        except Exception as error:
            payload.pop("_created_device_id", None)
            payload.pop("_deleted_device_id", None)
            raise ImportProblem(f"NetBox duomenų bazės klaida: {error}") from error

    def create_interfaces(self, device, tenant_id: int, plans):
        existing = list(self.models.Interface.objects.filter(device_id=device.id))
        primary_set = False
        for plan in plans:
            matches = [item for item in existing if item.name == plan["name"]]
            if len(matches) > 1:
                raise ImportProblem(f"Interface '{plan['name']}': rasti keli atitikmenys.")
            if matches:
                interface = matches[0]
                if interface.type != plan["type"]:
                    raise ImportProblem(f"Interface '{plan['name']}' is device type sablono turi kita type.")
            else:
                interface = self.models.Interface(device=device, name=plan["name"], type=plan["type"])
                interface.full_clean()
                interface.save()
            for address in plan["addresses"]:
                ip_obj = self.models.IPAddress(
                    address=address,
                    status="active",
                    tenant_id=tenant_id,
                    vrf_id=plan["vrf"],
                    assigned_object=interface,
                )
                ip_obj.full_clean()
                ip_obj.save()
                if plan["is_primary"]:
                    if primary_set:
                        raise ImportProblem("Device gali tureti tik viena primary IPv4 adresa.")
                    device.primary_ip4 = ip_obj
                    device.save()
                    primary_set = True
