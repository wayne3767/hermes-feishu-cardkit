"""Opt-in wire check: create and update one UNSENT CardKit entity; never send IM.

Requires the runtime's lark-oapi and python-dotenv. Credentials are read in process
from --env-file or the environment. No recipient, message API or credential output.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import time
import types
import uuid


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    from dotenv import dotenv_values
    import lark_oapi as lark
    from lark_oapi.api.cardkit.v1 import (
        CreateCardRequest, CreateCardRequestBody, ContentCardElementRequest,
        ContentCardElementRequestBody, UpdateCardRequest, UpdateCardRequestBody,
        Card, BatchUpdateCardRequest, BatchUpdateCardRequestBody,
    )

    env = dotenv_values(args.env_file) if args.env_file else os.environ
    if not env.get("FEISHU_APP_ID") or not env.get("FEISHU_APP_SECRET"):
        parser.error("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
    domain = str(env.get("FEISHU_DOMAIN") or "feishu").lower()
    if domain not in {"feishu", "lark"}:
        parser.error("FEISHU_DOMAIN must be feishu or lark")
    # Load only the pure renderer, without plugin registration or gateway startup.
    package = types.ModuleType("cardkit_wire_probe")
    package.__path__ = [str(Path(__file__).resolve().parent.parent)]
    sys.modules[package.__name__] = package
    layout = importlib.import_module(package.__name__ + ".card_layout")
    client = (lark.Client.builder().app_id(env["FEISHU_APP_ID"])
              .app_secret(env["FEISHU_APP_SECRET"])
              .domain(lark.LARK_DOMAIN if domain == "lark" else lark.FEISHU_DOMAIN)
              .log_level(lark.LogLevel.ERROR).build())
    ck = client.cardkit.v1

    def build(cls, **kwargs):
        builder = cls.builder()
        for key, value in kwargs.items():
            builder = getattr(builder, key)(value)
        return builder.build()

    def card(phase, answer, activity):
        return layout.build_stream_card(
            title="", markdown=answer, activity=activity, tool_count=2,
            footer="API 验证示例 · 3s · example-model", phase=phase,
            status=layout.status_label(phase, has_answer=bool(answer)),
        )

    def check(response, step):
        code = getattr(response, "code", None)
        print(json.dumps({"step": step, "code": code, "ok": code == 0}), flush=True)
        if code != 0:
            raise SystemExit("CardKit probe failed: " + step)
        return response

    print(json.dumps({"delivery": "unsent CardKit entity only", "domain": domain}), flush=True)
    response = check(ck.card.create(build(
        CreateCardRequest, request_body=build(CreateCardRequestBody, type="card_json",
        data=json.dumps(card("running", "", "尚无工具调用"), ensure_ascii=False)))), "create")
    card_id = response.data.card_id
    sequence = 0

    def fields():
        nonlocal sequence
        sequence += 1
        time.sleep(0.15)
        return {"sequence": sequence, "uuid": str(uuid.uuid4())}

    def content(answer, step):
        check(ck.card_element.content(build(
            ContentCardElementRequest, card_id=card_id, element_id=layout.ANSWER_ELEMENT_ID,
            request_body=build(ContentCardElementRequestBody, content=answer, **fields()))), step)

    def update(value, step):
        check(ck.card.update(build(UpdateCardRequest, card_id=card_id,
            request_body=build(UpdateCardRequestBody, card=build(Card, type="card_json",
            data=json.dumps(value, ensure_ascii=False)), **fields()))), step)

    content("API 验证正文第一段", "stream text")
    actions = layout.activity_update_actions("⏳ skill_view: sample", "⏳ 调用工具中 · 2s · example-model")
    check(ck.card.batch_update(build(BatchUpdateCardRequest, card_id=card_id,
        request_body=build(BatchUpdateCardRequestBody, actions=json.dumps(actions, ensure_ascii=False),
        **fields()))), "activity and footer batch")
    update(card("running", "API 验证正文第一段", "⏳ terminal"), "status while streaming")
    content("API 验证正文第一段。第二段继续。", "continue after status")
    answer = "API 验证完成。\n\n| 项目 | 数值 |\n|---|---|\n" + "\n".join(
        f"| 示例{i} | {i} |" for i in range(7))
    update(card("done", answer, "✅ terminal"), "final seal with table")
    print(json.dumps({"result": "passed", "operations": sequence + 1}), flush=True)


if __name__ == "__main__":
    main()
