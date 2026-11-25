import contextlib
import unittest
from datetime import datetime, timezone

from netbox_device_importer.importer import ImportProblem, InterfaceRow, PreparationProblem
from netbox_device_importer.netbox_backend import NetBoxModels, NetBoxORMBackend
from netbox_device_importer.presentation import demo_rows, row_metrics, table_data


class FakeValidationError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.messages = [message]


class FakeQuerySet:
    def __init__(self, items):
        self._items = list(items)

    def filter(self, **kwargs):
        result = []
        for item in self._items:
            matched = True
            for key, value in kwargs.items():
                if key.startswith("custom_field_data__"):
                    cf_name = key.split("__", 1)[1]
                    if (getattr(item, "custom_field_data", None) or {}).get(cf_name) != value:
                        matched = False
                        break
                elif key == "object_types__app_label":
                    if not any(ot[0] == value for ot in getattr(item, "object_types", ())):
                        matched = False
                        break
                elif key == "object_types__model":
                    if not any(ot[1] == value for ot in getattr(item, "object_types", ())):
                        matched = False
                        break
                else:
                    if getattr(item, key, None) != value:
                        matched = False
                        break
            if matched:
                result.append(item)
        return FakeQuerySet(result)

    def first(self):
        return self._items[0] if self._items else None

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)


class FakeManager:
    def __init__(self, model_cls):
        self.model_cls = model_cls
        self.items = []
        self.next_id = 1

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = self.next_id
            self.next_id += 1
        self.items.append(obj)
        return obj

    def filter(self, **kwargs):
        return FakeQuerySet(self.items).filter(**kwargs)


class FakeField:
    def __init__(self, choices):
        self.choices = choices


class FakeMeta:
    def __init__(self, fields):
        self._fields = fields

    def get_field(self, name):
        return self._fields[name]


class FakeModel:
    objects: FakeManager

    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", None)
        for k, v in kwargs.items():
            setattr(self, k, v)

    def full_clean(self):
        pass

    def save(self):
        if self.id is None:
            self.__class__.objects.add(self)

    def delete(self):
        self.__class__.objects.items = [i for i in self.__class__.objects.items if i.id != self.id]


class FakeUser:
    def __init__(self, perms=None):
        self.perms = set(perms or [])

    def has_perm(self, perm):
        return perm in self.perms


def build_fake_models():
    class Site(FakeModel):
        pass

    class Manufacturer(FakeModel):
        pass

    class DeviceType(FakeModel):
        pass

    class DeviceRole(FakeModel):
        pass

    class Tenant(FakeModel):
        pass

    class VRF(FakeModel):
        pass

    class CustomField(FakeModel):
        pass

    class InterfaceTemplate(FakeModel):
        pass

    class Interface(FakeModel):
        _meta = FakeMeta({"type": FakeField([("virtual", "Virtual"), ("1000base-t", "1000BASE-T")])})

    class IPAddress(FakeModel):
        pass

    class Device(FakeModel):
        _meta = FakeMeta({"status": FakeField([("active", "Active"), ("planned", "Planned")])})

    for cls in (
        Site,
        Manufacturer,
        DeviceType,
        DeviceRole,
        Tenant,
        VRF,
        CustomField,
        InterfaceTemplate,
        Interface,
        IPAddress,
        Device,
    ):
        cls.objects = FakeManager(cls)

    Site.objects.add(Site(id=1, name="Vilnius", slug="vilnius", status="active"))
    Tenant.objects.add(Tenant(id=10, name="Pavyzdys"))
    Manufacturer.objects.add(Manufacturer(id=20, name="Cisco"))
    DeviceType.objects.add(DeviceType(id=30, model="C9200L-24T-4G", manufacturer_id=20))
    DeviceRole.objects.add(DeviceRole(id=40, name="Access Switch"))
    VRF.objects.add(VRF(id=50, name="Customer-VRF", tenant_id=10))
    CustomField.objects.add(CustomField(id=60, name="PID", type="text", object_types=[("dcim", "device")]))
    CustomField.objects.add(CustomField(id=61, name="speed_mbps", type="integer", object_types=[("dcim", "device")]))
    InterfaceTemplate.objects.add(InterfaceTemplate(id=70, device_type_id=30, name="Gi1/0/1", type="1000base-t"))

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
        atomic=contextlib.nullcontext,
        close_old_connections=lambda: None,
        ValidationError=FakeValidationError,
    )


