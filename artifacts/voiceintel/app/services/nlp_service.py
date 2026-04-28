import re
import logging
from collections import Counter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword lists
# ---------------------------------------------------------------------------

URGENCY_KEYWORDS = {
    "emergency", "urgent", "hospital", "death", "died", "suicide",
    "crisis", "abuse", "threat", "help me", "immediately", "asap",
    "critical", "danger", "dying", "overdose", "violence", "attacked",
}

# Each category maps to a list of whole words/phrases.
# Matching uses word-boundary regex so "pray" won't match "praying grace" (a product name).
# Order matters only for tie-breaking — highest score wins.
CATEGORY_RULES = {
    "Prayer Request": [
        r"\bpray\b", r"\bprayer\b", r"\bpray(?:ing|ed|s)\s+(?:for|request)\b",
        r"\bgod\b", r"\bchurch\b", r"\bfaith\b", r"\bblessing\b",
        r"\bjesus\b", r"\bheal(?:ing)?\b", r"\bspirit(?:ual)?\b",
        r"\bworship\b", r"\bbible\b", r"\bamen\b", r"\blord\b",
    ],
    "Donation Issue": [
        r"\bdonat(?:e|ion|ions|ed|ing)\b", r"\bpayment\b", r"\bcharge(?:d|s)?\b",
        r"\brefund\b", r"\bcredit\s+card\b", r"\bbilling\b",
        r"\bcontribut(?:e|ion|ed|ing)\b", r"\bfund(?:ing|s)?\b",
        r"\bgav(?:e|ing)\b", r"\bgiving\b",
    ],
    "Technical Issue": [
        r"\bwebsite\b", r"\bapp\b", r"\blogin\b", r"\bpassword\b",
        r"\berror\b", r"\bbroken\b", r"\bbug\b", r"\bcrash(?:ed|ing)?\b",
        r"\btechnical\b", r"\bnot\s+working\b", r"\bproblem\s+with\b",
        r"\bcan't\s+(?:log\s+in|access|open)\b",
    ],
    "Complaint": [
        r"\bcomplaint\b", r"\bunhappy\b", r"\bdisappoint(?:ed|ing|ment)\b",
        r"\bterrible\b", r"\bawful\b", r"\bhorrible\b",
        r"\bunacceptable\b", r"\bridiculous\b", r"\bworst\b",
        r"\bdisgusted?\b", r"\bnever\s+again\b",
    ],
    "Urgent": [
        r"\burgent\b", r"\bemergency\b", r"\bimmediately\b", r"\basap\b",
        r"\bcritical\b", r"\bcrisis\b", r"\bdeath\b",
        r"\bhospital\b", r"\bsuicide\b", r"\babuse\b",
    ],
    "Product Inquiry": [
        r"\boffer\b", r"\boffers?\b", r"\bpromotion\b", r"\bpromo\b",
        r"\bdeal\b", r"\bdiscount\b", r"\bpackage\b",
        r"\bpurchas(?:e|ing|ed)\b", r"\bbuy\b", r"\border(?:ing|ed)?\b",
        r"\bsign(?:ing)?\s+up\b", r"\bregister\b", r"\benroll\b",
        r"\binterested\s+in\b", r"\blearn\s+more\b", r"\bmore\s+info\b",
        r"\bpric(?:e|ing)\b", r"\bcost\b",
    ],
    "General Inquiry": [
        r"\bquestion\b", r"\bquestions\b", r"\binfo(?:rmation)?\b",
        r"\bcalling\s+(?:about|regarding|to\s+ask)\b",
        r"\bwondering\b", r"\bwanted?\s+to\s+(?:know|ask|find\s+out)\b",
        r"\bfollow[\s-]up\b", r"\bget\s+(?:in\s+touch|more\s+details?)\b",
        r"\bspeak\s+(?:with|to)\b", r"\bcontact\b",
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
    "yeah", "well", "um", "uh", "like", "its", "it's", "i'm", "i'd",
    "they're", "we're", "there", "their", "get", "got", "going", "want",
    "wanted", "call", "called", "calling", "name", "number", "phone",
    "telephone", "area", "code", "thank", "thanks", "day", "great",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _match_keywords(text_lower: str, patterns: list[str]) -> int:
    """Return count of pattern matches (whole-word aware via regex)."""
    return sum(1 for p in patterns if re.search(p, text_lower))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_keywords(text: str, top_n: int = 10) -> list[str]:
    if not text:
        return []
    words = re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())
    filtered = [w for w in words if w not in STOPWORDS]
    counter = Counter(filtered)
    return [word for word, _ in counter.most_common(top_n)]


def detect_sentiment(text: str) -> tuple[str, float]:
    if not text:
        return "neutral", 0.0

    try:
        from textblob import TextBlob
        polarity = TextBlob(text).sentiment.polarity
        if polarity > 0.1:
            return "positive", round(polarity, 3)
        elif polarity < -0.1:
            return "negative", round(polarity, 3)
        return "neutral", round(polarity, 3)
    except ImportError:
        pass

    text_lower = text.lower()
    pos = {"thank", "great", "wonderful", "blessed", "appreciate", "love", "good", "excellent", "amazing"}
    neg = {"terrible", "awful", "bad", "horrible", "disgusting", "angry", "upset", "disappointed", "worst"}
    words = set(re.findall(r"\b[a-zA-Z]+\b", text_lower))
    p, n = len(words & pos), len(words & neg)
    if p > n:
        return "positive", round(p / max(len(words), 1), 3)
    if n > p:
        return "negative", round(-n / max(len(words), 1), 3)
    return "neutral", 0.0


def detect_urgency(text: str) -> tuple[bool, list[str]]:
    if not text:
        return False, []
    text_lower = text.lower()
    # Use word-boundary matching for multi-word phrases; simple `in` for single words
    found = []
    for kw in URGENCY_KEYWORDS:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, text_lower):
            found.append(kw)
    return len(found) > 0, found


def classify_category(text: str) -> str:
    if not text:
        return "General Inquiry"
    text_lower = text.lower()
    scores = {
        cat: _match_keywords(text_lower, patterns)
        for cat, patterns in CATEGORY_RULES.items()
    }
    # Filter out zero-score categories
    nonzero = {k: v for k, v in scores.items() if v > 0}
    if not nonzero:
        return "General Inquiry"

    best_cat = max(nonzero, key=nonzero.get)
    logger.debug(f"Category scores: {scores} → {best_cat}")
    return best_cat


def analyze(text: str) -> dict:
    keywords = extract_keywords(text)
    sentiment, sentiment_score = detect_sentiment(text)
    is_urgent, urgency_kws = detect_urgency(text)
    category = classify_category(text)
    return {
        "keywords":        keywords,
        "sentiment":       sentiment,
        "sentiment_score": sentiment_score,
        "urgency_keywords": urgency_kws,
        "is_urgent":       is_urgent,
        "category":        category,
    }
