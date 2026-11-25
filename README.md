# NetBox Device Importer

NetBox 4.x įskiepis (`netbox_device_importer`), skirtas masiniam įrenginių, interfaces, IP adresų ir trūkstamų `Site` objektų importavimui iš `.xlsx` failo.

## Funkcionalumas

- Excel šablono (`.xlsx`) atsisiuntimas ir įkėlimas su `Devices` ir `Interfaces` lapais.
- Duomenų patikra prieš importą tiesiogiai per NetBox.
- `PID` unikalumo tikrinimas ir valdomas seno įrenginio ištrynimas esant PID konfliktui (tik su aiškiu naudotojo patvirtinimu).
- Automatinis trūkstamų `Site` sukūrimas importo metu.
- Interfaces, IPv4/IPv6 adresų ir `primary_ip4` priskyrimas įrenginiui.
- Fone vykdomas importas su eigos juosta, sustabdymo galimybe ir rezultatų `.xlsx` ataskaita.
