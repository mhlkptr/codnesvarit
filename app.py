from pathlib import Path
from urllib.parse import quote_plus, urljoin
import re

from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import httpx

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
BASE_URL = "https://varecha.pravda.sk"

app = FastAPI(title="Čo dnes variť?")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CATEGORY_KEYWORDS = {
    "all": ["obed", "recept"],
    "meat": ["maso", "kuracie", "bravcove", "hovadzie", "morcacie", "ryba"],
    "pasta": ["cestoviny", "spagety", "penne", "gnocchi", "lasagne", "rezance"],
    "meatless": ["bezmasite", "syr", "sosovica", "fazula", "huby", "vajcia"],
    "veggies": ["zelenina", "zemiaky", "cuketa", "brokolica", "tekvica", "kapusta"],
}

DIFFICULTY_LABELS = {
    "all": "Všetky",
    "quick": "Rýchle",
    "classic": "Klasické",
    "weekend": "Víkendové",
}


def build_search_query(category: str, subcategory: str) -> str:
    if subcategory and subcategory != "all":
        return subcategory.strip()
    keywords = CATEGORY_KEYWORDS.get(category, CATEGORY_KEYWORDS["all"])
    return keywords[0]


def estimate_difficulty(text: str, requested_difficulty: str) -> tuple[str, int | None]:
    minutes_match = re.search(r"(\d{1,3})\s*min", text.lower())
    prep_minutes = int(minutes_match.group(1)) if minutes_match else None

    if requested_difficulty != "all":
        return requested_difficulty, prep_minutes

    if prep_minutes is not None:
        if prep_minutes <= 30:
            return "quick", prep_minutes
        if prep_minutes <= 60:
            return "classic", prep_minutes
        return "weekend", prep_minutes

    return "classic", prep_minutes


@app.get("/")
def serve_index():
    if INDEX_FILE.exists():
        return FileResponse(INDEX_FILE, media_type="text/html; charset=utf-8")
    return JSONResponse(
        status_code=404,
        content={"ok": False, "detail": "Súbor index.html sa nenašiel."},
    )


@app.get("/api/recipes")
async def get_recipes(
    category: str = Query(default="all"),
    subcategory: str = Query(default="all"),
    difficulty: str = Query(default="all"),
    people: int = Query(default=2, ge=1, le=20),
    days: int = Query(default=1, ge=1, le=7),
):
    query_text = build_search_query(category, subcategory)
    search_url = f"{BASE_URL}/vyhladavanie/?q={quote_plus(query_text)}"
    total_portions = people * days

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get(search_url)
            resp.raise_for_status()
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "query": query_text,
                "search_url": search_url,
                "total_portions": total_portions,
                "count": 0,
                "recipes": [],
                "error": str(exc),
            },
        )

    soup = BeautifulSoup(resp.text, "html.parser")
    seen_urls: set[str] = set()
    recipes: list[dict] = []

    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if "/recepty/" not in href:
            continue

        full_url = urljoin(BASE_URL, href)
        if full_url.rstrip("/") == f"{BASE_URL}/recepty" or full_url in seen_urls:
            continue

        title = " ".join(link.get_text(" ", strip=True).split())
        if not title or len(title) < 3:
            continue

        seen_urls.add(full_url)

        parent = link.find_parent(["article", "div", "li"]) or link
        card_text = " ".join(parent.get_text(" ", strip=True).split())

        img_tag = parent.find("img")
        img_url = None
        if img_tag:
            raw_img = img_tag.get("data-src") or img_tag.get("src") or ""
            if raw_img and not raw_img.startswith("data:"):
                img_url = urljoin(BASE_URL, raw_img.strip())

        est_diff, prep_min = estimate_difficulty(card_text, difficulty)
        if difficulty != "all" and est_diff != difficulty and prep_min is not None:
            continue

        snippet = card_text.replace(title, "", 1).strip()
        if len(snippet) > 160:
            snippet = snippet[:157].rstrip() + "..."

        recipes.append(
            {
                "title": title,
                "url": full_url,
                "image": img_url,
                "prep_minutes": prep_min,
                "difficulty": DIFFICULTY_LABELS.get(est_diff, est_diff),
                "snippet": snippet,
            }
        )

        if len(recipes) >= 18:
            break

    return {
        "ok": True,
        "query": query_text,
        "search_url": search_url,
        "total_portions": total_portions,
        "count": len(recipes),
        "recipes": recipes,
    }


@app.get("/api/recipe-detail")
async def get_recipe_detail(url: str = Query(...)):
    if not url.startswith("https://varecha.pravda.sk/"):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "detail": "Neplatná adresa receptu."},
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"ok": False, "url": url, "error": str(exc)},
        )

    soup = BeautifulSoup(resp.text, "html.parser")
    h1 = soup.find("h1")
    title = " ".join(h1.get_text(" ", strip=True).split()) if h1 else "Recept"

    ingredients: list[str] = []
    for item in soup.select("[itemprop='recipeIngredient'], .ingrediencie li, .ingredients li"):
        txt = " ".join(item.get_text(" ", strip=True).split())
        if txt and txt not in ingredients:
            ingredients.append(txt)

    steps: list[str] = []
    for item in soup.select("[itemprop='recipeInstructions'] p, [itemprop='recipeInstructions'] li, .postup p, .postup li"):
        txt = " ".join(item.get_text(" ", strip=True).split())
        if txt and len(txt) > 5 and txt not in steps:
            steps.append(txt)

    return {
        "ok": True,
        "title": title,
        "url": url,
        "ingredients": ingredients,
        "steps": steps,
    }