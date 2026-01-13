"""
VAF Query Analyzer - Intelligent Analysis of User Queries

Detects intent and suggests appropriate sources:
- News queries → News sources
- Tech queries → Tech sources
- Academic queries → Academic sources
- Specific sites → No filtering
"""

import re
from typing import List, Optional, Tuple
from dataclasses import dataclass


@dataclass
class QueryIntent:
    """Detected intent from user query."""
    intent_type: str  # "news", "tech", "academic", "general", "specific_site"
    language: str  # "de", "en", "auto"
    suggested_sources: List[str]  # List of domain names
    confidence: float  # 0.0-1.0
    reasoning: str  # Why this intent was detected


class QueryAnalyzer:
    """
    Analyzes user queries to detect intent and suggest relevant sources.
    """
    
    # Keywords for different intents (multilingual)
    NEWS_KEYWORDS = {
        "de": [
            "news", "nachrichten", "neuigkeiten", "aktuell", "aktuelles",
            "heute", "gestern", "schlagzeilen", "meldungen", "ticker",
            "pressemitteilung", "berichte", "breaking"
        ],
        "en": [
            "news", "headlines", "latest", "breaking", "current",
            "today", "yesterday", "reports", "updates", "press release"
        ]
    }
    
    TECH_KEYWORDS = {
        "de": [
            "tech", "technologie", "software", "hardware", "ki", "ai",
            "programmierung", "code", "entwicklung", "framework",
            "api", "dokumentation", "tutorial", "python", "javascript",
            "react", "vue", "rust", "go", "typescript", "java"
        ],
        "en": [
            "tech", "technology", "software", "hardware", "ai", "ml",
            "programming", "code", "development", "framework",
            "api", "documentation", "tutorial", "machine learning",
            "python", "javascript", "react", "vue", "rust", "go",
            "typescript", "java", "node", "npm", "docker"
        ]
    }
    
    ACADEMIC_KEYWORDS = {
        "de": [
            "studie", "forschung", "wissenschaft", "paper", "journal",
            "universität", "patent", "dissertation", "thesis", "papers"
        ],
        "en": [
            "study", "research", "science", "paper", "papers", "journal",
            "university", "patent", "dissertation", "thesis", "academic",
            "published", "peer-reviewed", "arxiv"
        ]
    }
    
    FINANCE_KEYWORDS = {
        "de": [
            "börse", "boerse", "aktie", "aktien", "finanzen", "wirtschaft", "markt",
            "investment", "trading", "kurs", "kurse", "dax", "dow jones"
        ],
        "en": [
            "stock", "stocks", "market", "finance", "trading", "investment",
            "economy", "dow jones", "nasdaq", "forex", "crypto", "bitcoin"
        ]
    }
    
    # Specific site patterns
    SPECIFIC_SITE_PATTERNS = [
        r'ebay[\s-]?kleinanzeigen',
        r'ebay',
        r'amazon',
        r'wikipedia',
        r'youtube',
        r'google',
        r'facebook',
        r'x.com',
        r'reddit',
        r'stackoverflow',
        r'github',
    ]
    
    def __init__(self):
        pass
    
    def analyze(self, query: str) -> QueryIntent:
        """
        Analyze a query and detect intent.
        
        Args:
            query: User query string
        
        Returns:
            QueryIntent with detected intent and suggested sources
        """
        query_lower = query.lower()
        
        # 1. Check for specific site mentions (highest priority)
        for pattern in self.SPECIFIC_SITE_PATTERNS:
            if re.search(pattern, query_lower):
                return QueryIntent(
                    intent_type="specific_site",
                    language="auto",
                    suggested_sources=[],  # No source filtering
                    confidence=1.0,
                    reasoning=f"Query mentions specific site: {pattern}"
                )
        
        # 2. Detect language (simple heuristic)
        lang = self._detect_language(query_lower)
        
        # 3. Check for news intent (try both detected language and "en" as fallback)
        news_score = max(
            self._check_keywords(query_lower, self.NEWS_KEYWORDS, lang),
            self._check_keywords(query_lower, self.NEWS_KEYWORDS, "en") if lang == "auto" else 0
        )
        if news_score > 0.5:
            # Get news sources for detected language
            sources = self._get_news_sources(lang)
            return QueryIntent(
                intent_type="news",
                language=lang,
                suggested_sources=sources,
                confidence=news_score,
                reasoning=f"Detected news query in {lang}"
            )
        
        # 4. Check for tech intent (try both detected language and "en" as fallback)
        tech_score = max(
            self._check_keywords(query_lower, self.TECH_KEYWORDS, lang),
            self._check_keywords(query_lower, self.TECH_KEYWORDS, "en") if lang == "auto" else 0
        )
        if tech_score > 0.5:
            sources = self._get_tech_sources()
            return QueryIntent(
                intent_type="tech",
                language=lang,
                suggested_sources=sources,
                confidence=tech_score,
                reasoning=f"Detected tech query"
            )
        
        # 5. Check for academic intent (try both detected language and "en" as fallback)
        academic_score = max(
            self._check_keywords(query_lower, self.ACADEMIC_KEYWORDS, lang),
            self._check_keywords(query_lower, self.ACADEMIC_KEYWORDS, "en") if lang == "auto" else 0
        )
        if academic_score > 0.5:
            sources = self._get_academic_sources()
            return QueryIntent(
                intent_type="academic",
                language=lang,
                suggested_sources=sources,
                confidence=academic_score,
                reasoning=f"Detected academic query"
            )
        
        # 6. Check for finance intent (try both detected language and "en" as fallback)
        finance_score = max(
            self._check_keywords(query_lower, self.FINANCE_KEYWORDS, lang),
            self._check_keywords(query_lower, self.FINANCE_KEYWORDS, "en") if lang == "auto" else 0
        )
        if finance_score > 0.5:
            sources = self._get_finance_sources()
            return QueryIntent(
                intent_type="finance",
                language=lang,
                suggested_sources=sources,
                confidence=finance_score,
                reasoning=f"Detected finance query"
            )
        
        # 7. Default: general search (no filtering)
        return QueryIntent(
            intent_type="general",
            language=lang,
            suggested_sources=[],
            confidence=0.5,
            reasoning="No specific intent detected - general search"
        )
    
    def _detect_language(self, query: str) -> str:
        """
        Simple language detection based on common words.
        
        Returns:
            "de" for German, "en" for English, "auto" otherwise
        """
        de_indicators = [
            "was", "ist", "sind", "der", "die", "das", "ein", "eine",
            "und", "oder", "nicht", "ich", "du", "er", "sie", "es",
            "aktuelle", "nachrichten", "wie", "wo", "wann", "warum"
        ]
        
        en_indicators = [
            "what", "is", "are", "the", "a", "an", "and", "or", "not",
            "i", "you", "he", "she", "it", "latest", "news", "how",
            "where", "when", "why"
        ]
        
        words = query.lower().split()
        de_count = sum(1 for w in words if w in de_indicators)
        en_count = sum(1 for w in words if w in en_indicators)
        
        if de_count > en_count and de_count > 0:
            return "de"
        elif en_count > de_count and en_count > 0:
            return "en"
        else:
            return "auto"
    
    def _check_keywords(self, query: str, keywords_dict: dict, lang: str) -> float:
        """
        Check how many keywords from a category appear in query.
        
        Returns:
            Score between 0.0 and 1.0
        """
        # Get keywords for detected language
        keywords = keywords_dict.get(lang, [])
        if not keywords and lang != "auto":
            # Fallback to all keywords if language not found
            keywords = []
            for kw_list in keywords_dict.values():
                keywords.extend(kw_list)
        
        if not keywords:
            return 0.0
        
        # Count matches (check if keyword is in query as whole word or part of word)
        matches = 0
        for kw in keywords:
            # Check for whole word match or substring
            if f" {kw} " in f" {query} " or kw in query:
                matches += 1
        
        # Normalize by number of keywords (with ceiling to prevent overshooting)
        score = min(matches / 2.0, 1.0)  # 2 matches = 100% confidence (reduced from 3)
        
        return score
    
    def _get_news_sources(self, lang: str) -> List[str]:
        """Get news sources for a specific language."""
        try:
            from vaf.core.sources import get_source_manager
            sm = get_source_manager()
            
            # Map language to category
            category_map = {
                "de": "news_de",
                "en": "news_us",  # Default English to US
                "auto": "news_international"
            }
            
            category = category_map.get(lang, "news_international")
            sources = sm.get_by_category(category)
            
            # Extract domains
            domains = []
            for source in sources:
                domains.extend(source.domains)
            
            return domains
        except Exception:
            return []
    
    def _get_tech_sources(self) -> List[str]:
        """Get tech news sources."""
        try:
            from vaf.core.sources import get_source_manager
            sm = get_source_manager()
            
            sources = sm.get_by_category("tech_news")
            domains = []
            for source in sources:
                domains.extend(source.domains)
            
            return domains
        except Exception:
            return []
    
    def _get_academic_sources(self) -> List[str]:
        """Get academic sources."""
        try:
            from vaf.core.sources import get_source_manager
            sm = get_source_manager()
            
            # Combine multiple academic categories
            categories = ["science_journals", "databases", "preprints"]
            domains = []
            
            for cat in categories:
                sources = sm.get_by_category(cat)
                for source in sources:
                    domains.extend(source.domains)
            
            return domains
        except Exception:
            return []
    
    def _get_finance_sources(self) -> List[str]:
        """Get finance news sources."""
        try:
            from vaf.core.sources import get_source_manager
            sm = get_source_manager()
            
            sources = sm.get_by_category("finance")
            domains = []
            for source in sources:
                domains.extend(source.domains)
            
            return domains
        except Exception:
            return []


# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL INSTANCE (Singleton Pattern)
# ═══════════════════════════════════════════════════════════════════════════

_global_query_analyzer: Optional[QueryAnalyzer] = None


def get_query_analyzer() -> QueryAnalyzer:
    """Get global QueryAnalyzer instance (singleton)."""
    global _global_query_analyzer
    if _global_query_analyzer is None:
        _global_query_analyzer = QueryAnalyzer()
    return _global_query_analyzer


# ═══════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTION
# ═══════════════════════════════════════════════════════════════════════════

def analyze_query(query: str) -> QueryIntent:
    """Quick analysis of a query."""
    return get_query_analyzer().analyze(query)
