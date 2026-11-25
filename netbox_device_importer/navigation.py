from netbox.plugins import PluginMenuItem

menu_items = (
    PluginMenuItem(
        link="plugins:netbox_device_importer:import",
        link_text="Įrenginių importas",
        permissions=["dcim.add_device"],
    ),
)
