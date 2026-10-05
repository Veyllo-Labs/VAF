# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`codesearch` (vaf/tools/codesearch.py), the coder's way to find code: it has no index, so
what its symbol patterns miss is simply not found."""
from vaf.tools.codesearch import CodeSearchTool


def _project(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "app.py").write_text(
        "class Loader:\n    async def fetch_rows(self):\n        return []\n", encoding="utf-8")
    (tmp_path / "pkg" / "view.tsx").write_text(
        "export function Banner() { return null }\nexport const useThing = () => 1\n",
        encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("def fetch_rows():\n    pass\n",
                                                       encoding="utf-8")
    return CodeSearchTool(str(tmp_path))


def test_a_method_is_found_as_a_symbol(tmp_path):
    """MUTATION: anchor the Python patterns at column 0 again."""
    out = _project(tmp_path).run(query="fetch_rows", search_type="symbol")
    assert "app.py:2" in out and "node_modules" not in out


def test_an_arrow_hook_in_a_tsx_file_is_found_as_a_symbol(tmp_path):
    """MUTATION: drop the .tsx row, so the file falls back to def/class/function."""
    tool = _project(tmp_path)
    assert "view.tsx:2" in tool.run(query="useThing", search_type="symbol")
    assert "view.tsx:1" in tool.run(query="Banner", search_type="symbol")


def test_a_path_outside_the_project_is_searched_inside_it(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.py").write_text("def fetch_rows():\n    pass\n", encoding="utf-8")
    out = _project(tmp_path).run(query="fetch_rows", search_type="text", path=str(outside))
    assert "secret.py" not in out and "app.py" in out


def test_a_symbol_query_with_regex_characters_does_not_break_the_search(tmp_path):
    (tmp_path / "x.rb").write_text("def total(items)\nend\n", encoding="utf-8")
    out = CodeSearchTool(str(tmp_path)).run(query="total(", search_type="symbol")
    assert "x.rb:1" in out
