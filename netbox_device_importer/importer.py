from dataclasses import asdict, dataclass, field
from copy import deepcopy
from ipaddress import ip_interface
import re
from threading import Event, Lock, Thread
from typing import Any, Callable, Protocol


def missing_required_values(values: dict[str, str]) -> list[str]:
    required = ("name", "site", "manufacturer", "device_type", "role", "tenant")
    missing = [name for name in required if not values.get(name, "").strip()]
    if not any(value.strip() for name, value in values.items() if name.casefold() == "cf_pid"):
        missing.append("cf_PID")
    return sorted(missing)


class ImportProblem(Exception):
    pass


class PreparationProblem(ImportProblem):
    def __init__(self, message: str, payload: dict[str, Any]):
        super().__init__(message)
        self.payload = payload


class UncertainResult(ImportProblem):
    pass


def interface_addresses(value: str):
    addresses = []
    for text in re.split(r"[;,\s]+", value.strip()):
        if not text:
            continue
        try:
            if "/" not in text:
                raise ValueError("missing prefix")
            address = ip_interface(text)
        except ValueError as error:
            raise ImportProblem(f"Netinkamas IP '{text}'. Naudokite CIDR, pvz. 192.0.2.1/24.") from error
        if any(item.ip == address.ip for item in addresses):
            raise ImportProblem(f"Pasikartojantis IP '{text}'.")
        addresses.append(address)
    return addresses


def interface_is_primary(value: str) -> bool:
    normalized = value.strip().casefold()
    if not normalized:
        return False
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ImportProblem("is_primary turi buti TRUE, FALSE arba tuscias.")


@dataclass
class InterfaceRow:
    excel_row: int
    values: dict[str, str]


