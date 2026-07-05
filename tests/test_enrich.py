"""enrich 阶段测试：secrets 解析 / arXiv ID 提取 / OpenAlex 响应解析（不打真 API）。"""

from rebas.agents.stages import (
    _ARXIV_ID_RE, parse_openalex_authors, parse_openalex_works,
)
from rebas.config import load_secrets


def test_load_secrets(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# 注释行\n"
        "OpenAlexAPI=abc123\n"
        "QUOTED='v with space'\n"
        "无等号的坏行\n",
        encoding="utf-8")
    s = load_secrets(env)
    assert s["OpenAlexAPI"] == "abc123"
    assert s["QUOTED"] == "v with space"
    assert len(s) == 2
    assert load_secrets(tmp_path / "不存在.env") == {}


def test_arxiv_id_extraction():
    m = _ARXIV_ID_RE.search("https://arxiv.org/abs/2607.01237")
    assert m and m.group(1) == "2607.01237"
    m = _ARXIV_ID_RE.search("https://arxiv.org/pdf/2607.01237v2")
    assert m and m.group(1) == "2607.01237"       # 版本后缀不进 ID
    assert _ARXIV_ID_RE.search("https://example.com/post") is None


def test_parse_openalex_works():
    payload = {"results": [{
        "doi": "https://doi.org/10.48550/arxiv.2607.00100",
        "cited_by_count": 3,
        "authorships": [
            {"author": {"id": "https://openalex.org/A1"},
             "institutions": [{"display_name": "A Very Long Institution Name" + "x" * 40}]},
            {"author": {"id": "https://openalex.org/A2"}, "institutions": []},
            {"author": {"id": "https://openalex.org/A3"}, "institutions": []},
            {"author": {"id": "https://openalex.org/A4"}, "institutions": []},
        ],
    }, {
        "doi": None,      # 无 DOI 的记录直接跳过
    }]}
    works = parse_openalex_works(payload)
    assert list(works) == ["10.48550/arxiv.2607.00100"]
    w = works["10.48550/arxiv.2607.00100"]
    assert w["cites"] == 3
    assert w["author_ids"] == ["A1", "A2", "A4"]   # 前两位 + 末位
    assert len(w["inst"]) == 40                    # 机构名截断


def test_parse_openalex_authors():
    payload = {"results": [
        {"id": "https://openalex.org/A1", "summary_stats": {"h_index": 42}},
        {"id": "https://openalex.org/A2", "summary_stats": {}},
        {"id": "https://openalex.org/A3"},
    ]}
    h = parse_openalex_authors(payload)
    assert h == {"A1": 42, "A2": 0, "A3": 0}
