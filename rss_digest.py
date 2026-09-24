"""
Lee feeds RSS, filtra por palabras clave, usa la API de Claude para traducir/
describir en español, marcar cuáles pasan el filtro editorial de GDM, actualiza el
Google Doc (reemplazo total, resaltando los que pasan el filtro) y envía por correo
el análisis de esos artículos junto con el link del Doc.
"""
import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/documents"]
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-5"
USER_AGENT = "Mozilla/5.0 (compatible; GDM-digest/1.0)"
GOOGLE_NEWS_URL = "https://news.google.com/rss/search?q={}&hl=es-419&gl=CO&ceid=CO:es-419"


def load_config(path="feeds_config.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def compile_keywords(keywords):
    # palabra completa sin distinguir mayúsculas: "kit" no coincide con "kitchen"
    # ni "NFL" con "influencer"
    return [re.compile(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", re.IGNORECASE) for kw in keywords]


def matches_any(text, patterns):
    return any(p.search(text) for p in patterns)


def entry_text(entry):
    return entry.get("title", "") + " " + entry.get("summary", "")


def entry_is_recent(entry, max_age_hours):
    if not entry.get("published_parsed"):
        return True
    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    return published >= cutoff


def google_news_feeds(queries):
    return [GOOGLE_NEWS_URL.format(quote_plus(q)) for q in queries]


def collect_articles(config):
    futbol_patterns = compile_keywords(config["keywords_futbol"])
    excluir_patterns = compile_keywords(config.get("keywords_excluir", []))
    # los feeds de fútbol entran sin filtro de palabras; los generales necesitan
    # al menos una palabra de fútbol. La exclusión aplica a ambos.
    feed_groups = [
        (config.get("feeds_futbol", []) + google_news_feeds(config.get("google_news_futbol", [])), False),
        (config.get("feeds_generales", []), True),
    ]

    articles = []
    for feeds, requiere_futbol in feed_groups:
        for feed_url in feeds:
            try:
                parsed = feedparser.parse(feed_url, agent=USER_AGENT)
            except Exception as e:  # nunca debe tumbar la ejecución
                print(f"⚠️  Error leyendo {feed_url}: {e}")
                continue
            if not parsed.entries:
                motivo = parsed.get("bozo_exception") or parsed.get("status", "sin entradas")
                print(f"⚠️  Feed vacío o con error ({motivo}): {feed_url}")
                continue

            source_name = parsed.feed.get("title", feed_url)
            count = 0
            for entry in parsed.entries:
                if count >= config.get("max_articles_per_feed", 10):
                    break
                if not entry_is_recent(entry, config.get("max_age_hours", 30)):
                    continue
                text = entry_text(entry)
                if matches_any(text, excluir_patterns):
                    continue
                if requiere_futbol and not matches_any(text, futbol_patterns):
                    continue
                articles.append(
                    {
                        "title": entry.get("title", "(sin título)"),
                        "summary": entry.get("summary", "")[:500],
                        "link": entry.get("link", ""),
                        "source": source_name,
                    }
                )
                count += 1
    return articles


def call_claude(prompt, api_key, max_tokens=4000):
    response = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )


def parse_ndjson(text):
    articles = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            art = json.loads(line)
        except json.JSONDecodeError:
            continue  # probablemente la última línea, cortada — la ignoramos
        art["pasa_filtro"] = art.get("pasa_filtro") is True or str(art.get("pasa_filtro")).lower() == "true"
        articles.append(art)
    return articles


def build_doc_prompt(articles):
    articles_json = json.dumps(articles, ensure_ascii=False, indent=2)
    return f"""Eres el editor de Gol de Mano (GDM), una cuenta de Instagram en español sobre el fútbol como fenómeno de negocio, marketing, diseño y cultura — no de análisis de partidos ni resultados.

    Tienes esta lista de artículos encontrados hoy (en formato JSON, puede venir en inglés o español):

    {articles_json}

    Para CADA artículo, decide primero si pasa el filtro editorial de GDM: el fútbol es el tema central. Un artículo pasa el filtro solo si el fútbol es el asunto propio de la noticia, o si conecta de forma clara y directa con él — negocio del fútbol (derechos de TV, patrocinios, fichajes, valoraciones de clubes, finanzas, apuestas deportivas como sponsor), marketing o tecnología aplicados al fútbol (campañas de marcas deportivas, IA o datos en clubes/ligas, VAR, analítica), diseño futbolero (camisetas, escudos, rebrands, guayos/botines, streetwear), o cultura e identidad (hinchada, ultras, fenómenos sociales alrededor del deporte). NO pasan: artículos de marketing, tecnología o diseño sin ningún vínculo con fútbol o deporte, fútbol americano (NFL, college football), el análisis táctico o resultados de partidos puros, y el chisme de fichajes sin sustancia que no aporte un ángulo de negocio, cultura o diseño. Ante la duda de si algo conecta con fútbol, márcalo como que pasa — es preferible un ángulo límite que perder algo útil.

    Luego genera una línea con un objeto JSON (NDJSON: un objeto por línea, SIN array, SIN comas entre líneas, SIN backticks de markdown) con las claves:
    - "titulo": el título traducido o adaptado al español
    - "descripcion": una descripción corta (1-2 frases) en español
    - "link": el mismo link original, sin modificarlo
    - "pasa_filtro": true si pasa el filtro editorial de GDM, false si no

    Usa español neutro colombiano: nada de voseo rioplatense, sin regionalismos argentinos o españoles.

    Ejemplo de formato de salida (2 líneas de ejemplo):
    {{"titulo": "Ejemplo uno", "descripcion": "Descripción corta.", "link": "https://...", "pasa_filtro": true}}
    {{"titulo": "Ejemplo dos", "descripcion": "Descripción corta.", "link": "https://...", "pasa_filtro": false}}

    Responde SOLO con esas líneas, nada más antes ni después."""

def build_analysis_prompt(articles):
    articles_json = json.dumps(articles, ensure_ascii=False, indent=2)
    return f"""Eres el analista editorial de Gol de Mano (GDM), una cuenta de Instagram en español sobre el fútbol como fenómeno de negocio, marketing, diseño y cultura — no de análisis de partidos ni resultados.

    Estos son los artículos de hoy que ya pasaron el filtro editorial de GDM (fútbol como tema central y sus ramificaciones de negocio, marketing, tecnología, diseño y cultura):
    {articles_json}

    Con estos artículos, escribe en español un análisis breve (para leer en 2-3 minutos) que incluya:
    - Temas en común que se repiten entre varias fuentes
    - Tendencias que se puedan identificar
    - Si hay puntos de vista distintos o contradictorios sobre un mismo tema, menciónalos
    - Un cierre corto con lo más destacado del día
    
    Usa español neutro colombiano: nada de voseo rioplatense ("vos", "pensás", "andá"), sin regionalismos argentinos o españoles, sin jerga forzada tipo "parce" o "chimba". Donde sea relevante, señala explícitamente el ángulo de negocio/cultura/diseño detrás de la noticia — no te quedes solo en el titular.
    
    Tono natural y directo. No repitas la lista completa de artículos ni sus links, enfócate en el análisis. Si los artículos son pocos o no tienen mucha relación entre sí, dilo brevemente en vez de forzar conexiones."""


HIGHLIGHT_COLOR = {"red": 1.0, "green": 0.93, "blue": 0.55}  # amarillo suave


def utf16_len(text):
    # Google Docs cuenta posiciones en unidades UTF-16 (un emoji ocupa 2)
    return len(text.encode("utf-16-le")) // 2


def build_doc_text(doc_articles):
    """Devuelve (texto, rangos) — rangos son (inicio, fin) relativos al texto,
    en unidades UTF-16, de los artículos que pasaron el filtro."""
    today = datetime.now().strftime("%Y-%m-%d")
    total_filtro = sum(1 for a in doc_articles if a.get("pasa_filtro"))

    text = f"📰 Resumen del {today}\n"
    if doc_articles:
        text += f"🟨 Resaltados: {total_filtro} de {len(doc_articles)} artículos pasaron el filtro de GDM y van en el análisis del correo.\n"
    text += "\n"
    if not doc_articles:
        text += "No se encontraron artículos relevantes hoy.\n"

    ranges = []
    for art in doc_articles:
        block = f"• {art.get('titulo', '')}\n  {art.get('descripcion', '')}\n  {art.get('link', '')}"
        start = utf16_len(text)
        text += block
        if art.get("pasa_filtro"):
            ranges.append((start, start + utf16_len(block)))
        text += "\n\n"
    return text, ranges


def clear_and_write_google_doc(text, doc_id, credentials_info, highlight_ranges=()):
    creds = Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
    service = build("docs", "v1", credentials=creds)

    doc = service.documents().get(documentId=doc_id).execute()
    end_index = doc["body"]["content"][-1]["endIndex"]

    requests_batch = []
    if end_index > 2:  # hay contenido previo que borrar
        requests_batch.append(
            {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index - 1}}}
        )
    requests_batch.append({"insertText": {"location": {"index": 1}, "text": text}})

    # el texto nuevo hereda el formato del que había antes: se limpia el fondo de todo...
    requests_batch.append(
        {
            "updateTextStyle": {
                "range": {"startIndex": 1, "endIndex": 1 + utf16_len(text)},
                "textStyle": {},
                "fields": "backgroundColor",
            }
        }
    )
    # ...y se resaltan solo los artículos que pasaron el filtro
    for start, end in highlight_ranges:
        requests_batch.append(
            {
                "updateTextStyle": {
                    "range": {"startIndex": 1 + start, "endIndex": 1 + end},
                    "textStyle": {"backgroundColor": {"color": {"rgbColor": HIGHLIGHT_COLOR}}},
                    "fields": "backgroundColor",
                }
            }
        )
    service.documents().batchUpdate(documentId=doc_id, body={"requests": requests_batch}).execute()


