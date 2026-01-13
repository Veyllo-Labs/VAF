"""
VAF Source Manager - Centralized Management of Trusted Sources

Loads and manages trusted sources from JSON files for:
- News sources (by region)
- Tech sources (documentation, news)
- Academic sources (journals, databases, patents)

Sources are categorized and rated by trust score for quality filtering.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Set
from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    """Represents a single trusted source."""
    name: str
    url: str
    domains: tuple  # Changed from List to tuple for hashability
    trust_score: int
    tags: tuple  # Changed from List to tuple for hashability
    category: str = ""


class SourceManager:
    """
    Manages trusted sources from JSON files.
    
    Features:
    - Load sources from JSON files (news.json, tech.json, academic.json)
    - Get sources by category or tag
    - Check if a domain is trusted
    - Get trust score for a domain
    """
    
    def __init__(self, sources_dir: Optional[Path] = None):
        """
        Initialize SourceManager.
        
        Args:
            sources_dir: Path to sources directory (default: vaf/sources/)
        """
        if sources_dir is None:
            # Default to vaf/sources/ relative to this file
            sources_dir = Path(__file__).parent.parent / "sources"
        
        self.sources_dir = Path(sources_dir)
        self.sources: List[Source] = []
        self.domains_index: Dict[str, Source] = {}  # domain -> Source
        self.category_index: Dict[str, List[Source]] = {}  # category -> [Sources]
        self.tag_index: Dict[str, List[Source]] = {}  # tag -> [Sources]
        
        self._load_all_sources()
    
    def _load_all_sources(self):
        """Load all JSON source files from sources directory."""
        if not self.sources_dir.exists():
            return
        
        # Load each JSON file
        json_files = list(self.sources_dir.glob("*.json"))
        for json_file in json_files:
            self._load_source_file(json_file)
    
    def _load_source_file(self, file_path: Path):
        """Load sources from a single JSON file."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            categories = data.get("categories", {})
            
            for category_key, category_data in categories.items():
                sources_list = category_data.get("sources", [])
                
                for source_data in sources_list:
                    source = Source(
                        name=source_data.get("name", ""),
                        url=source_data.get("url", ""),
                        domains=tuple(source_data.get("domains", [])),
                        trust_score=source_data.get("trust_score", 5),
                        tags=tuple(source_data.get("tags", [])),
                        category=category_key
                    )
                    
                    # Add to main list
                    self.sources.append(source)
                    
                    # Index by domains
                    for domain in source.domains:
                        self.domains_index[domain.lower()] = source
                    
                    # Index by category
                    if category_key not in self.category_index:
                        self.category_index[category_key] = []
                    self.category_index[category_key].append(source)
                    
                    # Index by tags
                    for tag in source.tags:
                        if tag not in self.tag_index:
                            self.tag_index[tag] = []
                        self.tag_index[tag].append(source)
        
        except Exception as e:
            # Silent fail - don't crash if a JSON file is malformed
            pass
    
    # ═══════════════════════════════════════════════════════════════════════════
    # QUERY METHODS
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_all_sources(self) -> List[Source]:
        """Get all loaded sources."""
        return self.sources
    
    def get_by_category(self, category: str) -> List[Source]:
        """
        Get sources by category.
        
        Args:
            category: Category name (e.g., "news_de", "tech_news", "science_journals")
        
        Returns:
            List of sources in that category
        """
        return self.category_index.get(category, [])
    
    def get_by_tag(self, tag: str) -> List[Source]:
        """
        Get sources by tag.
        
        Args:
            tag: Tag name (e.g., "news", "ai", "peer-reviewed")
        
        Returns:
            List of sources with that tag
        """
        return self.tag_index.get(tag, [])
    
    def get_by_tags(self, tags: List[str], match_all: bool = False) -> List[Source]:
        """
        Get sources matching one or more tags.
        
        Args:
            tags: List of tags to match
            match_all: If True, source must have ALL tags. If False, ANY tag matches.
        
        Returns:
            List of matching sources
        """
        if not tags:
            return []
        
        if match_all:
            # Source must have ALL tags
            result = set(self.get_by_tag(tags[0]))
            for tag in tags[1:]:
                result &= set(self.get_by_tag(tag))
            return list(result)
        else:
            # Source must have ANY tag
            result = set()
            for tag in tags:
                result |= set(self.get_by_tag(tag))
            return list(result)
    
    def is_trusted(self, domain: str) -> bool:
        """
        Check if a domain is in the trusted sources list.
        
        Args:
            domain: Domain to check (e.g., "bbc.com")
        
        Returns:
            True if domain is trusted, False otherwise
        """
        domain = domain.lower()
        
        # Exact match
        if domain in self.domains_index:
            return True
        
        # Partial match (e.g., "news.bbc.com" matches "bbc.com")
        for trusted_domain in self.domains_index.keys():
            if domain.endswith(f".{trusted_domain}") or domain == trusted_domain:
                return True
        
        return False
    
    def get_trust_score(self, domain: str) -> int:
        """
        Get trust score for a domain.
        
        Args:
            domain: Domain to check
        
        Returns:
            Trust score (1-10), or 0 if not found
        """
        domain = domain.lower()
        
        # Exact match
        if domain in self.domains_index:
            return self.domains_index[domain].trust_score
        
        # Partial match
        for trusted_domain, source in self.domains_index.items():
            if domain.endswith(f".{trusted_domain}") or domain == trusted_domain:
                return source.trust_score
        
        return 0
    
    def get_source_by_domain(self, domain: str) -> Optional[Source]:
        """
        Get source object by domain.
        
        Args:
            domain: Domain to look up
        
        Returns:
            Source object if found, None otherwise
        """
        domain = domain.lower()
        
        # Exact match
        if domain in self.domains_index:
            return self.domains_index[domain]
        
        # Partial match
        for trusted_domain, source in self.domains_index.items():
            if domain.endswith(f".{trusted_domain}") or domain == trusted_domain:
                return source
        
        return None
    
    def get_all_domains(self) -> Set[str]:
        """Get all trusted domains as a set."""
        return set(self.domains_index.keys())
    
    def get_categories(self) -> List[str]:
        """Get all available category names."""
        return list(self.category_index.keys())
    
    def get_tags(self) -> List[str]:
        """Get all available tag names."""
        return list(self.tag_index.keys())
    
    # ═══════════════════════════════════════════════════════════════════════════
    # STATISTICS
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_stats(self) -> Dict:
        """Get statistics about loaded sources."""
        return {
            "total_sources": len(self.sources),
            "total_domains": len(self.domains_index),
            "categories": len(self.category_index),
            "tags": len(self.tag_index),
            "avg_trust_score": sum(s.trust_score for s in self.sources) / len(self.sources) if self.sources else 0,
        }


# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL INSTANCE (Singleton Pattern)
# ═══════════════════════════════════════════════════════════════════════════

_global_source_manager: Optional[SourceManager] = None


def get_source_manager() -> SourceManager:
    """
    Get global SourceManager instance (singleton).
    
    Returns:
        SourceManager instance
    """
    global _global_source_manager
    if _global_source_manager is None:
        _global_source_manager = SourceManager()
    return _global_source_manager


# ═══════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def is_trusted_domain(domain: str) -> bool:
    """Quick check if domain is trusted."""
    return get_source_manager().is_trusted(domain)


def get_domain_trust_score(domain: str) -> int:
    """Quick check for domain trust score."""
    return get_source_manager().get_trust_score(domain)
