import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeWebhookURLError(ValueError):
    """Raised when a webhook URL violates the outbound URL policy."""


def validate_webhook_url_syntax(url: str) -> None:
    """Validate URL structure without making a network request."""
    if not url or len(url) > 2083:
        raise UnsafeWebhookURLError("Invalid webhook URL.")

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsafeWebhookURLError("Invalid webhook URL.") from exc

    if parsed.scheme.lower() != "https":
        raise UnsafeWebhookURLError("Webhook URLs must use HTTPS.")

    if not hostname:
        raise UnsafeWebhookURLError("Webhook URL must include a hostname.")

    if parsed.username is not None or parsed.password is not None:
        raise UnsafeWebhookURLError("Webhook URLs must not contain credentials.")

    if parsed.fragment:
        raise UnsafeWebhookURLError("Webhook URLs must not contain fragments.")

    if port not in (None, 443):
        raise UnsafeWebhookURLError("Webhook URLs must use port 443.")

    if hostname.endswith("."):
        hostname = hostname[:-1]

    normalized_host = hostname.lower()

    if normalized_host == "localhost" or normalized_host.endswith(
        (".localhost", ".local", ".internal", ".test", ".invalid")
    ):
        raise UnsafeWebhookURLError("Webhook hostname is not allowed.")

    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        # A hostname still needs connection-time destination enforcement.
        return

    if not address.is_global:
        raise UnsafeWebhookURLError("Webhook IP address is not publicly routable.")


def resolve_and_check_public_addresses(hostname: str) -> set[str]:
    """Reject hostnames whose current DNS answers include non-global IPs.

    This is a supplementary check only. It does not pin the address used
    by a later HTTP connection and cannot independently prevent DNS rebinding.
    """
    try:
        results = socket.getaddrinfo(
            hostname,
            443,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise UnsafeWebhookURLError("Webhook hostname could not be resolved.") from exc

    addresses = {result[4][0].split("%", maxsplit=1)[0] for result in results}

    if not addresses:
        raise UnsafeWebhookURLError("Webhook hostname has no usable addresses.")

    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise UnsafeWebhookURLError(
                "Webhook hostname resolved to an invalid address."
            ) from exc

        if not address.is_global:
            raise UnsafeWebhookURLError(
                "Webhook hostname resolves to a non-public address."
            )

    return addresses
