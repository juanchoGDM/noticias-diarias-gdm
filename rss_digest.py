"""
Lee feeds RSS definidos en feeds_config.json, filtra por palabras clave,
y escribe un resumen diario al inicio de un Google Doc.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import feedparser
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/documents"]


def load_config(path="feeds_config.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def entry_matches_keywords(entry, keywords):
    text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
    return any(kw.lower() in text for kw in keywords)


def entry_is_recent(entry, max_age_hours):
    if not entry.get("published_parsed"):
        return True  # si no trae fecha, lo dejamos pasar
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
            if not entry_matches_keywords(entry, config["keywords"]):
                continue
            articles.append(
                {
                    "title": entry.get("title", "(sin título)"),
                    "link": entry.get("link", ""),
                    "source": source_name,
                }
            )
            count += 1
    return articles


def build_digest_text(articles):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📰 Resumen del {today}\n"]
    if not articles:
        lines.append("No se encontraron artículos relevantes hoy.\n")
    for art in articles:
        lines.append(f"• {art['title']} — {art['source']}")
        lines.append(f"  {art['link']}\n")
    lines.append("\n" + "=" * 40 + "\n\n")
    return "\n".join(lines)


def clear_and_write_google_doc(text, doc_id, credentials_info):
    creds = Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
    service = build("docs", "v1", credentials=creds)

    doc = service.documents().get(documentId=doc_id).execute()
    end_index = doc["body"]["content"][-1]["endIndex"]

    requests = []
    if end_index > 2:  # hay contenido previo que borrar
        requests.append(
            {
                "deleteContentRange": {
                    "range": {"startIndex": 1, "endIndex": end_index - 1}
                }
            }
        )
    requests.append(
        {
            "insertText": {
                "location": {"index": 1},
                "text": text,
            }
        }
    )
    service.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()


def main():
    config = load_config()
    articles = collect_articles(config)
    digest_text = build_digest_text(articles)

    doc_id = os.environ["GOOGLE_DOC_ID"]
    credentials_info = json.loads(os.environ["GOOGLE_CREDENTIALS"])

    clear_and_write_google_doc(digest_text, doc_id, credentials_info)
    print(f"Listo. {len(articles)} artículos agregados al Google Doc.")


if __name__ == "__main__":
    main()