def doc_url(doc_id):
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def select_filtered_articles(doc_articles, original_articles):
    """Devuelve los artículos originales (en su idioma) que pasaron el filtro,
    para que el análisis trabaje con el texto completo de la fuente."""
    by_link = {a["link"]: a for a in original_articles}
    selected = []
    for art in doc_articles:
        if not art.get("pasa_filtro"):
            continue
        original = by_link.get(art.get("link"))
        if original:
            selected.append(original)
        else:  # el modelo alteró el link: usamos la versión traducida
            selected.append(
                {"title": art.get("titulo", ""), "summary": art.get("descripcion", ""), "link": art.get("link", "")}
            )
    return selected


def send_email(subject, body, gmail_address, gmail_app_password, to_address):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to_address

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [to_address], msg.as_string())


def main():
    config = load_config()
    articles = collect_articles(config)
    today = datetime.now().strftime("%Y-%m-%d")

    api_key = os.environ["ANTHROPIC_API_KEY"]
    doc_id = os.environ["GOOGLE_DOC_ID"]
    credentials_info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    email_to = os.environ.get("EMAIL_TO", gmail_address)

    link_doc = doc_url(doc_id)
    subject = f"Resumen de noticias - {today}"

    def email_body(text):
        return f"📄 Doc con todas las noticias de hoy (las resaltadas son las del análisis):\n{link_doc}\n\n{text}"

    if not articles:
        doc_text, _ = build_doc_text([])
        clear_and_write_google_doc(doc_text, doc_id, credentials_info)
        send_email(subject, email_body("No se encontraron artículos relevantes hoy."),
                   gmail_address, gmail_app_password, email_to)
        print("Sin artículos hoy.")
        return

    doc_response = call_claude(build_doc_prompt(articles), api_key, max_tokens=8000)
    doc_articles = parse_ndjson(doc_response)
    doc_text, highlight_ranges = build_doc_text(doc_articles)
    clear_and_write_google_doc(doc_text, doc_id, credentials_info, highlight_ranges)

    filtered = select_filtered_articles(doc_articles, articles)
    if filtered:
        analysis = call_claude(build_analysis_prompt(filtered), api_key)
    else:
        analysis = "Hoy ningún artículo pasó el filtro de GDM. Igual puedes revisar la lista completa en el Doc."
    send_email(subject, email_body(analysis), gmail_address, gmail_app_password, email_to)

    print(
        f"Listo. {len(articles)} artículos encontrados, {len(doc_articles)} en el Doc, "
        f"{len(filtered)} resaltados y enviados al análisis del correo."
    )


if __name__ == "__main__":
    main()
