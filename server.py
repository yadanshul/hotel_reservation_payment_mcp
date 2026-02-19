from __future__ import annotations
from pathlib import Path

import json
import os
import random
import time
import uuid
import requests
import logging
from dotenv import load_dotenv
from typing import Any, Dict, List, Optional, Tuple

from mcp.server.fastmcp import FastMCP
import sys

print("Tool invoked", file=sys.stderr)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESERVATIONS_PATH = os.path.join(BASE_DIR, "reservations.json")
WIDGET_PATH = os.path.join(BASE_DIR, "public", "reservation-anshul.html")

load_dotenv()

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "hotel-reservation-app",
    # stateless_http=True,
    # json_response=True,
)

UI_URI = "ui://widget/reservation-anshul.html"
DATA_URI = "data://reservations.json"

# In-memory quote store: quote_id -> quote details
# In production, store this in Redis/DB and include idempotency keys.
QUOTES: Dict[str, Dict[str, Any]] = {}
QUOTE_TTL_SECONDS = 15 * 60  # 15 minutes


# -----------------------------
# Data helpers
# -----------------------------
def _load_reservations() -> List[Dict[str, Any]]:
    with open(RESERVATIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_reservations(reservations: List[Dict[str, Any]]) -> None:
    with open(RESERVATIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(reservations, f, indent=2, ensure_ascii=False)


def _find_reservation(reservation_number: str) -> Optional[Dict[str, Any]]:
    rn = reservation_number.strip()
    if not rn:
        return None
    for r in _load_reservations():
        if str(r.get("reservation_number", "")).strip() == rn:
            return r
    return None


def _update_reservation(reservation_number: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rn = reservation_number.strip()
    if not rn:
        return None

    reservations = _load_reservations()
    updated: Optional[Dict[str, Any]] = None

    for i, r in enumerate(reservations):
        if str(r.get("reservation_number", "")).strip() == rn:
            updated = {**r, **patch}
            reservations[i] = updated
            break

    if updated is not None:
        _save_reservations(reservations)

    return updated


def _read_widget_html() -> str:
    print("TEMPLATE CALLED----------->", WIDGET_PATH)
    p = Path(WIDGET_PATH).resolve()
    html = p.read_text(encoding="utf-8")
    return f"<!-- READ_FROM: {p} -->\n" + html


# -----------------------------
# Response helpers
# -----------------------------
def _widget_meta(invoking: str = "", invoked: str = "") -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "openai/outputTemplate": UI_URI,
        "openai/widgetAccessible": True,
    }
    if invoking:
        meta["openai/toolInvocation/invoking"] = invoking
    if invoked:
        meta["openai/toolInvocation/invoked"] = invoked
    return meta


def _tool_ok(structured: Dict[str, Any], message: str = "", invoking: str = "", invoked: str = "", *, show_widget: bool = True) -> Dict[str, Any]:
    content = [{"type": "text", "text": message}] if message else []
    if show_widget:
        content.append({"type": "resource", "uri": UI_URI})  # <-- force render
        content.append({"type": "text", "mimeType": "text/html",
                       "text": _read_widget_html()})  # <-
    return {
        "content": content,
        "structuredContent": structured,
        "_meta": _widget_meta(invoking=invoking, invoked=invoked),
    }


def _tool_err(message: str, invoking: str = "", invoked: str = "") -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": {"message": message},
        "_meta": _widget_meta(invoking=invoking, invoked=invoked),
    }


# -----------------------------
# Resources
# -----------------------------


@mcp.resource(UI_URI)
def reservation_widget_template() -> Dict[str, Any]:
    print("TEMPLATE CALLED", time.time(), "UI_URI=", UI_URI)
    html = _read_widget_html()
    html = f"""
    <!-- TEMPLATE_TS {time.time()} -->
    <div style="padding:12px;border:3px solid red;font-weight:800">
      CUSTOM TEMPLATE LOADED
    </div>
    """ + html
    return {
        "contents": [
            {
                "uri": UI_URI,
                "mimeType": "text/html",
                "text": html,
                "_meta": {"openai/widgetPrefersBorder": True},
            }
        ]
    }


@mcp.resource(DATA_URI)
def reservations_json_resource() -> Dict[str, Any]:
    with open(RESERVATIONS_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    return {"contents": [{"uri": DATA_URI, "mimeType": "application/json", "text": text}]}


# -----------------------------
# Payment skeleton (you implement internals)
# -----------------------------

# Function to get a bearer token from Amadeus OAuth2 API
def get_amadeus_bearer_token():
    """Retrieve a bearer token from the Amadeus OAuth2 API."""
    logger.info("Requesting bearer token from Amadeus OAuth2 API")
    url = "https://test.travel.api.amadeus.com/v1/security/oauth2/token"
    client_id = os.getenv("AMADEUS_CLIENT_ID")
    client_secret = os.getenv("AMADEUS_CLIENT_SECRET")
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        response = requests.post(url, data=data)
        response.raise_for_status()
        token_response = response.json()
        logger.info("Successfully retrieved bearer token from Amadeus API")
        return token_response["access_token"]
    except Exception as e:
        logger.error(f"Failed to retrieve bearer token: {e}")
        raise


def charge_payment_api(
    *,
    reservation_number: str,
    amount: int,
    currency: str,
    description: str,
    quote_id: str,
) -> Tuple[bool, str]:
    """
        Calls the Amadeus Payment Authorization API.
        Return: (success, payment_id_or_error)
    """

    url = "https://test.travel.api.amadeus.com/v2/payment/records/authorization"
    bearer_token = get_amadeus_bearer_token()
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": "*/*"
    }

    # Payment payload for authorization
    payment_payload = {
        "data": {
            "authorization": {
                "operationContext": {
                    "termsAndConditions": {
                        "credentialsOnFile": {
                            "reuseProof": {
                                "traceReference": "48XY06XLU0910"
                            },
                            "instrumentFilingRequest": "REUSE"
                        }
                    },
                    "transactionIntent": "REUSABLE",
                    "interactionCondition": "ON_FILE",
                    "transactionInitiator": "MERCHANT"
                },
                "pointOfInteraction": {
                    "referenceType": "propertyId",
                    "reference": "20182436",
                    "location": {}
                },
                "purposeOfOperation": {
                    "sales": [
                        {
                            "reference": "1927827_20182436",
                            "referenceType": "ORD"
                        }
                    ]
                }
            },
            "payee": {
                "code": "H2X"
            },
            "amount": {
                "value": str(amount),
                "currencyCode": "EUR"
            },
            "method": "CARD",
            "card": {
                "vendorCode": "CA",
                "tokenizedCardNumber": "555544G4MN3T1111",
                "expiryDate": "2030-03",
                "holderName": "Test"
            }
        }
    }

    response = requests.post(url, headers=headers, json=payment_payload)
    response.raise_for_status()
    print("payment response: ",  response.json())

    fake_payment_id = response.json()["data"]["reference"]
    return True, fake_payment_id


def _cleanup_quotes() -> None:
    now = int(time.time())
    expired = [qid for qid, q in QUOTES.items(
    ) if now - int(q["created_at"]) > QUOTE_TTL_SECONDS]
    for qid in expired:
        QUOTES.pop(qid, None)


# -----------------------------
# Tools
# -----------------------------
# @mcp.tool(name="lookup_reservation", description="Look up a hotel reservation by reservation number.")
# def lookup_reservation(reservation_number: str) -> Dict[str, Any]:
#     invoking = "Searching reservation"
#     invoked = "Reservation loaded"

#     rn = reservation_number.strip()
#     if not rn:
#         return _tool_err("Reservation number is required.", invoking=invoking, invoked=invoked)

#     r = _find_reservation(rn)
#     if not r:
#         return _tool_ok(
#             {"message": f"No reservation found for {rn}."},
#             message=f"No reservation found for {rn}.",
#             invoking=invoking,
#             invoked=invoked,
#         )

#     return _tool_ok(
#         {"message": f"Found reservation {rn}.", "reservation": r},
#         message=f"Found reservation {rn}.",
#         invoking=invoking,
#         invoked=invoked,
#     )

@mcp.tool(name="lookup_reservation", description="Look up a hotel reservation by reservation number.")
def lookup_reservation(reservation_number: str) -> Dict[str, Any]:
    invoking = "Searching reservation"
    invoked = "Reservation loaded"

    rn = reservation_number.strip()
    if not rn:
        return _tool_err("Reservation number is required.", invoking=invoking, invoked=invoked)

    r = _find_reservation(rn)
    if not r:
        return _tool_ok(
            {"message": f"No reservation found for {rn}."},
            message=f"No reservation found for {rn}.",
            invoking=invoking,
            invoked=invoked,
            show_widget=False,  # <-- no widget on not-found
        )

    return _tool_ok(
        {"message": f"Found reservation {rn}.", "data": {"reservation": r}},
        message=f"Found reservation {rn}.",
        invoking=invoking,
        invoked=invoked,
        show_widget=True,
    )


@mcp.tool(name="quote_add_breakfast", description="Provide a quote (10–99) to add breakfast to a reservation.")
def quote_add_breakfast(reservation_number: str) -> Dict[str, Any]:
    invoking = "Quoting breakfast"
    invoked = "Breakfast price quoted"

    _cleanup_quotes()

    rn = reservation_number.strip()
    r = _find_reservation(rn)
    if not r:
        return _tool_err(f"Reservation {rn} not found.", invoking=invoking, invoked=invoked)

    if bool(r.get("has_breakfast")):
        return _tool_ok(
            {"message": "Breakfast is already included.", "reservation": r},
            message="Breakfast is already included.",
            invoking=invoking,
            invoked=invoked,
        )

    amount = random.randint(10, 99)
    quote_id = f"q_{uuid.uuid4().hex[:10]}"
    quote = {
        "quote_id": quote_id,
        "amount": amount,
        "currency": "GBP",
        "item": "breakfast",
        "reservation_number": rn,
        "created_at": int(time.time()),
    }
    QUOTES[quote_id] = quote

    # Return quote payload to UI
    return _tool_ok(
        {
            "message": f"Breakfast will cost an additional £{amount}.",
            "reservation": r,
            "quote": {"quote_id": quote_id, "amount": amount, "currency": "GBP", "item": "breakfast"},
        },
        message=f"Breakfast will cost an additional £{amount}.",
        invoking=invoking,
        invoked=invoked,
    )


@mcp.tool(
    name="confirm_add_breakfast",
    description="Charge the quoted amount and amend the reservation to include breakfast.",
)
def confirm_add_breakfast(reservation_number: str, quote_id: str) -> Dict[str, Any]:
    invoking = "Processing payment"
    invoked = "Payment complete"

    _cleanup_quotes()

    rn = reservation_number.strip()
    qid = quote_id.strip()

    r = _find_reservation(rn)
    if not r:
        return _tool_err(f"Reservation {rn} not found.", invoking=invoking, invoked=invoked)

    # Guard: don't double-charge
    if bool(r.get("has_breakfast")):
        return _tool_ok(
            {"message": "Breakfast is already included.", "reservation": r},
            message="Breakfast is already included.",
            invoking=invoking,
            invoked=invoked,
        )

    q = QUOTES.get(qid)
    if not q:
        return _tool_err(
            "Quote not found or expired. Please request a new quote.",
            invoking=invoking,
            invoked=invoked,
        )

    if q.get("reservation_number") != rn:
        return _tool_err(
            "Quote does not match this reservation. Please request a new quote.",
            invoking=invoking,
            invoked=invoked,
        )

    amount = int(q["amount"])
    currency = str(q["currency"])

    # Call your payment API (skeleton)
    success, payment_id_or_error = charge_payment_api(
        reservation_number=rn,
        amount=amount,
        currency=currency,
        description="Add breakfast to reservation",
        quote_id=qid,
    )

    if not success:
        return _tool_err(f"Payment failed: {payment_id_or_error}", invoking=invoking, invoked=invoked)

    updated = _update_reservation(
        rn,
        {
            "has_breakfast": True,
            "last_payment_id": payment_id_or_error,
            "last_amendment": {
                "type": "add_breakfast",
                "amount": amount,
                "currency": currency,
                "quote_id": qid,
                "timestamp": int(time.time()),
            },
        },
    )

    if not updated:
        return _tool_err(
            "Payment succeeded, but failed to update reservation file.",
            invoking=invoking,
            invoked=invoked,
        )

    # consume the quote so it can't be reused
    QUOTES.pop(qid, None)

    return _tool_ok(
        {
            "message": f"Payment successful ({payment_id_or_error}). Breakfast added.",
            "updated_reservation": updated,
        },
        message=f"Payment successful ({payment_id_or_error}). Breakfast added.",
        invoking=invoking,
        invoked=invoked,
    )


# app = mcp.streamable_http_app()

# Use your proven ngrok configuration (host header) on Windows.
# Run: python server.py
# Then: ngrok http --host-header=localhost:8000 8000
if __name__ == "__main__":
    mcp.run()
    # import uvicorn

    # uvicorn.run("server:app", host="0.0.0.0", port=8000)

    # uvicorn.run(
    #     mcp.streamable_http_app(),
    #     host="0.0.0.0",
    #     port=8000,
    # )
