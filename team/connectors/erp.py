"""The ``erp`` capability: what any ERP driver (Bling today, others later) offers, independent of the vendor.

Reading, issuing a charge and paying are kept apart. A charge being *issued* does not mean it was *paid*; payment is
only ever proven by the ERP itself (authenticated event or authorized polling), never by what a customer says.
Moving money and paying suppliers stay manual: nothing here offers it.
"""
import hashlib
import hmac
import re

from ..schema import redact_secrets
from .registry import Capability

# -- the contract ---------------------------------------------------------------------------------------------------
_CHARGE_ARGS = {"cliente": ("string", True), "beneficiario": ("string", True), "documento": ("string", True),
                "valor_centavos": ("integer", True), "moeda": ("string", True), "vencimento": ("string", False),
                "chave": ("string", True), "dry_run": ("boolean", False)}
_CHARGE_BINDINGS = {"recipient_argument": "cliente", "amount_argument": "valor_centavos", "currency_argument": "moeda"}

ERP = Capability(
    "erp",
    required={"consultar-clientes": "read", "consultar-cobrancas": "read", "status-cobranca": "read"},
    optional={"consultar-pedidos": "read", "emitir-cobranca": "write", "alterar-cobranca": "write"},
    arguments={"consultar-clientes": {"busca": ("string", False)},
               "consultar-cobrancas": {"cliente": ("string", False), "documento": ("string", False), "status": ("string", False),
                                       "limite": ("integer", False)},
               "status-cobranca": {"documento": ("string", True)},
               "consultar-pedidos": {"cliente": ("string", False), "limite": ("integer", False)},
               "emitir-cobranca": _CHARGE_ARGS, "alterar-cobranca": _CHARGE_ARGS},
    bindings={"emitir-cobranca": _CHARGE_BINDINGS, "alterar-cobranca": _CHARGE_BINDINGS},
    idempotent={"emitir-cobranca": "chave", "alterar-cobranca": "chave"})

# -- the common charge model ------------------------------------------------------------------------------------------
CHARGE_STATES = ("emitida", "vencida", "paga", "estornada", "cancelada")
_STATE_ALIASES = {"pendente": "emitida"}  # many ERPs call an issued, unpaid charge "pending"
_CURRENCY = re.compile(r"^[a-z]{3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_EXTRAS = 4096


class ChargeRejected(ValueError):
    """Invalid charge data; the message is safe to show and never contains secrets."""


def _text(value, high, what):
    if not (isinstance(value, str) and 1 <= len(value.strip()) <= high):
        raise ChargeRejected(f"{what} invalido.")
    return value.strip()


def normalize_charge(raw):
    """A driver maps its vendor's charge onto this shape. Money is integer cents with an explicit currency;
    vendor-specific fields stay in ``extras`` (size-bounded, secrets redacted) and are never interpreted."""
    if not isinstance(raw, dict):
        raise ChargeRejected("Cobranca invalida.")
    valor, status = raw.get("valor_centavos"), _STATE_ALIASES.get(raw.get("status"), raw.get("status"))
    moeda = raw.get("moeda")
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
        raise ChargeRejected("Valor invalido (use centavos inteiros).")
    if not (isinstance(moeda, str) and _CURRENCY.match(moeda.lower())):
        raise ChargeRejected("Moeda invalida.")
    if status not in CHARGE_STATES:
        raise ChargeRejected("Status invalido.")
    vencimento = raw.get("vencimento")
    if vencimento is not None and not (isinstance(vencimento, str) and _DATE.match(vencimento)):
        raise ChargeRejected("Vencimento invalido (use AAAA-MM-DD).")
    extras = raw.get("extras") or {}
    if not isinstance(extras, dict) or len(str(extras)) > MAX_EXTRAS:
        raise ChargeRejected("extras invalido ou grande demais.")
    beneficiario = raw.get("beneficiario")
    return {"documento": _text(raw.get("documento"), 80, "Documento"), "cliente": _text(raw.get("cliente"), 200, "Cliente"),
            "beneficiario": _text(beneficiario, 200, "Beneficiario") if beneficiario is not None else None,
            "valor_centavos": valor, "moeda": moeda.lower(), "status": status, "vencimento": vencimento,
            "extras": redact_secrets(extras)}


# -- helpers every driver and the reconciler share ---------------------------------------------------------------------
def verify_hmac_sha256(secret: bytes, body: bytes, signature: str, *, prefix="") -> bool:
    """Authenticate a webhook: constant-time HMAC-SHA256 check of the raw body. Never raises; False when in doubt."""
    if not (isinstance(secret, (bytes, bytearray)) and secret and isinstance(body, (bytes, bytearray))
            and isinstance(signature, str)):
        return False
    given = signature[len(prefix):] if prefix and signature.startswith(prefix) else signature
    return hmac.compare_digest(hmac.new(bytes(secret), bytes(body), hashlib.sha256).hexdigest(), given.strip().lower())


def emission_blockers(analysis):
    """Findings from the finance analysis (#39) that block a proposal: an issue request must not go to approval
    while any of them stands. ``analysis`` is the dict returned by ``FinanceService.analyze``."""
    return [f for f in (analysis or {}).get("achados", []) if f.get("bloqueia")]
