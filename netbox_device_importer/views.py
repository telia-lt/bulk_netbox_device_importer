import uuid
from threading import Lock
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.cache import cache
from django.core.exceptions import SuspiciousOperation
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from .excel_io import parse_excel, report_bytes, template_bytes
from .forms import ExcelUploadForm
from .importer import ImportJob, ImportProblem, rows_from_list, rows_to_list, set_delete_choice, validate_row
from .netbox_backend import NetBoxORMBackend
from .presentation import FILTER_OPTIONS, demo_rows, row_metrics, table_data

CACHE_TIMEOUT_SECONDS = 4 * 60 * 60
_ACTIVE_JOBS: dict[str, ImportJob] = {}
_JOBS_LOCK = Lock()


def _state_key(request) -> str:
    session_token = request.session.get("netbox_device_importer_token")
    if not session_token:
        session_token = uuid.uuid4().hex
        request.session["netbox_device_importer_token"] = session_token
    token = request.POST.get("import_session") or request.GET.get("import_session")
    try:
        request.import_session = uuid.UUID(token).hex if token else uuid.uuid4().hex
    except ValueError as error:
        raise SuspiciousOperation("Netinkamas importavimo skirtuko identifikatorius.") from error
    return f"netbox_device_importer:{request.user.pk}:{session_token}:{request.import_session}"


def _import_url(request, **query) -> str:
    return f"{request.path}?{urlencode({**query, 'import_session': request.import_session})}"


def _load_state(key: str) -> dict:
    return cache.get(key) or {
        "rows": [],
        "is_demo": False,
        "allow_duplicate_ips": False,
        "confirm_import": False,
        "last_import": None,
        "filename": "",
    }


def _save_state(key: str, state: dict) -> None:
    cache.set(key, state, timeout=CACHE_TIMEOUT_SECONDS)


def _sync_job(key: str, state: dict) -> tuple[bool, dict | None]:
    with _JOBS_LOCK:
        job = _ACTIVE_JOBS.get(key)
    if job is None:
        return False, None
    progress = job.snapshot()
    state["rows"] = rows_to_list(progress.rows)
    if progress.finished:
        state["last_import"] = {
            "completed": progress.completed,
            "total": progress.total,
            "stopping": progress.stopping,
            "error": progress.error,
        }
        state["confirm_import"] = False
        _save_state(key, state)
        with _JOBS_LOCK:
            _ACTIVE_JOBS.pop(key, None)
        return False, {
            "active": False,
            "completed": progress.completed,
            "total": progress.total,
            "current_name": "",
            "stopping": progress.stopping,
            "finished": True,
            "error": progress.error,
        }
    _save_state(key, state)
    return True, {
        "active": True,
        "completed": progress.completed,
        "total": progress.total,
        "current_name": progress.current_name,
        "stopping": progress.stopping,
        "finished": False,
        "error": progress.error,
    }


