"""
VAF Trust Map - Multi-Layer Source Quality Rating

Categorizes and rates URLs by trustworthiness to prevent low-quality sources
from polluting research reports.
"""

from __future__ import annotations

from urllib.parse import urlparse
from typing import Dict, List, Tuple

# ═══════════════════════════════════════════════════════════════════════════════
# TRUST MAP - Categorized Trusted Domains
# ═══════════════════════════════════════════════════════════════════════════════

# A. Science & Research (Highest priority for facts)
SCIENCE_DOMAINS = [
    "arxiv.org",           # Preprints (Physics, CS, Math)
    "nature.com",          # Top-tier science
    "sciencemag.org",      # Top-tier science
    "springer.com",        # Scientific books/papers
    "ieee.org",            # Engineering & technology
    "acm.org",             # Computer Science
    "researchgate.net",    # (Caution: User uploads, but usually ok)
    "pubmed.ncbi.nlm.nih.gov", # Medicine & biology
    "patents.google.com",  # Google Patents - official patent database
    "fraunhofer.de",       # German research
    "max-planck.de",       # German research
    "mpg.de",              # Max Planck Society
    "dfg.de",              # German Research Foundation
]

# B. Official & Government (Data & laws)
GOV_DOMAINS = [
    ".gov",                # US government
    ".bund.de",            # German government
    ".europa.eu",          # EU
    ".int",                # International orgs (WHO, UN)
    "destatis.de",         # German Federal Statistical Office
    "statista.com",        # (Often paywall, but snippets are good)
    "bundestag.de",        # German Bundestag
    "bmbf.de",             # German Ministry of Education and Research
]

# C. Tech & Documentation (For IT/code topics)
TECH_DOCS = [
    "github.com",          # Readmes/code only
    "readthedocs.io",      # Documentation
    "stackoverflow.com",   # Concrete problem solutions
    "developer.mozilla.org", # Web standards
    "docs.microsoft.com",  # Microsoft documentation
    "aws.amazon.com",      # AWS documentation
    "w3.org",              # Web standards
    "python.org",          # Official Python
    "rust-lang.org",       # Official Rust
    "golang.org",          # Official Go
]

# D. Business & Professional Networks (Useful for business information, company profiles, professional contacts)
BUSINESS_DOMAINS = [
    "linkedin.com",        # Professional network - useful for business info, company profiles
    "xing.com",            # German professional network - useful for business info, company profiles
]

# Trust Map (consolidated)
TRUSTED_MAP: Dict[str, List[str]] = {
    "science": SCIENCE_DOMAINS,
    "gov": GOV_DOMAINS,
    "tech": TECH_DOCS,
    "business": BUSINESS_DOMAINS,
}

# Domains to actively avoid (SEO spam, social media without login)
BLACKLIST = [
    "pinterest.",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "quora.com",
    "gutefrage.net",
    "bild.de",
    "softonic.com",
    "reddit.com",          # User-generated, often unreliable
    "twitter.com",
    "x.com",
]


# ═══════════════════════════════════════════════════════════════════════════════
# QUALITY RATING FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def rate_url_quality(url: str) -> Tuple[int, str]:
    """
    Rates a URL with a score from 0 to 10 and a category.
    
    Returns:
        (score, category) - score 0-10, category string (e.g., "science", "gov", "tech", "low")
    """
    if not url or not url.startswith(("http://", "https://")):
        return (0, "invalid")
    
    try:
        parsed_url = urlparse(url)
        netloc = parsed_url.netloc.lower()
    except Exception:
        return (0, "invalid")
    
    # 1. Check blacklist (immediate exclusion)
    if any(blocked in netloc for blocked in BLACKLIST):
        return (0, "blacklisted")
    
    # 2. Special handling for Google Scholar (medium quality - broad search but not peer-reviewed)
    if "scholar.google.com" in netloc or "scholar.google." in netloc:
        return (6, "scholar")  # Medium quality - provides broad scholarly literature search
    
    # 3. Check high-trust domains
    for category, domains in TRUSTED_MAP.items():
        for domain in domains:
            # Check for exact domain or suffix (e.g., .gov)
            if domain.startswith("."):
                # Suffix match (e.g., .gov, .edu)
                if netloc.endswith(domain):
                    if category == "science" or category == "gov":
                        return (10, category)  # Gold standard
                    elif category == "tech":
                        return (8, category)
                    elif category == "business":
                        return (5, category)  # Medium quality - useful for business info
            else:
                # Exact domain or subdomain
                if netloc == domain or netloc.endswith(f".{domain}"):
                    if category == "science" or category == "gov":
                        return (10, category)  # Gold standard
                    elif category == "tech":
                        return (8, category)
                    elif category == "business":
                        return (5, category)  # Medium quality - useful for business info
    
    # 4. Bonus for educational institutions (universal)
    if netloc.endswith(".edu") or netloc.endswith(".ac.uk") or netloc.endswith(".edu.au"):
        return (9, "academic")
    
    # 5. Bonus for Wikipedia (good for overview, but not gold)
    if "wikipedia.org" in netloc:
        return (6, "wikipedia")
    
    # 6. Default: Low quality (blogs, forums, unknown domains)
    return (1, "low")


