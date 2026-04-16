from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated
from rest_framework.response import Response
from rest_framework.views import exception_handler


def mailbox_api_exception_handler(exc, context):
    if isinstance(exc, (AuthenticationFailed, NotAuthenticated)):
        return Response({"error": "not_authenticated"}, status=status.HTTP_401_UNAUTHORIZED)
    return exception_handler(exc, context)