class NetBoxORMBackendTests(unittest.TestCase):
    def base_values(self):
        return {
            "name": "SW-VLN-001",
            "tenant": "Pavyzdys",
            "site": "Vilnius",
            "manufacturer": "Cisco",
            "device_type": "C9200L-24T-4G",
            "role": "Access Switch",
            "status": "active",
            "cf_PID": "PID-001",
            "cf_speed_mbps": "1000",
        }

    def test_connect_requires_add_device_permission(self):
        models = build_fake_models()
        backend = NetBoxORMBackend(user=FakeUser(), models=models)
        with self.assertRaisesRegex(ImportProblem, "dcim.add_device"):
            backend.connect()

        allowed = NetBoxORMBackend(user=FakeUser({"dcim.add_device"}), models=models)
        self.assertEqual(allowed.connect(), ["active", "planned"])

    def test_prepare_and_create_device_with_interfaces_and_primary_ip(self):
        models = build_fake_models()
        user = FakeUser({"dcim.add_device", "dcim.add_interface", "ipam.add_ipaddress"})
        backend = NetBoxORMBackend(user=user, models=models)
        interfaces = [
            InterfaceRow(
                2,
                {
                    "device_name": "SW-VLN-001",
                    "tenant": "Pavyzdys",
                    "name": "Gi1/0/1",
                    "type": "1000base-t",
                    "ip_addresses": "192.0.2.10/24",
                    "is_primary": "TRUE",
                    "vrf": "Customer-VRF",
                },
            )
        ]
        payload = backend.prepare(self.base_values(), interfaces)
        self.assertEqual(payload["custom_fields"], {"PID": "PID-001", "speed_mbps": 1000})
        result = backend.create(payload)
        self.assertEqual(result["name"], "SW-VLN-001")
        created_device = models.Device.objects.items[0]
        self.assertEqual(created_device.custom_field_data["PID"], "PID-001")
        self.assertEqual(len(models.Interface.objects.items), 1)
        self.assertEqual(len(models.IPAddress.objects.items), 1)
        self.assertEqual(created_device.primary_ip4.id, models.IPAddress.objects.items[0].id)

    def test_missing_site_is_created_on_import(self):
        models = build_fake_models()
        user = FakeUser({"dcim.add_device", "dcim.add_site"})
        backend = NetBoxORMBackend(user=user, models=models)
        values = self.base_values()
        values["site"] = "Klaipėda"
        payload = backend.prepare(values)
        self.assertEqual(payload["_site_name"], "Klaipėda")
        backend.create(payload)
        self.assertTrue(any(s.name == "Klaipėda" for s in models.Site.objects.items))

    def test_pid_conflict_deletes_approved_old_device(self):
        models = build_fake_models()
        old_site = models.Site.objects.items[0]
        old_tenant = models.Tenant.objects.items[0]
        models.Device.objects.add(
            models.Device(
                id=99,
                name="SW-OLD-001",
                site=old_site,
                site_id=old_site.id,
                tenant=old_tenant,
                tenant_id=old_tenant.id,
                last_updated=datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc),
                custom_field_data={"PID": "PID-001"},
            )
        )
        user = FakeUser({"dcim.add_device", "dcim.delete_device"})
        backend = NetBoxORMBackend(user=user, models=models)
        payload = backend.prepare(self.base_values())
        self.assertEqual(len(payload["_pid_matches"]), 1)
        with self.assertRaisesRegex(ImportProblem, "patvirtinimas"):
            backend.create(payload)

        payload["_delete_approved"] = dict(payload["_pid_matches"][0])
        res = backend.create(payload)
        self.assertEqual(payload["_deleted_device_id"], 99)
        self.assertEqual([d.id for d in models.Device.objects.items], [res["id"]])

    def test_interface_template_type_mismatch_is_rejected(self):
        models = build_fake_models()
        backend = NetBoxORMBackend(models=models)
        interfaces = [
            InterfaceRow(
                2,
                {
                    "device_name": "SW-VLN-001",
                    "tenant": "Pavyzdys",
                    "name": "Gi1/0/1",
                    "type": "virtual",
                    "ip_addresses": "",
                    "is_primary": "",
                    "vrf": "",
                },
            )
        ]
        with self.assertRaises(PreparationProblem) as ctx:
            backend.prepare(self.base_values(), interfaces)
        self.assertIn("neatitinka NetBox device type sablone", str(ctx.exception))

    def test_duplicate_ip_in_vrf_respects_allow_duplicate_ips(self):
        models = build_fake_models()
        models.IPAddress.objects.add(models.IPAddress(id=1, address="192.0.2.10/24", vrf_id=50))
        interfaces = [
            InterfaceRow(
                2,
                {
                    "device_name": "SW-VLN-001",
                    "tenant": "Pavyzdys",
                    "name": "Gi1/0/1",
                    "type": "1000base-t",
                    "ip_addresses": "192.0.2.10/32",
                    "is_primary": "",
                    "vrf": "Customer-VRF",
                },
            )
        ]
        backend_strict = NetBoxORMBackend(allow_duplicate_ips=False, models=models)
        with self.assertRaises(PreparationProblem) as ctx:
            backend_strict.prepare(self.base_values(), interfaces)
        self.assertIn("jau yra VRF", str(ctx.exception))

        backend_relaxed = NetBoxORMBackend(allow_duplicate_ips=True, models=models)
        payload = backend_relaxed.prepare(self.base_values(), interfaces)
        self.assertEqual(len(payload["_interfaces"]), 1)


class PresentationTests(unittest.TestCase):
    def test_demo_rows_metrics_and_filtering(self):
        rows = demo_rows()
        metrics = row_metrics(rows)
        self.assertEqual(metrics["total"], 6)
        self.assertEqual(metrics["ready"], 3)
        self.assertEqual(metrics["errors"], 3)
        self.assertEqual(len(table_data(rows, "Su klaidomis", "")), 3)
        self.assertEqual(len(table_data(rows, "Visos", "Klaipėda")), 1)
