"""
Stage 10 工具一：產生 `demo/sample-run/` 用的公開樣本會議。

跟 stage1-9 不同，stage10 不是一個新的 agentic 概念，是建立在 stage9
完整管線之上的展示層——所以這裡不重寫一份 graph.py，而是直接匯入
stage9 的模組來跑，只做兩件事：

1. 強制只用公開範例設定（`personas.example.yaml`／`users.example.yaml`／
   `company.example.md`），完全不讀使用者真實的 `personas.yaml` 等檔案
   ——用 monkeypatch 換掉 `load_personas`/`load_users`/`load_company`，
   不是暫時搬走使用者的真實檔案再搬回來（那樣萬一中途出錯，真實設定
   檔可能留在錯誤的位置）。
2. 把所有輸出路徑（events.jsonl／checkpoint／Chroma／原型／報告）都
   指向一個獨立的 `demo_workspace/`，不會混進使用者真實的
   `practice/outputs/`、`practice/chroma_db/`——尤其是 Chroma，如果
   把虛構的示範資料寫進使用者真實在用的集體智慧庫，會污染之後
   `recall_memory` 真的檢索到假資料。

跑完後用 `build_replay.py` 把這裡產出的 events.jsonl 轉成回放 HTML，
再手動複製需要的檔案進 `demo/sample-run/`（唯一允許 commit 的產出）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

STAGE9_DIR = Path(__file__).resolve().parent.parent / "stage9"
sys.path.insert(0, str(STAGE9_DIR))
import graph as sg  # noqa: E402
import yaml  # noqa: E402

WORKSPACE = Path(__file__).resolve().parent / "demo_workspace"
WORKSPACE.mkdir(parents=True, exist_ok=True)

sg.OUTPUT_DIR = WORKSPACE / "outputs"
sg.EVENTS_PATH = sg.OUTPUT_DIR / "events.jsonl"
sg.CHECKPOINT_DB_PATH = sg.OUTPUT_DIR / "demo_checkpoints.sqlite"
sg.CHROMA_DIR = WORKSPACE / "chroma_db"
sg.PROTOTYPE_DIR = sg.OUTPUT_DIR / "prototypes"
sg.REPORT_DIR = sg.OUTPUT_DIR / "reports"


def _load_example_personas() -> list:
    data = yaml.safe_load((sg.PRACTICE_DIR / "personas.example.yaml").read_text(encoding="utf-8"))
    return data["personas"]


def _load_example_users() -> list:
    data = yaml.safe_load((sg.PRACTICE_DIR / "users.example.yaml").read_text(encoding="utf-8"))
    return data["users"]


def _load_example_company() -> str:
    return (sg.PRACTICE_DIR / "company.example.md").read_text(encoding="utf-8")


sg.load_personas = _load_example_personas
sg.load_users = _load_example_users
sg.load_company = _load_example_company


if __name__ == "__main__":
    os.environ.setdefault("BRAINSTORM_TOPIC", "如何提升新聞短影音互動率")
    skip_all_path = WORKSPACE / "skip_all.json"
    skip_all_path.write_text("{}", encoding="utf-8")
    sys.argv = [
        "run_sample_meeting.py",
        "--thread", "demo-sample",
        "--script", str(skip_all_path),
    ]
    print(f"=== 產生 demo 樣本會議（隔離工作區：{WORKSPACE}）===")
    print(f"=== 主題：{os.environ['BRAINSTORM_TOPIC']} ===\n")
    sg.main()
