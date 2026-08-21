import json
import unittest

from src.connector_http import (
    ConnectorHttpError,
    HttpResponse,
    fetch_paginated_json,
    fetch_shopify_graphql_orders,
    fetch_stripe_list_json,
)


class ConnectorHttpTests(unittest.TestCase):
    def test_pagination_retry_and_batch_are_bounded_and_idempotent(self):
        calls, sleeps = [], []
        responses = [
            HttpResponse(429, {}, b"rate limited and secret-like-body"),
            HttpResponse(200, {}, json.dumps({"orders": [{"id": "1"}], "next_cursor": "C2"}).encode()),
            HttpResponse(200, {}, json.dumps({"orders": [{"id": "2"}], "next_cursor": None}).encode()),
        ]
        def transport(request):
            calls.append(request)
            return responses.pop(0)
        first = fetch_paginated_json(
            "https://api.example.test/v1/orders", bearer_token="TOP-SECRET", source_name="test",
            transport=transport, sleeper=sleeps.append,
        )
        self.assertEqual(first["page_count"], 2)
        self.assertEqual(first["retry_count"], 1)
        self.assertEqual(sleeps, [1])
        self.assertEqual(calls[-1].url, "https://api.example.test/v1/orders?cursor=C2")
        self.assertTrue(all(call.headers["Authorization"] == "Bearer TOP-SECRET" for call in calls))
        second_responses = [
            HttpResponse(200, {}, json.dumps({"orders": [{"id": "1"}], "next_cursor": "C2"}).encode()),
            HttpResponse(200, {}, json.dumps({"orders": [{"id": "2"}], "next_cursor": None}).encode()),
        ]
        second = fetch_paginated_json(
            "https://api.example.test/v1/orders", bearer_token="TOP-SECRET", source_name="test",
            transport=lambda request: second_responses.pop(0), sleeper=lambda seconds: None,
        )
        self.assertEqual(first["batch_id"], second["batch_id"])

    def test_errors_never_include_secret_or_response_body(self):
        secret = "DO-NOT-LEAK"
        with self.assertRaises(ConnectorHttpError) as raised:
            fetch_paginated_json(
                "https://api.example.test/v1/orders", bearer_token=secret, source_name="test",
                max_attempts=1,
                transport=lambda request: HttpResponse(401, {}, f"bad {secret}".encode()),
            )
        message = str(raised.exception)
        self.assertNotIn(secret, message)
        self.assertNotIn("bad", message)

    def test_retry_after_numeric_seconds_is_honored_and_capped_without_leaking_body(self):
        sleeps = []
        responses = [
            HttpResponse(429, {"Retry-After": "99"}, b"private rate body"),
            HttpResponse(200, {}, b'{"orders":[],"next_cursor":null}'),
        ]
        result = fetch_paginated_json(
            "https://api.example.test/v1/orders", bearer_token="private",
            source_name="test", transport=lambda request: responses.pop(0),
            sleeper=sleeps.append,
        )
        self.assertEqual(sleeps, [30.0])
        self.assertEqual(result["rate_limit_count"], 1)
        self.assertEqual(result["retry_delay_seconds_total"], 30.0)
        self.assertTrue(result["retry_after_honored"])

    def test_repeated_cursor_and_page_limit_fail_closed(self):
        responses = [
            HttpResponse(200, {}, b'{"next_cursor":"X"}'),
            HttpResponse(200, {}, b'{"next_cursor":"X"}'),
        ]
        with self.assertRaisesRegex(ConnectorHttpError, "cursor repeated"):
            fetch_paginated_json(
                "https://api.example.test/data", bearer_token="secret", source_name="x",
                transport=lambda request: responses.pop(0), sleeper=lambda seconds: None,
            )

    def test_endpoint_must_be_fixed_https_without_credentials(self):
        for endpoint in ("http://api.example.test", "https://user:pass@api.example.test", "https://api.example.test/#frag"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ConnectorHttpError):
                fetch_paginated_json(endpoint, bearer_token="x", source_name="x")

    def test_stripe_list_uses_starting_after_version_header_and_retry(self):
        calls, sleeps = [], []
        responses = [
            HttpResponse(429, {}, b"rate limit body must stay private"),
            HttpResponse(200, {}, json.dumps({
                "object": "list", "data": [{"id": "txn_1"}], "has_more": True,
                "url": "/v1/balance_transactions",
            }).encode()),
            HttpResponse(200, {}, json.dumps({
                "object": "list", "data": [{"id": "txn_2"}], "has_more": False,
                "url": "/v1/balance_transactions",
            }).encode()),
        ]

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        result = fetch_stripe_list_json(
            "https://api.stripe.com/v1/balance_transactions",
            restricted_key="rk_test_private",
            api_version="2026-06-24.dahlia",
            parameters={"limit": 100, "created[gte]": 100},
            transport=transport,
            sleeper=sleeps.append,
        )
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["rate_limit_count"], 1)
        self.assertFalse(result["retry_after_honored"])
        self.assertEqual(sleeps, [1])
        self.assertIn("starting_after=txn_1", calls[-1].url)
        self.assertIn("created%5Bgte%5D=100", calls[-1].url)
        self.assertEqual(calls[-1].headers["Stripe-Version"], "2026-06-24.dahlia")
        self.assertEqual(calls[-1].headers["Authorization"], "Bearer rk_test_private")

    def test_stripe_list_fails_closed_on_bad_contract_or_non_stripe_host(self):
        with self.assertRaisesRegex(ConnectorHttpError, "rk_ restricted"):
            fetch_stripe_list_json(
                "https://api.stripe.com/v1/payouts",
                restricted_key="sk_live_PRIVATE",
                api_version="2026-06-24.dahlia",
                transport=lambda request: self.fail("secret keys must fail before transport"),
            )
        with self.assertRaisesRegex(ConnectorHttpError, "list response contract"):
            fetch_stripe_list_json(
                "https://api.stripe.com/v1/payouts",
                restricted_key="rk_test_private",
                api_version="2026-06-24.dahlia",
                transport=lambda request: HttpResponse(200, {}, b'{"data":[],"has_more":false}'),
            )
        with self.assertRaisesRegex(ConnectorHttpError, "api.stripe.com"):
            fetch_stripe_list_json(
                "https://stripe.example.test/v1/payouts",
                restricted_key="rk_test_private",
                api_version="2026-06-24.dahlia",
            )

    def test_stripe_list_binds_connected_account_header_without_returning_it(self):
        calls = []
        result = fetch_stripe_list_json(
            "https://api.stripe.com/v1/payouts",
            restricted_key="rk_test_private",
            api_version="2026-06-24.dahlia",
            stripe_account="acct_123456789ABC",
            transport=lambda request: (
                calls.append(request)
                or HttpResponse(200, {}, b'{"object":"list","data":[],"has_more":false}')
            ),
        )
        self.assertEqual(calls[0].headers["Stripe-Account"], "acct_123456789ABC")
        self.assertNotIn("acct_123456789ABC", json.dumps(result))
        with self.assertRaisesRegex(ConnectorHttpError, "account binding"):
            fetch_stripe_list_json(
                "https://api.stripe.com/v1/payouts",
                restricted_key="rk_test_private",
                api_version="2026-06-24.dahlia",
                stripe_account="not-an-account",
            )

    def test_stripe_error_never_contains_key_or_body(self):
        secret = "rk_test_DO_NOT_LEAK"
        with self.assertRaises(ConnectorHttpError) as raised:
            fetch_stripe_list_json(
                "https://api.stripe.com/v1/payouts",
                restricted_key=secret,
                api_version="2026-06-24.dahlia",
                max_attempts=1,
                transport=lambda request: HttpResponse(401, {}, f"bad key {secret}".encode()),
            )
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn("bad key", str(raised.exception))

    def test_shopify_graphql_uses_post_versioned_store_endpoint_and_cursor(self):
        calls, sleeps = [], []
        responses = [
            HttpResponse(503, {}, b"private upstream body"),
            HttpResponse(200, {"X-Shopify-API-Version": "2026-07"}, json.dumps({
                "data": {"orders": {
                    "nodes": [{"id": "gid://shopify/Order/1"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR-1"},
                }},
            }).encode()),
            HttpResponse(200, {"X-Shopify-API-Version": "2026-07"}, json.dumps({
                "data": {"orders": {
                    "nodes": [{"id": "gid://shopify/Order/2"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": "CURSOR-2"},
                }},
            }).encode()),
        ]

        def transport(request):
            calls.append(request)
            return responses.pop(0)

        result = fetch_shopify_graphql_orders(
            "demo-store.myshopify.com",
            access_token="shpat_private",
            api_version="2026-07",
            query="query Orders { orders { nodes { id } } }",
            search_query="status:any created_at:>=2026-08-01T00:00:00Z",
            transport=transport,
            sleeper=sleeps.append,
        )
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(sleeps, [1])
        self.assertTrue(all(call.method == "POST" for call in calls))
        self.assertTrue(all(call.url == "https://demo-store.myshopify.com/admin/api/2026-07/graphql.json" for call in calls))
        self.assertTrue(all(call.headers["X-Shopify-Access-Token"] == "shpat_private" for call in calls))
        last_body = json.loads(calls[-1].body.decode())
        self.assertEqual(last_body["variables"]["after"], "CURSOR-1")
        self.assertEqual(last_body["variables"]["first"], 100)

    def test_shopify_graphql_rejects_host_version_fallforward_and_graphql_errors(self):
        for domain in ("shopify.com", "evil.myshopify.com.attacker.test", "user@store.myshopify.com"):
            with self.subTest(domain=domain), self.assertRaisesRegex(ConnectorHttpError, "myshopify.com"):
                fetch_shopify_graphql_orders(
                    domain, access_token="private", api_version="2026-07",
                    query="query { orders { nodes { id } } }", search_query="status:any",
                )
        with self.assertRaisesRegex(ConnectorHttpError, "different API version"):
            fetch_shopify_graphql_orders(
                "demo.myshopify.com", access_token="private", api_version="2026-07",
                query="query { orders { nodes { id } } }", search_query="status:any",
                transport=lambda request: HttpResponse(
                    200, {"X-Shopify-API-Version": "2026-04"},
                    b'{"data":{"orders":{"nodes":[],"pageInfo":{"hasNextPage":false}}}}',
                ),
            )
        with self.assertRaisesRegex(ConnectorHttpError, "GraphQL returned errors"):
            fetch_shopify_graphql_orders(
                "demo.myshopify.com", access_token="private", api_version="2026-07",
                query="query { orders { nodes { id } } }", search_query="status:any",
                transport=lambda request: HttpResponse(200, {}, b'{"errors":[{"message":"private"}]}'),
            )

    def test_shopify_errors_do_not_expose_token_or_response_body(self):
        token = "shpat_DO_NOT_LEAK"
        with self.assertRaises(ConnectorHttpError) as raised:
            fetch_shopify_graphql_orders(
                "demo.myshopify.com", access_token=token, api_version="2026-07",
                query="query { orders { nodes { id } } }", search_query="status:any",
                max_attempts=1,
                transport=lambda request: HttpResponse(401, {}, f"bad {token}".encode()),
            )
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn("bad", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