class DeviceImportView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "dcim.add_device"
    template_name = "netbox_device_importer/import.html"

    def get(self, request):
        key = _state_key(request)
        state = _load_state(key)
        import_active, job_status = _sync_job(key, state)

        rows = rows_from_list(state["rows"]) if state["rows"] else []
        selected_filter = request.GET.get("filter", "Visos")
        if selected_filter not in FILTER_OPTIONS:
            selected_filter = "Visos"
        search = request.GET.get("q", "").strip()

        records = table_data(rows, selected_filter, search) if rows else []
        custom_columns = sorted(
            {
                name
                for row in rows
                for name in row.values
                if name.startswith("cf_") and name.casefold() != "cf_pid"
            }
        )
        for rec in records:
            rec["custom_values"] = [rec["custom_fields"].get(col, "") for col in custom_columns]
        metrics = row_metrics(rows) if rows else {
            "total": 0,
            "ready": 0,
            "errors": 0,
            "created": 0,
            "uncertain": 0,
            "delete_count": 0,
        }
        bulk_delete_available = (
            selected_filter == "Su klaidomis"
            and any(
                r["state"] == "PID konfliktas" and r["can_delete"]
                for r in records
            )
        )

        form = ExcelUploadForm(initial={"allow_duplicate_ips": state.get("allow_duplicate_ips", False)})
        context = {
            "form": form,
            "has_rows": bool(rows),
            "is_demo": state.get("is_demo", False),
            "filename": state.get("filename", ""),
            "allow_duplicate_ips": state.get("allow_duplicate_ips", False),
            "confirm_import": state.get("confirm_import", False),
            "import_active": import_active,
            "job_status": job_status,
            "last_import": state.get("last_import"),
            "metrics": metrics,
            "records": records,
            "custom_columns": custom_columns,
            "filter_options": FILTER_OPTIONS,
            "selected_filter": selected_filter,
            "search": search,
            "bulk_delete_available": bulk_delete_available,
            "import_session": request.import_session,
            "import_storage_key": key.rsplit(":", 1)[0],
        }
        return render(request, self.template_name, context)

    def post(self, request):
        key = _state_key(request)
        state = _load_state(key)
        import_active, _ = _sync_job(key, state)
        action = request.POST.get("action", "")
        selected_filter = request.POST.get("filter", "Visos")
        search = request.POST.get("q", "").strip()

        if import_active:
            messages.warning(request, "Importavimas šiuo metu vyksta. Palaukite arba sustabdykite jį.")
            return redirect(_import_url(request))

        if action == "upload":
            form = ExcelUploadForm(request.POST, request.FILES)
            if not form.is_valid():
                messages.error(request, "Pasirinkite galiojantį .xlsx failą.")
                return redirect(_import_url(request))
            upload = form.cleaned_data["excel_file"]
            allow_duplicate_ips = bool(form.cleaned_data.get("allow_duplicate_ips"))
            if upload.size > 10 * 1024 * 1024:
                messages.error(request, "Failas per didelis. Riba: 10 MB.")
                return redirect(_import_url(request))
            content = upload.read()
            try:
                rows = parse_excel(content, allow_duplicate_ips=allow_duplicate_ips)
            except ImportProblem as error:
                messages.error(request, str(error))
                return redirect(_import_url(request))
            state = {
                "rows": rows_to_list(rows),
                "is_demo": False,
                "allow_duplicate_ips": allow_duplicate_ips,
                "confirm_import": False,
                "last_import": None,
                "filename": upload.name,
                "excel_content": content,
            }
            _save_state(key, state)
            messages.success(request, f"Įkelta įrenginių eilučių: {len(rows)}.")
            return redirect(_import_url(request))

        if action == "set_duplicate_ips":
            allow_duplicate_ips = request.POST.get("allow_duplicate_ips") == "on"
            if allow_duplicate_ips != state.get("allow_duplicate_ips", False):
                if state.get("rows") and not state.get("is_demo"):
                    content = state.get("excel_content")
                    if content is None:
                        messages.warning(request, "Įkelkite Excel failą iš naujo, kad būtų galima perskaičiuoti IP dublikatų klaidas.")
                        return redirect(_import_url(request, filter=selected_filter, q=search))
                    try:
                        parsed_rows = parse_excel(content, allow_duplicate_ips=allow_duplicate_ips)
                    except ImportProblem as error:
                        messages.error(request, str(error))
                        return redirect(_import_url(request))
                    previous_rows = {row.excel_row: row for row in rows_from_list(state["rows"])}
                    terminal_states = {"Sukurta", "Neaiskus rezultatas", "Neaiškus rezultatas"}
                    rows = [
                        previous_rows[row.excel_row]
                        if previous_rows[row.excel_row].state in terminal_states else row
                        for row in parsed_rows
                    ]
                    state["rows"] = rows_to_list(rows)
                state["confirm_import"] = False
            state["allow_duplicate_ips"] = allow_duplicate_ips
            _save_state(key, state)
            return redirect(_import_url(request, filter=selected_filter, q=search))

        if action == "demo":
            state = {
                "rows": rows_to_list(demo_rows()),
                "is_demo": True,
                "allow_duplicate_ips": False,
                "confirm_import": False,
                "last_import": None,
                "filename": "pavyzdys.xlsx",
            }
            _save_state(key, state)
            return redirect(_import_url(request))

        if action in {"close_demo", "clear_upload"}:
            _save_state(
                key,
                {
                    "rows": [],
                    "is_demo": False,
                    "allow_duplicate_ips": False,
                    "confirm_import": False,
                    "last_import": None,
                    "filename": "",
                },
            )
            return redirect(_import_url(request))

        rows = rows_from_list(state.get("rows", []))
        if not rows:
            messages.warning(request, "Pirmiausia įkelkite Excel failą.")
            return redirect(_import_url(request))

        if action == "validate":
            if state.get("is_demo"):
                messages.warning(request, "Demonstraciniai duomenys NetBox netikrinami.")
                return redirect(_import_url(request))
            backend = NetBoxORMBackend(
                allow_duplicate_ips=state.get("allow_duplicate_ips", False),
                user=request.user,
            )
            try:
                backend.connect()
                candidates = [
                    row
                    for row in rows
                    if row.state not in {"Sukurta", "Failo klaida", "Neaiskus rezultatas", "Neaiškus rezultatas"}
                ]
                for row in candidates:
                    validate_row(row, backend)
                state["rows"] = rows_to_list(rows)
                state["confirm_import"] = False
                _save_state(key, state)
                messages.success(request, f"Patikrinta eilučių: {len(candidates)}.")
            except ImportProblem as error:
                messages.error(request, str(error))
            finally:
                backend.close()

        elif action == "toggle_delete":
            visible_excel_rows = {int(v) for v in request.POST.getlist("visible_rows") if v.isdigit()}
            selected_delete_rows = {int(v) for v in request.POST.getlist("delete_rows") if v.isdigit()}
            changed = False
            for row in rows:
                if row.excel_row in visible_excel_rows:
                    changed = set_delete_choice(row, row.excel_row in selected_delete_rows) or changed
            if changed:
                state["rows"] = rows_to_list(rows)
                state["confirm_import"] = False
                _save_state(key, state)

        elif action == "bulk_delete":
            records = table_data(rows, selected_filter, search)
            visible_rows = {r["excel_row"] for r in records}
            changed = False
            for row in rows:
                if row.excel_row in visible_rows and row.state == "PID konfliktas" and len(row.pid_matches) == 1:
                    changed = set_delete_choice(row, True) or changed
            if changed:
                state["rows"] = rows_to_list(rows)
                state["confirm_import"] = False
                _save_state(key, state)

        elif action == "import":
            if state.get("is_demo"):
                messages.warning(request, "Demonstraciniai duomenys neimportuojami.")
                return redirect(_import_url(request))
            confirmed = request.POST.get("confirm_import") == "on"
            if not confirmed:
                messages.error(request, "Pažymėkite patvirtinimo varnelę prieš importuojant.")
                return redirect(_import_url(request))
            ready_count = sum(1 for row in rows if row.state in {"Paruosta", "Paruošta"})
            if not ready_count:
                messages.warning(request, "Nėra paruoštų importavimui eilučių.")
                return redirect(_import_url(request))

            state["confirm_import"] = True
            state["last_import"] = None
            _save_state(key, state)

            user = request.user
            allow_duplicates = state.get("allow_duplicate_ips", False)
            job = ImportJob(rows)
            with _JOBS_LOCK:
                _ACTIVE_JOBS[key] = job
            job.start(lambda: NetBoxORMBackend(allow_duplicate_ips=allow_duplicates, user=user))

        return redirect(_import_url(request, filter=selected_filter, q=search))