def filter_results_by_quality(
    results: List[Dict[str, str]],
    min_score: int = 4,
    max_results: int = 10
) -> Tuple[List[Dict[str, str]], int, str]:
    """
    Filters search results by quality score.
    
    Args:
        results: List of dicts with 'href', 'title', 'body'
        min_score: Minimum score (0-10)
        max_results: Maximum number of results
    
    Returns:
        (filtered_results, lowest_score, quality_warning)
        - filtered_results: Filtered list
        - lowest_score: Lowest score in the filtered list
        - quality_warning: Warning message if low quality (or "")
    """
    if not results:
        return ([], 0, "")
    
    # Rate all URLs
    rated: List[Tuple[Dict[str, str], int, str]] = []
    for res in results:
        url = res.get("href", "") or res.get("link", "")
        score, category = rate_url_quality(url)
        rated.append((res, score, category))
    
    # Sort by score (highest first)
    rated.sort(key=lambda x: x[1], reverse=True)
    
    # Filter by min_score
    filtered = [(res, score, cat) for res, score, cat in rated if score >= min_score]
    
    # If too few results, lower threshold gradually
    if len(filtered) < 3 and min_score > 1:
        # Allow lower scores too
        filtered = [(res, score, cat) for res, score, cat in rated if score >= max(1, min_score - 2)]
    
    # Limit to max_results
    filtered = filtered[:max_results]
    
    if not filtered:
        return ([], 0, "")
    
    # Extract only the results
    filtered_results = [res for res, score, cat in filtered]
    lowest_score = min(score for _, score, _ in filtered)
    
    # Generate warning based on lowest score
    quality_warning = ""
    if lowest_score < 4:
        quality_warning = (
            "⚠️ Note: Some information is based on unverified sources. "
            "Please verify critically."
        )
    elif lowest_score < 6:
        quality_warning = (
            "ℹ️ Note: Some sources have medium quality. "
            "For critical information, additional sources should be consulted."
        )
    
    return (filtered_results, lowest_score, quality_warning)


def find_optimal_threshold(results: List[Dict[str, str]], target_count: int = 5) -> Tuple[int, str]:
    """
    Finds the optimal quality threshold to get target_count results.
    
    Implements "dynamic adjustment" logic:
    - Start with score >= 7 (Science/Gov)
    - If empty -> score >= 4 (Wikipedia, academic)
    - If still empty -> score >= 1 (Blogs, forums) with warning
    
    Returns:
        (threshold, warning_message) - threshold to use, warning if low quality (or "")
    """
    thresholds = [7, 4, 1]
    warnings = ["", "", "⚠️ Note: Information is based on unverified sources. Please verify critically."]
    
    for threshold, warning in zip(thresholds, warnings):
        filtered, _, _ = filter_results_by_quality(results, min_score=threshold, max_results=target_count * 2)
        if len(filtered) >= target_count:
            return (threshold, warning)
    
    # Fallback: Take everything with warning
    return (1, "⚠️ Note: Information is based on unverified sources. Please verify critically.")

