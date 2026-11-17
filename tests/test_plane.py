import asyncio

import httpx

from app.plane import PlaneClient


def test_member_display_name_is_resolved_and_cached():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["member_id"] == "user-2"
        assert request.url.params["expand"] == "member"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "membership-1",
                        "member_id": "user-2",
                        "member": {
                            "id": "user-2",
                            "display_name": "Bob Jones",
                        },
                    }
                ]
            },
        )

    async def run():
        client = PlaneClient("https://plane.example.com", "acme", "api-key")
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            data = {
                "id": "work-item-1",
                "comment": {"actor_id": "user-2", "comment_stripped": "Hello"},
            }
            enriched = await client.enrich_work_item(data)
            assert enriched["comment"]["actor_name"] == "Bob Jones"
            assert await client.get_member_display_name("user-2") == "Bob Jones"
        finally:
            await client.close()

    asyncio.run(run())
    assert len(requests) == 1
