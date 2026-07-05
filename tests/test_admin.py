"""管理后台测试：鉴权（scrypt/JWT）、画像编辑校验与 TOML 落盘、反馈表、参数白名单。"""

import tomlkit

from rebas import db as database
from rebas.admin import auth
from rebas.admin.app import (
    SETTING_BOUNDS, InterestIn, ProfileIn, apply_profile_to_doc,
    validate_profile_payload,
)


class TestAuth:
    def test_password_roundtrip(self):
        salt, h = auth.hash_password("Wyx-test-123!")
        assert auth.verify_password("Wyx-test-123!", salt, h)
        assert not auth.verify_password("wrong", salt, h)
        assert not auth.verify_password("Wyx-test-123!", "垃圾", h)  # 坏散列不炸

    def test_jwt_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "jwt.secret")
        tok = auth.issue_token("a@b.c")
        assert auth.verify_token(tok) == "a@b.c"
        assert auth.verify_token(tok + "x") is None
        assert (tmp_path / "jwt.secret").stat().st_mode & 0o777 == 0o600

    def test_new_secret_revokes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "s1")
        tok = auth.issue_token("a@b.c")
        monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "s2")
        assert auth.verify_token(tok) is None  # 换密钥 = 吊销所有旧 token


PROFILE_TOML = """\
# 头部注释应保留
[board]
id = "academic"
name = "学术 · 旧名"

[[interest]]
name = "旧方向"
weight = 5
keywords = ["old"]

[low_priority]
keywords = []

[blocklist]
keywords = []

[reader]
assumed = "旧"
explain = "旧"
"""


def make_payload(**kw):
    base = dict(
        name="学术 · AI/ML",
        interests=[InterestIn(name="LLM", weight=5, keywords=["LLM", "agent"]),
                   InterestIn(name="世界模型", weight=4, keywords=["world model"])],
        low_priority=["hiring"], blocklist=["crypto spam"],
        reader_assumed="从业者", reader_explain="子领域概念白话")
    base.update(kw)
    return ProfileIn(**base)


class TestProfileEdit:
    def test_validate(self):
        assert validate_profile_payload(make_payload()) == []
        assert validate_profile_payload(make_payload(interests=[]))
        bad = make_payload(interests=[InterestIn(name="x", weight=3, keywords=["  "])])
        assert any("关键词" in e for e in validate_profile_payload(bad))

    def test_apply_to_doc(self):
        doc = tomlkit.parse(PROFILE_TOML)
        apply_profile_to_doc(doc, make_payload())
        out = tomlkit.dumps(doc)
        assert "# 头部注释应保留" in out
        assert doc["board"]["id"] == "academic"          # id 不可被编辑动到
        assert doc["board"]["name"] == "学术 · AI/ML"
        assert [i["name"] for i in doc["interest"]] == ["LLM", "世界模型"]
        assert doc["interest"][0]["keywords"] == ["LLM", "agent"]
        assert doc["low_priority"]["keywords"] == ["hiring"]
        assert doc["reader"]["assumed"] == "从业者"
        # 落盘再 parse 一遍不炸（管线 load_profile 的前提）
        assert tomlkit.parse(out)["interest"][1]["weight"] == 4


class TestFeedback:
    def test_upsert_and_cancel(self, tmp_path):
        conn = database.init_db(tmp_path / "t.sqlite")
        conn.execute(
            "INSERT INTO topics (id, issue_date, board, title, item_ids, decision,"
            " created_at) VALUES (1, '2026-07-06', 'academic', 'T', '[]', 'feature', 'x')")
        conn.execute("INSERT INTO feedback (topic_id, vote, updated_at) VALUES (1, 1, 'x')")
        conn.execute(
            "INSERT INTO feedback (topic_id, vote, updated_at) VALUES (1, -1, 'y')"
            " ON CONFLICT(topic_id) DO UPDATE SET vote=excluded.vote,"
            " updated_at=excluded.updated_at")
        assert conn.execute("SELECT vote FROM feedback WHERE topic_id=1").fetchone()[0] == -1
        conn.execute("DELETE FROM feedback WHERE topic_id=1")
        assert conn.execute("SELECT count(*) FROM feedback").fetchone()[0] == 0


class TestSettings:
    def test_bounds_cover_config_fields(self):
        from rebas.config import load_config
        conf = load_config()
        for k, (lo, hi) in SETTING_BOUNDS.items():
            v = getattr(conf, k)   # 白名单键必须真实存在于 AppConfig
            assert lo <= v <= hi, f"{k}={v} 超出白名单范围"
