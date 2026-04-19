from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .models import ApplyLog, SenderBlocklistRule
from .services import apply_blocklist


def privacy_policy(request):
    return render(
        request,
        "mailops/privacy.html",
        {
            "contact_email": "postmaster@finestar.hr",
            "effective_date": "17. travnja 2026.",
        },
    )


@staff_member_required
def dashboard(request):
    context = {
        "rules": SenderBlocklistRule.objects.all()[:10],
        "last_apply": ApplyLog.objects.first(),
        "mailadmin_host": request.get_host(),
    }
    return render(request, "mailops/dashboard.html", context)


@staff_member_required
@require_POST
def apply_blocklist_view(request):
    try:
        apply_blocklist()
    except Exception as exc:
        ApplyLog.objects.create(status=ApplyLog.STATUS_ERROR, message=str(exc), applied_by=request.user)
        messages.error(request, f"Apply failed: {exc}")
        return redirect("mailops:dashboard")

    ApplyLog.objects.create(
        status=ApplyLog.STATUS_SUCCESS,
        message="Sender blocklist rendered and Postfix reloaded.",
        applied_by=request.user,
    )
    messages.success(request, "Rules applied to Postfix.")
    return redirect("mailops:dashboard")
