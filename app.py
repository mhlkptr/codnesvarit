from datetime import datetime, timezone
from functools import lru_cache
import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx

BASE_URL = "https://varecha.pravda.sk"
SEARCH_URL = f"{BASE_URL}/vyhladavanie/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en-US;q=0.8,en;q=0.7",
}

app = FastAPI(
    title="Varecha.sk Backend Proxy",
    description="Serverovy backend na vyhladavanie a nacitavanie receptov zo stranky varecha.pravda.sk bez CORS obmedzeni.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _parse_minutes(text: str) -> int | None:
    if not text:
        return None
    lower = text.lower()
    hours_match = re.search(r"(\d+)\s*(?:h|hod|hodin|hodiny|hodina)", lower)
    mins_match = re.search(r"(\d+)\s*(?:min|minut|minuty|minuta|m)", lower)
    total = 0
    if hours_match:
        total += int(hours_match.group(1)) * 60
    if mins_match:
        total += int(mins_match.group(1))
    return total if total > 0 else None


def _map_difficulty(minutes: int | None) -> str:
    if minutes is None:
        return "classic"
    if minutes <= 25:
        return "quick"
    if minutes <= 65:
        return "classic"
    return "weekend"


def _extract_image_url(container: BeautifulSoup) -> str | None:
    img = container.find("img")
    if not img:
        return None
    for attr in ("data-src", "data-lazy-src", "srcset", "src"):
        val = img.get(attr)
        if not val:
            continue
        if attr == "srcset":
            first_part = val.split(",")[0].strip().split(" ")[0]
            if first_part:
                return urljoin(BASE_URL, first_part)
        else:
            if not val.startswith("data:image"):
                return urljoin(BASE_URL, val)
    return None


def _build_search_query(
    category: str,
    subcategory: str,
    difficulty: str,
    days: int,
    custom_query: str | None,
) -> str:
    if custom_query and custom_query.strip():
        return custom_query.strip()

    parts: list[str] = []
    if subcategory and subcategory != "all":
        parts.append(subcategory.strip())
    elif category and category != "all":
        cat_map = {
            "meat": "maso",
            "pasta": "cestoviny",
            "meatless": "bezmasite",
            "veggies": "zelenina",
        }
        parts.append(cat_map.get(category, category))

    if difficulty == "quick":
        parts.append("rychly")
    elif difficulty == "weekend":
        parts.append("pecene")

    if days >= 3 and not parts:
        parts.append("gulas")

    return " ".join(parts).strip() or "obed"


def _parse_search_results(html: str, limit: int = 18) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    recipes: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    candidate_links = soup.select("a[href*='/recepty/']")
    for link in candidate_links:
        href = link.get("href")
        if not href:
            continue
        full_url = urljoin(BASE_URL, href)
        if full_url.rstrip("/") == f"{BASE_URL}/recepty" or full_url in seen_urls:
            continue

        title = _clean_text(link.get("title") or link.get_text(" "))
        card = link.find_parent(["article", "li", "div"]) or link
        if not title or len(title) < 3:
            heading = card.find(["h2", "h3", "h4"])
            if heading:
                title = _clean_text(heading.get_text(" "))

        if not title or len(title) < 3:
            continue

        seen_urls.add(full_url)
        card_text = _clean_text(card.get_text(" "))
        minutes = _parse_minutes(card_text)
        image_url = _extract_image_url(card)

        recipes.append(
            {
                "id": full_url,
                "title": title,
                "url": full_url,
                "image": image_url,
                "prep_minutes": minutes,
                "difficulty": _map_difficulty(minutes),
                "snippet": card_text[:220] if card_text else "",
                "source": "varecha.pravda.sk",
            }
        )

        if len(recipes) >= limit:
            break

    return recipes


async def _fetch_search_html(query: str) -> tuple[str, str]:
    params = {"q": query}
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15.0) as client:
        response = await client.get(SEARCH_URL, params=params)
        response.raise_for_status()
        return response.text, str(response.url)


@lru_cache(maxsize=64)
def _cached_parse_note() -> str:
    return "cache_ready"


@app.get("/api/health")
async def health() -> dict[str, str]:
    _cached_parse_note()
    return {
        "status": "ok",
        "source": "varecha.pravda.sk",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/recipes")
async def get_recipes(
    difficulty: str = Query(default="all", description="all | quick | classic | weekend"),
    people: int = Query(default=2, ge=1, le=20),
    days: int = Query(default=1, ge=1, le=7),
    category: str = Query(default="all", description="all | meat | pasta | meatless | veggies"),
    subcategory: str = Query(default="all"),
    q: str | None = Query(default=None),
    limit: int = Query(default=15, ge=1, le=30),
) -> dict[str, Any]:
    search_query = _build_search_query(category, subcategory, difficulty, days, q)
    direct_search_url = f"{SEARCH_URL}?q={quote_plus(search_query)}"

    try:
        html, resolved_url = await _fetch_search_html(search_query)
        items = _parse_search_results(html, limit=limit)

        if difficulty != "all":
            filtered = [r for r in items if r["difficulty"] == difficulty]
            if filtered:
                items = filtered

        return {
            "ok": True,
            "query": search_query,
            "people": people,
            "days": days,
            "total_portions": people * days,
            "search_url": resolved_url or direct_search_url,
            "count": len(items),
            "recipes": items,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "query": search_query,
            "people": people,
            "days": days,
            "total_portions": people * days,
            "search_url": direct_search_url,
            "count": 0,
            "recipes": [],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }


@app.get("/api/recipe-detail")
async def get_recipe_detail(url: str = Query(..., min_length=10)) -> dict[str, Any]:
    if not url.startswith(BASE_URL):
        return {"ok": False, "error": "Povolene su iba URL adresy z domeny varecha.pravda.sk."}

    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15.0) as client:
            response = await client.get(url)
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        title_el = soup.find("h1")
        title = _clean_text(title_el.get_text(" ")) if title_el else "Recept"

        ingredients: list[str] = []
        for sel in (
            "[itemprop='recipeIngredient']",
            ".ingredients li",
            ".suroviny li",
            ".recipe-ingredients li",
        ):
            found = soup.select(sel)
            if found:
                for el in found:
                    txt = _clean_text(el.get_text(" "))
                    if txt and txt not in ingredients:
                        ingredients.append(txt)
                break

        steps: list[str] = []
        for sel in (
            "[itemprop='recipeInstructions'] li",
            "[itemprop='recipeInstructions'] p",
            ".postup li",
            ".steps li",
            ".recipe-steps li",
        ):
            found = soup.select(sel)
            if found:
                for el in found:
                    txt = _clean_text(el.get_text(" "))
                    if txt and len(txt) > 5 and txt not in steps:
                        steps.append(txt)
                break

        return {
            "ok": True,
            "url": url,
            "title": title,
            "ingredients": ingredients[:40],
            "steps": steps[:25],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "url": url,
            "error": str(exc),
        }