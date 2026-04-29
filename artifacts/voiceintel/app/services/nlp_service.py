import re
import logging
from collections import Counter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword lists
# ---------------------------------------------------------------------------

# Factory defaults — used to seed the database on first run.
# Admins can modify the live list via Admin → Keywords without touching this file.
DEFAULT_URGENCY_KEYWORDS = [
    "emergency", "urgent", "hospital", "death", "died", "suicide",
    "crisis", "abuse", "threat", "help me", "immediately", "asap",
    "critical", "danger", "dying", "overdose", "violence", "attacked",
]

# Keep the old name as an alias so nothing else breaks during transition
URGENCY_KEYWORDS = set(DEFAULT_URGENCY_KEYWORDS)

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
    # Articles, conjunctions, prepositions
    "a", "an", "the", "and", "or", "but", "nor", "yet", "so", "in", "on",
    "at", "to", "for", "of", "with", "by", "from", "as", "into", "onto",
    "upon", "over", "under", "above", "below", "between", "through",
    "during", "before", "after", "since", "until", "while", "because",
    "although", "though", "unless", "however", "therefore", "thus",
    # Auxiliary / common verbs
    "is", "was", "are", "were", "be", "been", "being", "am",
    "have", "has", "had", "having", "do", "does", "did", "doing", "done",
    "will", "would", "could", "should", "may", "might", "shall", "can",
    "must", "ought",
    # Pronouns & possessives
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
    "us", "them", "my", "your", "his", "our", "their", "mine", "yours",
    "hers", "ours", "theirs", "myself", "yourself", "himself", "herself",
    "itself", "ourselves", "yourselves", "themselves",
    # Demonstratives & determiners
    "this", "that", "these", "those", "such", "some", "any", "all",
    "each", "every", "both", "either", "neither", "many", "much",
    "more", "most", "few", "fewer", "less", "least", "several",
    "other", "another", "same", "own",
    # Common adverbs / fillers
    "about", "just", "so", "up", "out", "in", "if", "then", "than",
    "too", "very", "also", "not", "no", "yes", "still", "only",
    "even", "ever", "never", "always", "often", "sometimes", "usually",
    "really", "quite", "almost", "perhaps", "maybe", "actually", "basically",
    "literally", "obviously", "definitely", "probably", "absolutely",
    "rather", "certainly", "anyway", "anyhow", "somehow", "somewhat",
    "however", "moreover", "furthermore", "indeed", "instead",
    "back", "down", "off", "away", "around", "again", "once", "twice",
    # Greetings, fillers, interjections
    "hi", "hello", "hey", "okay", "ok", "yeah", "yep", "nope", "well",
    "um", "uh", "like", "oh", "ah", "huh", "wow", "alright", "right",
    "sure", "fine", "please", "sorry", "bye", "goodbye",
    # Contractions
    "its", "it's", "i'm", "i'd", "i've", "i'll", "you're", "you've",
    "you'll", "you'd", "he's", "she's", "we're", "we've", "we'll",
    "we'd", "they're", "they've", "they'll", "they'd", "that's",
    "there's", "here's", "what's", "who's", "where's", "when's",
    "how's", "let's", "isn't", "aren't", "wasn't", "weren't", "hasn't",
    "haven't", "hadn't", "don't", "doesn't", "didn't", "won't",
    "wouldn't", "shouldn't", "couldn't", "can't", "cannot", "shan't",
    "mustn't",
    # Misc commonly stripped
    "there", "here", "where", "when", "why", "how", "what", "who",
    "whom", "whose", "which",
    # Common verbs that aren't useful as keywords
    "get", "got", "getting", "gets", "gotten",
    "go", "going", "goes", "gone", "went",
    "come", "comes", "came", "coming",
    "make", "makes", "made", "making",
    "take", "takes", "took", "taken", "taking",
    "give", "gives", "gave", "given", "giving",
    "see", "sees", "saw", "seen", "seeing",
    "know", "knows", "knew", "known", "knowing",
    "think", "thinks", "thought", "thinking",
    "say", "says", "said", "saying",
    "tell", "tells", "told", "telling",
    "want", "wants", "wanted", "wanting",
    "need", "needs", "needed", "needing",
    "try", "tries", "tried", "trying",
    "use", "uses", "used", "using",
    "find", "finds", "found", "finding",
    "let", "lets", "letting",
    "put", "puts", "putting",
    "keep", "keeps", "kept", "keeping",
    "look", "looks", "looked", "looking",
    "feel", "feels", "felt", "feeling",
    "leave", "leaves", "left", "leaving",
    "mean", "means", "meant", "meaning",
    "ask", "asks", "asked", "asking",
    "talk", "talks", "talked", "talking",
    "speak", "speaks", "spoke", "spoken", "speaking",
    "hear", "hears", "heard", "hearing",
    "show", "shows", "showed", "shown", "showing",
    "believe", "believes", "believed", "believing",
    "obtain", "obtains", "obtained", "obtaining",
    "offer", "offers", "offered", "offering",
    "help", "helps", "helped", "helping",
    "start", "starts", "started", "starting",
    "stop", "stops", "stopped", "stopping",
    "work", "works", "worked", "working",
    "run", "runs", "ran", "running",
    "happen", "happens", "happened", "happening",
    "seem", "seems", "seemed", "seeming",
    "become", "becomes", "became", "becoming",
    "remember", "remembers", "remembered", "remembering",
    "understand", "understands", "understood", "understanding",
    # Time / day fillers
    "yesterday", "today", "tomorrow", "tonight", "morning", "afternoon",
    "evening", "night", "day", "days", "week", "weeks", "month", "months",
    "year", "years", "hour", "hours", "minute", "minutes", "second",
    "seconds", "moment", "time", "times", "now", "soon", "later",
    "early", "late", "ago",
    # Call / phone vocabulary not useful as keywords
    "call", "calls", "called", "calling", "caller",
    "name", "names", "number", "numbers", "phone", "phones",
    "telephone", "area", "code", "voicemail", "message", "messages",
    "voice", "mail", "ring", "rings", "rang",
    # Politeness / sign-off
    "thank", "thanks", "thankful", "thanking",
    "great", "good", "fine", "nice", "kind",
    "appreciate", "appreciated", "appreciates", "appreciating",
    "regards", "sincerely", "best", "wish", "wishes", "wished", "wishing",
    "bless", "blessed", "blesses", "blessing", "blessings",
    # Quantity / generic
    "thing", "things", "stuff", "way", "ways", "lot", "lots",
    "kind", "kinds", "type", "types", "part", "parts",
    "person", "people", "someone", "something", "anything", "everything",
    "nothing", "nobody", "everybody", "anybody", "somebody",
    "anywhere", "everywhere", "somewhere", "nowhere",
    # Numbers as words
    "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "first", "second", "third", "next", "last",
    "previous", "final",
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


def detect_urgency(text: str, extra_keywords: list[str] | None = None) -> tuple[bool, list[str]]:
    """
    Check for urgency keywords.

    If `extra_keywords` is a non-empty list it is used as the **complete** keyword
    set (admin-managed, loaded from the DB).  If it is None or empty the built-in
    DEFAULT_URGENCY_KEYWORDS are used as a fallback so the detector still works
    when the DB is empty or unavailable.
    """
    if not text:
        return False, []
    text_lower = text.lower()
    if extra_keywords:
        all_keywords = {kw.lower().strip() for kw in extra_keywords if kw.strip()}
    else:
        all_keywords = set(URGENCY_KEYWORDS)

    found = []
    for kw in all_keywords:
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


def analyze(text: str, extra_urgency_keywords: list[str] | None = None) -> dict:
    keywords = extract_keywords(text)
    sentiment, sentiment_score = detect_sentiment(text)
    is_urgent, urgency_kws = detect_urgency(text, extra_keywords=extra_urgency_keywords)
    category = classify_category(text)
    return {
        "keywords":        keywords,
        "sentiment":       sentiment,
        "sentiment_score": sentiment_score,
        "urgency_keywords": urgency_kws,
        "is_urgent":       is_urgent,
        "category":        category,
    }
