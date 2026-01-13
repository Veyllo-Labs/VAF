"""
Simple Document Writer Tool for Main Agent.

For quick, simple documents (short contracts, letters, messages, templates).
For complex/large documents, use document_agent instead.
"""

from vaf.tools.base import BaseTool
from pathlib import Path

class DocumentWriterTool(BaseTool):
    """
    Quick document creation tool for simple documents.
    
    Use for:
    - Short contracts/agreements
    - Letters
    - Messages (WhatsApp, Email templates)
    - Simple forms/templates
    
    For complex or large documents (>5000 chars), use document_agent instead.
    """
    
    name = "document_writer"
    description = """Creates simple structured documents (contracts, letters, messages, templates).
Supports: Text (.txt), Markdown (.md), Word (.docx).
For large/complex documents, use document_agent instead."""
    
    parameters = {
        "type": "object",
        "properties": {
            "document_type": {
                "type": "string",
                "description": "Type of document (e.g., 'contract', 'letter', 'message', 'template')"
            },
            "content": {
                "type": "string",
                "description": "Complete document content (structured text, can use {{PLACEHOLDERS}})"
            },
            "filename": {
                "type": "string",
                "description": "Filename with extension (.txt, .md, .docx)"
            },
            "format": {
                "type": "string",
                "enum": ["text", "markdown", "word"],
                "description": "Output format (default: text)"
            }
        },
        "required": ["document_type", "content", "filename"]
    }
    
    def run(self, **kwargs) -> str:
        document_type = kwargs.get('document_type', 'document')
        content = kwargs.get('content', '')
        filename = kwargs.get('filename', 'document.txt')
        format_type = kwargs.get('format', 'text')
        
        if not content:
            return "[ERROR] No content provided for document."
        
        # Auto-detect format from filename
        file_path = Path(filename)
        if file_path.suffix == '.docx':
            format_type = 'word'
        elif file_path.suffix == '.md':
            format_type = 'markdown'
        elif not file_path.suffix:
            file_path = file_path.with_suffix('.txt')
        
        try:
            if format_type == 'word':
                return self._create_word_document(file_path, content, document_type)
            elif format_type == 'markdown':
                return self._create_markdown_document(file_path, content, document_type)
            else:
                return self._create_text_document(file_path, content, document_type)
        except Exception as e:
            return f"[ERROR] Failed to create document: {e}"
    
    def _create_text_document(self, file_path: Path, content: str, doc_type: str) -> str:
        """Create plain text document."""
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return f"""### {doc_type.capitalize()} created!

**File:** {file_path.name}
**Path:** {file_path.absolute()}
**Format:** Text
**Size:** {len(content):,} characters

✅ Document saved successfully."""
    
    def _create_markdown_document(self, file_path: Path, content: str, doc_type: str) -> str:
        """Create Markdown document."""
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return f"""### {doc_type.capitalize()} created!

**File:** {file_path.name}
**Path:** {file_path.absolute()}
**Format:** Markdown
**Size:** {len(content):,} characters

✅ Markdown document saved successfully."""
    
    def _create_word_document(self, file_path: Path, content: str, doc_type: str) -> str:
        """Create Word document (.docx)."""
        try:
            from docx import Document
            
            doc = Document()
            
            # Add title
            doc.add_heading(doc_type.capitalize(), 0)
            
            # Split content into paragraphs and add to document
            for paragraph in content.split('\n\n'):
                if paragraph.strip():
                    # Check if it's a heading (starts with § or #)
                    if paragraph.strip().startswith('§') or paragraph.strip().startswith('#'):
                        doc.add_heading(paragraph.strip().lstrip('#§ '), level=2)
                    else:
                        doc.add_paragraph(paragraph.strip())
            
            doc.save(str(file_path))
            
            return f"""### {doc_type.capitalize()} created!

**File:** {file_path.name}
**Path:** {file_path.absolute()}
**Format:** Microsoft Word (.docx)
**Size:** {len(content):,} characters

✅ Word document saved successfully.
   Open with: Microsoft Word, LibreOffice, Google Docs"""
            
        except ImportError:
            return f"""[ERROR] python-docx not installed.

To create Word documents, run:
    pip install python-docx

Alternative: Save as text (.txt) or Markdown (.md) instead."""
        except Exception as e:
            return f"[ERROR] Failed to create Word document: {e}"
