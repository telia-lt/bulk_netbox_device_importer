# NetBox Device Importer

NetBox 4.x įskiepis (`netbox_device_importer`), skirtas masiniam įrenginių, sąsajų (`Interfaces`), IP adresų ir trūkstamų `Site` objektų importavimui iš `.xlsx` failų.

## Funkcionalumas

- Excel šablono (`.xlsx`) atsisiuntimas ir įkėlimas su `Devices` ir `Interfaces` lapais.
- Duomenų patikra prieš importą tiesiogiai per NetBox ORM (be išorinio API tokeno).
- `cf_PID` unikalumo tikrinimas ir valdomas seno įrenginio ištrynimas esant PID konfliktui (tik su aiškiu naudotojo patvirtinimu).
- Automatinis trūkstamų `Site` sukūrimas importo metu.
- Sąsajų, IPv4/IPv6 adresų ir `primary_ip4` priskyrimas (su pasirenkama `Leisti IP adresams kartotis` parinktimi).
- Fone vykdomas importas su eigos juosta, sustabdymo galimybe ir rezultatų `.xlsx` ataskaita.
- Demonstracinis režimas su pavyzdiniais duomenimis.

## Diegimas į NetBox 4.x

1. Aktyvuokite NetBox virtualią aplinką ir įdiekite įskiepį:

```bash
source /opt/netbox/venv/bin/activate
pip install .
```

2. Pridėkite įskiepį į `/opt/netbox/netbox/netbox/configuration.py`:

```python
PLUGINS = [
    "netbox_device_importer",
]
```

3. Perkraukite NetBox servisus:

```bash
sudo systemctl restart netbox netbox-rq
```

## Testavimas

```bash
python -m unittest discover -s tests -v
```
