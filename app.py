import asyncio
import json
from pathlib import Path
import re
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import httpx

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
BASE_URL = "https://varecha.pravda.sk"
QUICK_MAX_MINUTES = 20
CLASSIC_MAX_MINUTES = 60

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
    "sweet": ["sladke", "palacinky", "kysnute cesto", "kolace"],
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


def parse_minutes(text: str) -> int | None:
    normalized = text.lower()
    hours_match = re.search(r"(\d{1,2})\s*(?:hod|hodin|hodín|h)\b", normalized)
    mins_match = re.search(r"(\d{1,3})\s*min", normalized)

    hours = int(hours_match.group(1)) if hours_match else 0
    mins = int(mins_match.group(1)) if mins_match else 0

    if hours_match or mins_match:
        return hours * 60 + mins
    return None


def parse_iso_or_text_minutes(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip().lower()
    if not text:
        return None

    iso_match = re.fullmatch(r"p(?:t(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?)", text)
    if iso_match:
        h = int(iso_match.group(1) or 0)
        m = int(iso_match.group(2) or 0)
        s = int(iso_match.group(3) or 0)
        total = h * 60 + m + (1 if s >= 30 else 0)
        return total if total > 0 else None

    return parse_minutes(text)


def extract_recipe_from_json_ld(soup: BeautifulSoup) -> dict:
    result = {
        "ingredients": [],
        "steps": [],
        "prep_minutes": None,
    }

    def find_recipe_nodes(node: object) -> list[dict]:
        found: list[dict] = []
        if isinstance(node, dict):
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if any(isinstance(t, str) and t.lower() == "recipe" for t in types):
                found.append(node)
            if "@graph" in node:
                found.extend(find_recipe_nodes(node["@graph"]))
        elif isinstance(node, list):
            for item in node:
                found.extend(find_recipe_nodes(item))
        return found

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        for recipe_node in find_recipe_nodes(data):
            raw_ing = recipe_node.get("recipeIngredient") or []
            if isinstance(raw_ing, list):
                for item in raw_ing:
                    txt = " ".join(str(item).split())
                    if txt and txt not in result["ingredients"]:
                        result["ingredients"].append(txt)

            raw_inst = recipe_node.get("recipeInstructions")
            if isinstance(raw_inst, str):
                txt = " ".join(raw_inst.split())
                if txt and txt not in result["steps"]:
                    result["steps"].append(txt)
            elif isinstance(raw_inst, list):
                for item in raw_inst:
                    if isinstance(item, str):
                        txt = " ".join(item.split())
                    elif isinstance(item, dict):
                        txt = " ".join(str(item.get("text") or item.get("name") or "").split())
                    else:
                        txt = ""
                    if txt and len(txt) > 3 and txt not in result["steps"]:
                        result["steps"].append(txt)

            for time_key in ("totalTime", "cookTime", "prepTime"):
                minutes = parse_iso_or_text_minutes(recipe_node.get(time_key))
                if minutes is not None:
                    result["prep_minutes"] = minutes
                    break

            if result["ingredients"] or result["steps"] or result["prep_minutes"] is not None:
                return result

    return result


def difficulty_from_minutes(prep_minutes: int | None) -> str:
    if prep_minutes is None:
        return "classic"
    if prep_minutes <= QUICK_MAX_MINUTES:
        return "quick"
    if prep_minutes <= CLASSIC_MAX_MINUTES:
        return "classic"
    return "weekend"


def estimate_difficulty(text: str) -> tuple[str, int | None]:
    prep_minutes = parse_minutes(text)
    return difficulty_from_minutes(prep_minutes), prep_minutes


async def fetch_detail_minutes(client: httpx.AsyncClient, url: str) -> int | None:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[DEBUG DETAIL FETCH] error={exc!r} | url={url}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    ld_data = extract_recipe_from_json_ld(soup)
    minutes = ld_data["prep_minutes"]

    if minutes is None:
        page_text = " ".join(soup.get_text(" ", strip=True).split())
        minutes = parse_minutes(page_text)

    print(f"[DEBUG DETAIL FETCH] minutes={minutes} | url={url}")
    return minutes


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

            soup = BeautifulSoup(resp.text, "html.parser")
            seen_urls: set[str] = set()
            candidates: list[dict] = []

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

                est_diff, prep_min = estimate_difficulty(card_text)
                snippet = card_text.replace(title, "", 1).strip()
                if len(snippet) > 160:
                    snippet = snippet[:157].rstrip() + "..."

                candidates.append(
                    {
                        "title": title,
                        "url": full_url,
                        "image": img_url,
                        "prep_minutes": prep_min,
                        "est_diff": est_diff,
                        "snippet": snippet,
                        "card_text": card_text,
                    }
                )

                if len(candidates) >= 24:
                    break

            if difficulty == "quick":
                missing_indices = [
                    idx for idx, item in enumerate(candidates) if item["prep_minutes"] is None
                ]
                if missing_indices:
                    fetched_minutes = await asyncio.gather(
                        *(fetch_detail_minutes(client, candidates[i]["url"]) for i in missing_indices)
                    )
                    for idx, minutes in zip(missing_indices, fetched_minutes):
                        if minutes is not None:
                            candidates[idx]["prep_minutes"] = minutes
                            candidates[idx]["est_diff"] = difficulty_from_minutes(minutes)

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

    recipes: list[dict] = []
    for item in candidates:
        prep_min = item["prep_minutes"]
        est_diff = item["est_diff"]
        print(f"[DEBUG] title={item['title']!r} | prep_min={prep_min} | card_text={item['card_text']!r}")

        if difficulty == "quick":
            if prep_min is None or prep_min > QUICK_MAX_MINUTES:
                continue
        elif difficulty != "all" and est_diff != difficulty:
            continue

        recipes.append(
            {
                "title": item["title"],
                "url": item["url"],
                "image": item["image"],
                "prep_minutes": prep_min,
                "difficulty": DIFFICULTY_LABELS.get(est_diff, est_diff),
                "snippet": item["snippet"],
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

    ld_data = extract_recipe_from_json_ld(soup)
    ingredients: list[str] = list(ld_data["ingredients"])
    steps: list[str] = list(ld_data["steps"])
    prep_minutes: int | None = ld_data["prep_minutes"]

    if not ingredients:
        for item in soup.select("[itemprop='recipeIngredient'], .ingrediencie li, .ingredients li"):
            txt = " ".join(item.get_text(" ", strip=True).split())
            if txt and txt not in ingredients:
                ingredients.append(txt)

    if not steps:
        for item in soup.select("[itemprop='recipeInstructions'] p, [itemprop='recipeInstructions'] li, .postup p, .postup li"):
            txt = " ".join(item.get_text(" ", strip=True).split())
            if txt and len(txt) > 5 and txt not in steps:
                steps.append(txt)

    if prep_minutes is None:
        page_text = " ".join(soup.get_text(" ", strip=True).split())
        prep_minutes = parse_minutes(page_text)

    print(
        f"[DEBUG DETAIL] title={title!r} | prep_minutes={prep_minutes} | "
        f"ingredients_count={len(ingredients)} | steps_count={len(steps)} | url={url}"
    )

    return {
        "ok": True,
        "title": title,
        "url": url,
        "prep_minutes": prep_minutes,
        "ingredients": ingredients,
        "steps": steps,
    }