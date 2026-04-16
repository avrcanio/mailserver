from django import forms

from .models import SenderBlocklistRule


class SenderBlocklistRuleForm(forms.ModelForm):
    class Meta:
        model = SenderBlocklistRule
        fields = ["kind", "value", "enabled", "note"]

    def clean_value(self):
        return SenderBlocklistRule.normalize_value(self.cleaned_data.get("kind"), self.cleaned_data["value"])
