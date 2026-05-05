# VAF Trusted Sources

This directory contains JSON files defining trusted sources for research and web search operations.

## 📁 Structure

```
vaf/sources/
├── news.json       # News sources (by region: DE, US, UK, International)
├── tech.json       # Tech news, documentation, developer platforms
├── academic.json   # Academic journals, databases, patents, research institutions
└── README.md       # This file
```

## 📝 JSON Format

Each JSON file follows this structure:

```json
{
  "title": "VAF Source Type",
  "description": "Description of source category",
  "version": "1.0.0",
  
  "categories": {
    "category_name": {
      "name": "Display Name",
      "description": "Category description",
      "sources": [
        {
          "name": "Source Name",
          "url": "https://example.com",
          "domains": ["example.com", "www.example.com"],
          "trust_score": 9,
          "tags": ["news", "politics", "germany"]
        }
      ]
    }
  },
  
  "metadata": {
    "last_updated": "2026-01-13",
    "maintainer": "VAF Team",
    "trust_score_scale": "1-10 (10 = highest trust)"
  }
}
```

## 🎯 Trust Score Scale

| Score | Meaning | Examples |
|-------|---------|----------|
| **10** | Gold standard - Official, peer-reviewed | Nature, Science, IEEE, Official government sites |
| **9** | Highly trusted - Reputable organizations | NYT, BBC, FAZ, Zeit, major universities |
| **8** | Trusted - Established media/tech | TechCrunch, The Verge, Heise |
| **7** | Generally reliable | Dev.to, VentureBeat |
| **6** | Medium quality - Useful but verify | Wikipedia, Google Scholar |
| **5** | Business/professional networks | LinkedIn, Xing |
| **1-4** | Low trust - Use with caution | Blogs, forums, unknown domains |
| **0** | Blacklisted - Never use | Pinterest, Quora, Bild.de |

## 🏷️ Tag System

### Common Tags

**General:**
- `news` - News source
- `analysis` - In-depth analysis
- `international` - International coverage

**Regional:**
- `germany` - German source
- `us` - US source
- `uk` - UK source
- `europe` - European coverage

**Specialized:**
- `tech` - Technology news
- `ai` - AI/ML specific
- `finance` - Financial news
- `business` - Business news
- `science` - Scientific content
- `peer-reviewed` - Peer-reviewed journals
- `documentation` - Official documentation
- `official` - Official source (government, organization)

## 🔧 Usage

### In Python Code

```python
from vaf.core.sources import get_source_manager

# Get the global source manager
sm = get_source_manager()

# Check if domain is trusted
if sm.is_trusted("bbc.com"):
    print("Trusted!")

# Get trust score
score = sm.get_trust_score("nature.com")  # Returns 10

# Get sources by category
news_de = sm.get_by_category("news_de")

# Get sources by tag
ai_sources = sm.get_by_tag("ai")

# Get sources matching multiple tags
tech_docs = sm.get_by_tags(["documentation", "official"], match_all=True)

# Get source details
source = sm.get_source_by_domain("tagesschau.de")
print(f"{source.name}: Trust score {source.trust_score}")
```

### Automatic Integration

The SourceManager is automatically used by:
- **`web_search`** - Prioritizes trusted sources in search results
- **`research_agent`** - Filters sources by trust score
- **Trust Map** - Uses JSON sources as primary, falls back to legacy hardcoded list

## ➕ Adding New Sources

### 1. Choose the Right File

- **News sources** → `news.json`
- **Tech/documentation** → `tech.json`
- **Academic/research** → `academic.json`

### 2. Add to Appropriate Category

```json
{
  "name": "Source Name",
  "url": "https://example.com",
  "domains": ["example.com"],
  "trust_score": 8,
  "tags": ["relevant", "tags"]
}
```

### 3. Guidelines

**Trust Score:**
- **10**: Only for gold-standard sources (Nature, IEEE, .gov)
- **9**: Major reputable organizations (BBC, NYT, top universities)
- **8**: Established trusted media
- **7-6**: Generally reliable but verify
- **5**: Business networks (LinkedIn)
- **<5**: Rarely use

**Domains:**
- Include all variations: `["bbc.com", "bbc.co.uk"]`
- Subdomains are automatically matched: `news.bbc.com` matches `bbc.com`

**Tags:**
- Use lowercase
- Be specific but not excessive (3-5 tags ideal)
- Follow existing tag conventions

## 🔄 Maintenance

### Updating Sources

1. Edit the appropriate JSON file
2. Update `last_updated` in metadata
3. No restart needed - sources are loaded once at startup

### Removing Sources

1. Remove the source entry from JSON
2. Update `last_updated`
3. Document reason in commit message

## 📊 Statistics

Current sources (as of 2026-01-13):

```python
from vaf.core.sources import get_source_manager
stats = get_source_manager().get_stats()
print(stats)
# {
#   'total_sources': ~90+,
#   'total_domains': ~150+,
#   'categories': 14,
#   'tags': 30+,
#   'avg_trust_score': ~8.5
# }
```

## 🛡️ Quality Control

**Before adding a source:**
1. ✅ Verify it's a real, active website
2. ✅ Check reputation and editorial standards
3. ✅ Confirm it's not paywalled (or note if it is)
4. ✅ Ensure it provides accurate, factual content
5. ✅ Check it's not already in BLACKLIST (`trust_map.py`)

**Blacklisted domains** (never add):
- Social media (Facebook, Twitter/X, Instagram, TikTok)
- SEO spam sites (Pinterest, Softonic)
- User Q&A sites (Quora, Reddit, Gutefrage)
- Tabloids (Bild.de)

## 📚 Examples

### News Query
```python
# Get all German news sources
news_de = sm.get_by_category("news_de")
# Returns: Tagesschau, Zeit, FAZ, Spiegel, etc.
```

### Tech Research
```python
# Get official documentation sources
docs = sm.get_by_tags(["documentation", "official"], match_all=True)
# Returns: Python, Rust, MDN, React docs, etc.
```

### Academic Research
```python
# Get peer-reviewed journals
journals = sm.get_by_tag("peer-reviewed")
# Returns: Nature, Science, PLOS, Cell, etc.
```

## 🤝 Contributing

To contribute new sources:

1. Fork the repo
2. Add sources to appropriate JSON file
3. Follow trust score guidelines
4. Test with `get_source_manager().get_stats()`
5. Submit PR with rationale

---

**Maintained by:** VAF Team  
**Last Updated:** 2026-01-13  
**Version:** 1.0.0
