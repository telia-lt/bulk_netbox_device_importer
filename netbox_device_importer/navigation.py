from netbox.plugins import PluginMenuItem

menu_items = (
    PluginMenuItem(
        link="plugins:netbox_device_importer:import",
        link_text="Masinis įrenginių importavimas",
        permissions=["dcim.add_device"],
    ),
)
