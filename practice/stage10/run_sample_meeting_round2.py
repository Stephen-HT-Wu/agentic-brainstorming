"""同一個隔離 demo_workspace，換一個相關主題再跑一輪——展示『跨場記憶引用』
這個維度的真實非零數字（stage7/9 都驗證過同一模式：第一輪 Chroma 是空的，
recall 必然是 0，第二輪換相關主題才會真的命中）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

STAGE9_DIR = Path(__file__).resolve().parent.parent / "stage9"
sys.path.insert(0, str(STAGE9_DIR))
import graph as sg  # noqa: E402
import yaml  # noqa: E402

WORKSPACE = Path(__file__).resolve().parent / "demo_workspace"

sg.OUTPUT_DIR = WORKSPACE / "outputs"
sg.EVENTS_PATH = sg.OUTPUT_DIR / "events.jsonl"
sg.CHECKPOINT_DB_PATH = sg.OUTPUT_DIR / "demo_checkpoints.sqlite"
sg.CHROMA_DIR = WORKSPACE / "chroma_db"
sg.PROTOTYPE_DIR = sg.OUTPUT_DIR / "prototypes"
sg.REPORT_DIR = sg.OUTPUT_DIR / "reports"

sg.load_personas = lambda: yaml.safe_load((sg.PRACTICE_DIR / "personas.example.yaml").read_text(encoding="utf-8"))["personas"]
sg.load_users = lambda: yaml.safe_load((sg.PRACTICE_DIR / "users.example.yaml").read_text(encoding="utf-8"))["users"]
sg.load_company = lambda: (sg.PRACTICE_DIR / "company.example.md").read_text(encoding="utf-8")


if __name__ == "__main__":
    os.environ["BRAINSTORM_TOPIC"] = "公司要不要導入 AI 自動生成新聞內容"
    skip_all_path = WORKSPACE / "skip_all.json"
    sys.argv = [
        "run_sample_meeting_round2.py",
        "--thread", "demo-sample-round2",
        "--script", str(skip_all_path),
    ]
    print(f"=== 產生 demo 樣本會議第二輪（同一個隔離工作區，展示跨輪 recall）===")
    print(f"=== 主題：{os.environ['BRAINSTORM_TOPIC']} ===\n")
    sg.main()