class DeviceAPI(Protocol):
    def connect(self) -> list[str]: ...

    def close(self) -> None: ...

    def prepare(self, values: dict[str, str], interfaces: list[InterfaceRow] | None = None) -> dict[str, Any]: ...

    def create(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class DeviceRow:
    excel_row: int
    values: dict[str, str]
    state: str = "Laukia patikros"
    error: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    device_id: int | None = None
    pid_matches: list[dict[str, Any]] = field(default_factory=list)
    delete_old: bool = False
    deleted_device_id: int | None = None
    interfaces: list[InterfaceRow] = field(default_factory=list)


def rows_to_list(rows: list[DeviceRow]) -> list[dict[str, Any]]:
    return [asdict(row) for row in rows]


def rows_from_list(data: list[dict[str, Any]]) -> list[DeviceRow]:
    result = []
    for item in data:
        interfaces = [InterfaceRow(**iface) for iface in item.get("interfaces", [])]
        row_data = {k: v for k, v in item.items() if k != "interfaces"}
        result.append(DeviceRow(**row_data, interfaces=interfaces))
    return result


def prepare_device(row: DeviceRow, api: DeviceAPI) -> dict[str, Any]:
    if row.interfaces:
        return api.prepare(row.values, row.interfaces)
    return api.prepare(row.values)


def set_delete_choice(row: DeviceRow, selected: bool) -> bool:
    allowed = row.state in {"Paruosta", "PID konfliktas"} and len(row.pid_matches) == 1
    if not allowed:
        changed = row.delete_old
        row.delete_old = False
        return changed
    choice = bool(selected and allowed)
    if row.delete_old == choice:
        return False
    row.delete_old = choice
    row.state = "Paruosta" if choice else "PID konfliktas"
    row.error = "" if choice else "PID jau naudojamas. Pažymėkite seno device ištrynimą arba pakeiskite PID."
    return True


def validate_row(row: DeviceRow, api: DeviceAPI) -> None:
    if row.state in {"Sukurta", "Neaiškus rezultatas", "Failo klaida"}:
        return
    row.payload = {}
    row.error = ""
    try:
        row.payload = prepare_device(row, api)
        matches = row.payload.get("_pid_matches", [])
        if matches != row.pid_matches:
            row.delete_old = False
        row.pid_matches = matches
        if len(matches) > 1:
            row.delete_old = False
            row.state = "PID konfliktas"
            row.error = "Tam paciam PID rasti keli device. Sutvarkykite NetBox rankiniu budu; automatinis trynimas draudziamas."
        elif matches and not row.delete_old:
            row.state = "PID konfliktas"
            row.error = "PID jau naudojamas. Pažymėkite seno device ištrynimą arba pakeiskite PID."
        else:
            row.state = "Paruosta"
    except ImportProblem as error:
        row.payload = error.payload if isinstance(error, PreparationProblem) else {}
        row.pid_matches = row.payload.get("_pid_matches", [])
        row.delete_old = False
        row.state = "Klaida"
        row.error = str(error)


def import_row(row: DeviceRow, api: DeviceAPI) -> None:
    if row.state != "Paruosta":
        return
    try:
        current_payload = prepare_device(row, api)
        matches = current_payload.get("_pid_matches", [])
        if matches != row.pid_matches:
            row.payload = current_payload
            row.pid_matches = matches
            row.delete_old = False
            row.state = "PID konfliktas" if matches else "Klaida"
            row.error = "PID atitikmenys pasikeite. Is naujo patikrinkite eilute ir patvirtinkite trynima."
            return
        if matches and (len(matches) != 1 or not row.delete_old):
            row.state = "PID konfliktas"
            row.error = "PID jau naudojamas. Seno device ištrynimas nepatvirtintas."
            return
        if matches:
            current_payload["_delete_approved"] = dict(matches[0])
        row.payload = current_payload
        result = api.create(current_payload)
        row.device_id = result["id"]
        row.state = "Sukurta"
        row.error = ""
    except UncertainResult as error:
        row.state = "Neaiškus rezultatas"
        row.error = str(error)
    except ImportProblem as error:
        row.state = "Klaida"
        row.error = str(error)
    finally:
        if row.payload.get("_created_device_id"):
            row.device_id = row.payload["_created_device_id"]
        if row.payload.get("_deleted_device_id"):
            row.deleted_device_id = row.payload["_deleted_device_id"]
        if row.state in {"Sukurta", "Klaida", "Neaiškus rezultatas"}:
            row.delete_old = False


@dataclass
class ImportProgress:
    rows: list[DeviceRow]
    completed: int
    total: int
    current_name: str
    stopping: bool
    finished: bool
    error: str


class ImportJob:
    def __init__(self, rows: list[DeviceRow]):
        self._rows = deepcopy(rows)
        self._indices = [index for index, row in enumerate(rows) if row.state == "Paruosta"]
        self._lock = Lock()
        self._stop = Event()
        self._finished = Event()
        self._started = False
        self._completed = 0
        self._current_name = ""
        self._error = ""

    def start(self, api_factory: Callable[[], DeviceAPI]) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("Importavimo uzduotis jau paleista.")
            self._started = True
        Thread(target=self._run, args=(api_factory,), daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def wait(self, timeout: float) -> bool:
        return self._finished.wait(timeout)

    def snapshot(self) -> ImportProgress:
        with self._lock:
            return ImportProgress(deepcopy(self._rows), self._completed, len(self._indices),
                                  self._current_name, self._stop.is_set(), self._finished.is_set(), self._error)

    def _run(self, api_factory: Callable[[], DeviceAPI]) -> None:
        api = None
        current_row = None
        try:
            if self._stop.is_set():
                return
            api = api_factory()
            api.connect()
            for index in self._indices:
                with self._lock:
                    if self._stop.is_set():
                        break
                    current_row = deepcopy(self._rows[index])
                    self._current_name = current_row.values.get("name", "")
                try:
                    import_row(current_row, api)
                except Exception:
                    current_row.state = "Neaiškus rezultatas"
                    current_row.delete_old = False
                    current_row.error = "Netiketa importavimo klaida. Pries kartodami patikrinkite irasus NetBox."
                    raise
                finally:
                    with self._lock:
                        self._rows[index] = current_row
                        self._completed += 1
                        self._current_name = ""
                current_row = None
        except ImportProblem as error:
            with self._lock:
                self._error = str(error)
        except Exception:
            with self._lock:
                self._error = "Importavimas nutrauktas del netiketos klaidos. Patikrinkite NetBox pries kartodami."
        finally:
            try:
                if api is not None:
                    api.close()
            finally:
                self._finished.set()