class TemplateDownloadView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "dcim.add_device"

    def get(self, request):
        response = HttpResponse(
            template_bytes(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="netbox_devices_template.xlsx"'
        return response


class ReportDownloadView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "dcim.add_device"

    def get(self, request):
        key = _state_key(request)
        state = _load_state(key)
        _sync_job(key, state)
        rows = rows_from_list(state.get("rows", []))
        if not rows:
            messages.warning(request, "Nėra duomenų ataskaitai.")
            return redirect(
                f"{reverse('plugins:netbox_device_importer:import')}?"
                f"{urlencode({'import_session': request.import_session})}"
            )
        response = HttpResponse(
            report_bytes(rows),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="netbox_import_results.xlsx"'
        return response


class ImportStatusView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "dcim.add_device"

    def get(self, request):
        key = _state_key(request)
        state = _load_state(key)
        import_active, status = _sync_job(key, state)
        if status is None:
            return JsonResponse({"active": False, "finished": True, "last_import": state.get("last_import")})
        return JsonResponse(status)


class ImportStopView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "dcim.add_device"

    def post(self, request):
        key = _state_key(request)
        with _JOBS_LOCK:
            job = _ACTIVE_JOBS.get(key)
        if job is not None:
            job.stop()
            return JsonResponse({"stopping": True})
        return JsonResponse({"stopping": False})
