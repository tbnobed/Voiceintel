import re
import logging
from collections import Counter

logger = logging.getLogger(__name__)

URGENCY_KEYWORDS = {
    "emergency", "urgent", "hospital", "death", "died", "suicide",
    "crisis", "abuse", "threat", "help me", "immediately", "asap",
    "critical", "danger", "dying", "overdose", "violence", "attacked",
}

CATEGORY_RULES = {
    "Prayer Request": [
        "pray", "prayer", "god", "church", "faith", "blessing", "jesus",
        "heal", "spirit", "worship", "bible", "amen", "lord",
    ],
    "Donation Issue": [
        "donate", "donation", "payment", "charge", "refund", "credit card",
        "billing", "contribute", "fund", "gave", "giving",
    ],
    "Technical Issue": [
        "website", "app", "login", "password", "error", "broken", "bug",
        "crash", "technical", "not working", "issue", "problem with",
    ],
    "Complaint": [
        "complaint", "unhappy", "disappointed", "terrible", "awful",
        "horrible", "unacceptable", "ridiculous", "worst", "disgusted",
        "never again",
    ],
    "Urgent": [
        "urgent", "emergency", "immediately", "asap", "critical", "crisis",
        "death", "hospital", "suicide", "abuse",
    ],
}

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "was", "are", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "i", "you", "he",
    "she", "it", "we", "they", "me", "him", "her", "us", "them", "my",
    "your", "his", "our", "their", "this", "that", "these", "those",
    "about", "just", "so", "up", "out", "if", "then", "than", "too",
    "very", "also", "not", "no", "am", "hi", "hello", "yes", "okay",
    "yeah", "well", "um", "uh", "like",
}


def extract_keywords(text, top_n=10):
    if not text:
        return []
    words = re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())
    filtered = [w for w in words if w not in STOPWORDS]
    counter = Counter(filtered)
    return [word for word, _ in counter.most_common(top_n)]


def detect_sentiment(text):
    if not text:
        return "neutral", 0.0

    try:
        from textblob import TextBlob
        blob = TextBlob(text)
        polarity = blob.sentiment.polarity
        if polarity > 0.1:
            return "positive", round(polarity, 3)
        elif polarity < -0.1:
            return "negative", round(polarity, 3)
        else:
            return "neutral", round(polarity, 3)
    except ImportError:
        pass

    text_lower = text.lower()
    positive_words = {"thank", "great", "wonderful", "blessed", "appreciate", "love", "good", "excellent", "amazing"}
    negative_words = {"terrible", "awful", "bad", "horrible", "disgusting", "angry", "upset", "disappointed", "worst"}

    words = set(re.findall(r"\b[a-zA-Z]+\b", text_lower))
    pos_count = len(words & positive_words)
    neg_count = len(words & negative_words)

    if pos_count > neg_count:
        return "positive", round(pos_count / max(len(words), 1), 3)
    elif neg_count > pos_count:
        return "negative", round(-neg_count / max(len(words), 1), 3)
    return "neutral", 0.0


def detect_urgency(text):
    if not text:
        return False, []
    text_lower = text.lower()
    found = [kw for kw in URGENCY_KEYWORDS if kw in text_lower]
    return len(found) > 0, found


def classify_category(text):
    if not text:
        return "General Inquiry"
    text_lower = text.lower()
    scores = {}
    for category, keywords in CATEGORY_RULES.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[category] = score
    if not scores:
        return "General Inquiry"
    return max(scores, key=scores.get)


def analyze(text):
    keywords = extract_keywords(text)
    sentiment, sentiment_score = detect_sentiment(text)
    is_urgent, urgency_kws = detect_urgency(text)
    category = classify_category(text)
    return {
        "keywords": keywords,
        "sentiment": sentiment,
        "sentiment_score": sentiment_score,
        "urgency_keywords": urgency_kws,
        "is_urgent": is_urgent,
        "category": category,
    }
