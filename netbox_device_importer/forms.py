from django import forms


class ExcelUploadForm(forms.Form):
    excel_file = forms.FileField(
        label="Excel failas (.xlsx)",
        required=True,
        widget=forms.ClearableFileInput(
            attrs={"accept": ".xlsx", "class": "visually-hidden", "tabindex": "-1"}
        ),
    )
    allow_duplicate_ips = forms.BooleanField(
        label="Leisti IP adresams kartotis",
        required=False,
        help_text="Pažymėjus, IP dublikatai tarp skirtingų įrenginių nebus laikomi failo klaida.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
