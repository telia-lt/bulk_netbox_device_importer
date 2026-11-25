try:
    from netbox.plugins import PluginConfig
except ImportError:  # pragma: no cover - allows standalone unit testing without NetBox installed
    PluginConfig = object


class NetBoxDeviceImporterConfig(PluginConfig):
    name = "netbox_device_importer"
    verbose_name = "Device Importer"
    description = "Masinis įrenginių, sąsajų, IP adresų ir trūkstamų site importas iš Excel."
    version = "0.1.0"
    base_url = "device-importer"
    min_version = "4.0.0"


config = NetBoxDeviceImporterConfig
