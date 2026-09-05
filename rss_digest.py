"""
Lee feeds RSS, filtra por palabras clave, usa la API de Claude para traducir/
describir en español y generar un análisis, actualiza el Google Doc (reemplazo
total) y envía el análisis por correo.
"""
import json
import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/documents"]
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-5"


def load_config(path="feeds_config.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def entry_matches_keywords(entry, config):
    text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
    tiene_futbol = any(kw.lower() in text for kw in config["keywords_futbol"])
    return tiene_futbol


def entry_is_recent(entry, max_age_hours):
    if not entry.get("published_parsed"):
        return True
    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    return published >= cutoff


def collect_articles(config):
    articles = []
    for feed_url in config["feeds"]:
        parsed = feedparser.parse(feed_url)
        source_name = parsed.feed.get("title", feed_url)
        count = 0
        for entry in parsed.entries:
            if count >= config.get("max_articles_per_feed", 10):
                break
            if not entry_is_recent(entry, config.get("max_age_hours", 30)):
                continue
            if not entry_matches_keywords(entry, config):
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
            articles.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # probablemente la última línea, cortada — la ignoramos
    return articles


def build_doc_prompt(articles):
    articles_json = json.dumps(articles, ensure_ascii=False, indent=2)
    return f"""Tienes esta lista de artículos encontrados hoy (en formato JSON, puede venir en inglés o español):

    {articles_json}
    
    Para CADA artículo, genera una línea con un objeto JSON (NDJSON: un objeto por línea, SIN array, SIN comas entre líneas, SIN backticks de markdown) con las claves:
    - "titulo": el título traducido o adaptado al español
    - "descripcion": una descripción corta (1-2 frases) en español
    - "link": el mismo link original, sin modificarlo
    
    Ejemplo de formato de salida (2 líneas de ejemplo):
    {{"titulo": "Ejemplo uno", "descripcion": "Descripción corta.", "link": "https://..."}}
    {{"titulo": "Ejemplo dos", "descripcion": "Descripción corta.", "link": "https://..."}}
    
    Responde SOLO con esas líneas, nada más antes ni después."""

def build_analysis_prompt(articles):
    articles_json = json.dumps(articles, ensure_ascii=False, indent=2)
    return f"""Eres el analista editorial de Gol de Mano (GDM), una cuenta de Instagram en español sobre el fútbol como fenómeno de negocio, marketing, diseño y cultura — no de análisis de partidos ni resultados.

    Tienes esta lista de artículos encontrados hoy sobre fútbol, marketing, tecnología y diseño:
    {articles_json}
    
    Antes de escribir, aplica este filtro mentalmente: el fútbol es el tema central. Un artículo importa para este análisis solo si el fútbol es el asunto propio de la noticia, o si conecta de forma clara y directa con él — negocio del fútbol (derechos de TV, patrocinios, fichajes, valoraciones de clubes, finanzas, apuestas deportivas como sponsor), marketing o tecnología aplicados al fútbol (campañas de marcas deportivas, IA o datos en clubes/ligas, VAR, analítica), diseño futbolero (camisetas, escudos, rebrands, guayos/botines, streetwear), o cultura e identidad (hinchada, ultras, fenómenos sociales alrededor del deporte). Ignora en tu análisis los artículos de marketing, tecnología o diseño que no tengan ningún vínculo con fútbol o deporte, el análisis táctico o resultados de partidos puros, y el chisme de fichajes sin sustancia que no aporte un ángulo de negocio, cultura o diseño. Ante la duda de si algo conecta con fútbol, inclúyelo — es preferible un ángulo límite que perder algo útil.
    
    Con lo que quede después de ese filtro, escribe en español un análisis breve (para leer en 2-3 minutos) que incluya:
    - Temas en común que se repiten entre varias fuentes
    - Tendencias que se puedan identificar
    - Si hay puntos de vista distintos o contradictorios sobre un mismo tema, menciónalos
    - Un cierre corto con lo más destacado del día
    
    Usa español neutro colombiano: nada de voseo rioplatense ("vos", "pensás", "andá"), sin regionalismos argentinos o españoles, sin jerga forzada tipo "parce" o "chimba". Donde sea relevante, señala explícitamente el ángulo de negocio/cultura/diseño detrás de la noticia — no te quedes solo en el titular.
    
    Tono natural y directo. No repitas la lista completa de artículos ni sus links, enfócate en el análisis. Si después del filtro no queda nada relevante para GDM, dilo brevemente en vez de forzar un análisis."""


def build_doc_text(doc_articles):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📰 Resumen del {today}\n"]
    if not doc_articles:
        lines.append("No se encontraron artículos relevantes hoy.\n")
    for art in doc_articles:
        lines.append(f"• {art['titulo']}")
        lines.append(f"  {art['descripcion']}")
        lines.append(f"  {art['link']}\n")
    return "\n".join(lines)


def clear_and_write_google_doc(text, doc_id, credentials_info):
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
    service.documents().batchUpdate(documentId=doc_id, body={"requests": requests_batch}).execute()


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

    if not articles:
        doc_text = f"📰 Resumen del {today}\n\nNo se encontraron artículos relevantes hoy.\n"
        clear_and_write_google_doc(doc_text, doc_id, credentials_info)
        send_email(
            f"Resumen de noticias - {today}",
            "No se encontraron artículos relevantes hoy.",
            gmail_address,
            gmail_app_password,
            email_to,
        )
        print("Sin artículos hoy.")
        return

    doc_response = call_claude(build_doc_prompt(articles), api_key, max_tokens=8000)
    doc_articles = parse_ndjson(doc_response)
    doc_text = build_doc_text(doc_articles)
    clear_and_write_google_doc(doc_text, doc_id, credentials_info)

    analysis = call_claude(build_analysis_prompt(articles), api_key)
    send_email(f"Resumen de noticias - {today}", analysis, gmail_address, gmail_app_password, email_to)

    print(f"Listo. {len(articles)} artículos procesados, documento actualizado y correo enviado.")


if __name__ == "__main__":
    main()
