"""
TASK 04A-REAL-SEARCH: Real SerpApi verification script.
Uses the EXISTING SerpApiProductSearchProvider implementation.
Can be run inside the Docker container:
  docker compose exec backend python verify_search.py
Or locally in the backend folder:
  python verify_search.py
"""
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Load .env if not in environment
if not os.getenv("SERPAPI_API_KEY"):
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from app.tools.product_search.serpapi_provider import SerpApiProductSearchProvider
from app.tools.product_search.base import (
    MissingApiKeyError,
    ProductSearchError,
    ProviderRateLimitError,
)

QUERY = "wireless headphones under 3000"
MAX_PRICE = Decimal("3000")
CURRENCY = "INR"
LIMIT = 5

api_key = (os.getenv("SERPAPI_API_KEY") or "").strip()
base_url = (os.getenv("SERPAPI_BASE_URL") or "https://serpapi.com/search").strip()

if not api_key:
    print("STATUS: FAILED")
    print("ERROR: SERPAPI_API_KEY is not configured in environment.")
    sys.exit(1)

print("=" * 60)
print("TASK 04A-REAL-SEARCH: Real SerpApi Live Product Search")
print("=" * 60)
print(f"Query           : {QUERY}")
print(f"Max Price Budget: {MAX_PRICE} {CURRENCY}")
print(f"Currency        : {CURRENCY}")
print(f"Result Limit    : {LIMIT}")
print(f"Key Configured  : True")
print(f"Base URL        : {base_url}")
print("-" * 60)


async def main():
    provider = SerpApiProductSearchProvider(api_key=api_key, base_url=base_url)

    built_query = provider.build_search_query(QUERY)
    safe_params = provider.build_request_params(built_query, currency=CURRENCY)
    safe_params["api_key"] = "[REDACTED_SECRET]"
    print(f"Built Search Query : {built_query}")
    print(f"Safe Request Params: {safe_params}")
    print("Executing real external HTTP request to SerpApi...")
    print("-" * 60)

    try:
        results = await provider.search(
            QUERY,
            max_price=MAX_PRICE,
            currency=CURRENCY,
            limit=LIMIT,
        )
    except MissingApiKeyError as e:
        print("STATUS: FAILED")
        print(f"ERROR: Missing API Key - {e}")
        return
    except ProviderRateLimitError:
        print("STATUS: FAILED")
        print("ERROR: Rate limit exceeded (HTTP 429) from SerpApi.")
        return
    except ProductSearchError as e:
        print("STATUS: FAILED")
        print(f"ERROR: Provider Error - {e} (HTTP status: {e.status_code})")
        return
    except Exception as e:
        print("STATUS: FAILED")
        print(f"ERROR: Unexpected exception: {type(e).__name__}: {e}")
        return

    print("STATUS: SUCCESS")
    print("HTTP Status: 200 OK")
    print(f"Products Returned (after budget filter): {len(results)}")
    print("-" * 60)

    if not results:
        print("Notice: Provider returned 0 products matching the budget criteria.")
        return

    print("SAMPLE PRODUCT RESULTS (Real Google Shopping data):")
    for i, p in enumerate(results[:3], 1):
        print(f"\n[{i}] Title   : {p.title}")
        print(f"    Price   : {p.price} (Numeric extracted: {p.extracted_price} {p.currency})")
        print(f"    Merchant: {p.merchant or 'Unknown'}")
        print(f"    Source  : {p.source}")
        print(f"    Has URL : {bool(p.product_url)}")
        print(f"    Rating  : {p.rating if p.rating is not None else 'N/A'}")
        if p.extensions:
            print(f"    Badges  : {', '.join(p.extensions[:2])}")

    print("\n" + "=" * 60)
    print("VERIFICATION CHECKPOINT PASSED: Real external SerpApi search succeeded.")
    print("Normalized ProductResult objects generated from live Google Shopping results.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())

