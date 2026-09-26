#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bridge 侧测试（v0.3）：message_id 去重 + Runtime 真的被调用。

做法：真实起一个 server.py 子进程（独立端口/独立落盘目录，不污染 received/），
用真实 HTTP POST 打进去，再读回 turns.jsonl 断言。
    BRIDGE_LLM=none —— 测试不依赖外网模型；Runtime 仍然被调用（status=no_llm）。

运行： python test_dedupe.py      （标准库 unittest，无第三方依赖）
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(BASE_DIR, "server.py")
PORT = 8788
BASE_URL = "http://127.0.0.1:%d" % PORT


def _http_json(url, payload=None, timeout=15):
    if payload is None:
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _turns(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


class TestMessageIdDedupe(unittest.TestCase):
    tmp = None
    proc = None
    turns_path = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bridge-v03-test-")
        recv = os.path.join(cls.tmp, "received")
        os.makedirs(recv, exist_ok=True)
        cls.turns_path = os.path.join(recv, "turns.jsonl")

        env = dict(os.environ)
        env.update({
            "BRIDGE_PORT": str(PORT),
            "BRIDGE_RECV_DIR": recv,
            "BRIDGE_EVENT_LOG": os.path.join(cls.tmp, "event_log.jsonl"),
            "BRIDGE_STATE_FILE": os.path.join(cls.tmp, "state.json"),
            "BRIDGE_LOG": os.path.join(cls.tmp, "server.log"),
            "BRIDGE_LLM": "none",
        })
        cls.proc = subprocess.Popen(
            [sys.executable, SERVER],
            cwd=BASE_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )

        deadline = time.time() + 40
        last = None
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                out = cls.proc.stdout.read() if cls.proc.stdout else ""
                raise RuntimeError("server exited early:\n" + out)
            try:
                last = _http_json(BASE_URL + "/health", timeout=3)
                if last.get("ok"):
                    break
            except Exception:
                time.sleep(0.4)
        else:
            raise RuntimeError("server did not become healthy in time")
        cls.health = last

    @classmethod
    def tearDownClass(cls):
        if cls.proc and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        if cls.tmp:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def _post(self, message_id, text, role="user", with_id=True):
        payload = {
            "type": "BRIDGE_TURN",
            "source": "test://unittest",
            "role": role,
            "text": text,
            "at": "2026-09-26T00:00:00.000Z",
        }
        if with_id:
            payload["message_id"] = message_id
            payload["message_id_source"] = "attr:data-message-id"
        return _http_json(BASE_URL + "/turn", payload)

    # ---------------- 用例 ----------------

    def test_0_health_and_runtime_wired(self):
        self.assertTrue(self.health["ok"])
        self.assertEqual(self.health["version"], "0.4.0")
        self.assertEqual(self.health["runtime"]["status"], "wired")
        self.assertEqual(self.health["runtime"]["llm"], "none")

    def test_1_three_distinct_ids_three_records(self):
        texts = [
            "预算是200万，我们按这个假设继续测算。",
            "明白，按预算200万来测算。",
            "假设预算改成100万，之前我们讨论过预算是200万。",
        ]
        for i, t in enumerate(texts, start=1):
            r = self._post("msg-%d" % i, t)
            self.assertFalse(r["duplicate"], "msg-%d 不应判为重复" % i)
            self.assertEqual(r["turn"], i)

        recs = _turns(self.turns_path)
        self.assertEqual(len(recs), 3, "3 个不同 message_id 应恰好落 3 条")
        self.assertEqual([r["message_id"] for r in recs], ["msg-1", "msg-2", "msg-3"])

    def test_2_same_id_repeated_is_deduped(self):
        before = len(_turns(self.turns_path))
        r = self._post("msg-1", "预算是200万，我们按这个假设继续测算。")
        self.assertTrue(r["duplicate"])
        self.assertIsNone(r["turn"])
        self.assertEqual(r["runtime_status"], "skipped_duplicate")

        r2 = self._post("msg-3", "假设预算改成100万，之前我们讨论过预算是200万。")
        self.assertTrue(r2["duplicate"])

        recs = _turns(self.turns_path)
        self.assertEqual(len(recs), before, "重复 POST 不应新增记录")
        self.assertEqual(len(recs), 3)

    def test_3_runtime_was_actually_called(self):
        recs = _turns(self.turns_path)
        self.assertEqual(len(recs), 3)
        for r in recs:
            rt = r.get("_runtime")
            self.assertIsNotNone(rt, "每条记录都要带 _runtime 块")
            self.assertNotEqual(rt.get("status"), "not-called")
            self.assertIn(rt.get("status"), {"ok", "no_candidates", "no_llm", "error"})
            self.assertIsInstance(rt.get("records"), list)
            self.assertIsInstance(rt.get("effects"), list)
            self.assertIsInstance(rt.get("decisions"), list)

        # msg-3 会触发候选（假设值变化 + 引用过去），Runtime 应给出 decisions；
        # BRIDGE_LLM=none 时不应伪造判定，status 应为 no_llm 且不落事件。
        r3 = [r for r in recs if r["message_id"] == "msg-3"][0]
        self.assertEqual(r3["_runtime"]["status"], "no_llm")
        self.assertTrue(r3["_runtime"]["decisions"], "Runtime 应产出 decisions")
        self.assertEqual(r3["_runtime"]["records"], [], "没有模型时不得伪造事件记录")

    def test_4_legacy_payload_without_message_id(self):
        before = len(_turns(self.turns_path))
        r1 = self._post(None, "老客户端消息：没有 message_id 字段", with_id=False)
        self.assertFalse(r1["duplicate"])
        r2 = self._post(None, "老客户端消息：没有 message_id 字段", with_id=False)
        self.assertTrue(r2["duplicate"], "无 message_id 时用 role+text 哈希兜底去重")

        recs = _turns(self.turns_path)
        self.assertEqual(len(recs), before + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
