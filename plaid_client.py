"""
plaid_client.py
Thin wrapper around the Plaid Python SDK for all operations we need.

Environment variables required:
    PLAID_CLIENT_ID
    PLAID_SECRET
    PLAID_ENV   — sandbox | development | production
"""

import os
from datetime import date, timedelta

import plaid
from plaid.api import plaid_api
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.investments_transactions_get_request import InvestmentsTransactionsGetRequest
from plaid.model.investments_transactions_get_request_options import InvestmentsTransactionsGetRequestOptions
from plaid.model.country_code import CountryCode
from plaid.model.products import Products

PLAID_CLIENT_ID = os.environ["PLAID_CLIENT_ID"]
PLAID_SECRET    = os.environ["PLAID_SECRET"]
PLAID_ENV       = os.environ.get("PLAID_ENV", "development")

_ENV_MAP = {
    "sandbox":     plaid.Environment.Sandbox,
    "development": plaid.Environment.Development,
    "production":  plaid.Environment.Production,
}

configuration = plaid.Configuration(
    host=_ENV_MAP[PLAID_ENV],
    api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
)
api_client = plaid.ApiClient(configuration)
client     = plaid_api.PlaidApi(api_client)


def create_link_token(user_id: str = "default-user") -> str:
    """
    Create a Plaid link token for the browser Link flow.
    Returns the link_token string.
    """
    request = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=user_id),
        client_name="Portracker",
        products=[Products("investments")],
        country_codes=[CountryCode("US")],
        language="en",
    )
    response = client.link_token_create(request)
    return response["link_token"]


def exchange_public_token(public_token: str) -> dict:
    """
    Exchange a public token (from browser Link flow) for a permanent access token.
    Returns {"access_token": ..., "item_id": ...}
    """
    request  = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    return {
        "access_token": response["access_token"],
        "item_id":      response["item_id"],
    }


def get_holdings(access_token: str) -> dict:
    """
    Fetch current holdings for an Item.
    Returns the full Plaid response dict with keys:
        accounts, holdings, securities
    """
    request  = InvestmentsHoldingsGetRequest(access_token=access_token)
    response = client.investments_holdings_get(request)
    return response.to_dict()


def get_transactions(access_token: str,
                     start_date: date | None = None,
                     end_date: date | None = None) -> list[dict]:
    """
    Fetch investment transactions for an Item.
    Paginates automatically.
    Returns a flat list of transaction dicts.
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)

    all_txns = []
    offset   = 0

    while True:
        options  = InvestmentsTransactionsGetRequestOptions(offset=offset)
        request  = InvestmentsTransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options=options,
        )
        response = client.investments_transactions_get(request).to_dict()
        txns     = response.get("investment_transactions", [])
        all_txns.extend(txns)

        total = response.get("total_investment_transactions", 0)
        offset += len(txns)
        if offset >= total or not txns:
            break

    return all_txns


def get_item_info(access_token: str) -> dict:
    """
    Fetch item metadata (institution name/id).
    """
    from plaid.model.item_get_request import ItemGetRequest
    from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest

    item_resp = client.item_get(ItemGetRequest(access_token=access_token)).to_dict()
    item      = item_resp["item"]

    inst_resp = client.institutions_get_by_id(
        InstitutionsGetByIdRequest(
            institution_id=item["institution_id"],
            country_codes=[CountryCode("US")],
        )
    ).to_dict()

    return {
        "item_id":          item["item_id"],
        "institution_id":   item["institution_id"],
        "institution_name": inst_resp["institution"]["name"],
    }