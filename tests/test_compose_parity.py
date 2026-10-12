"""两份 compose 文件的 environment 一致性守卫。

背景：docker-compose.yml（本地构建）与 docker-compose.prod.yml（拉镜像部署）
的 services.stock-board.environment 段曾逐行重复 38 行。YAML anchor 无法跨
文件共享，改 env_file 又会丢「shell 环境变量覆盖」语义，所以这里退而求其次：
用测试把「改一处忘一处」从静默漂移变成 CI 红灯。

修改任一份 compose 的 environment 时，必须同步另一份，本测试才会通过。
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent


def _load_env(service_name: str = "stock-board") -> dict:
    out = {}
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        doc = yaml.safe_load((_ROOT / name).read_text(encoding="utf-8"))
        out[name] = doc["services"][service_name].get("environment") or {}
    return out


def test_environment_sections_identical() -> None:
    envs = _load_env()
    dev, prod = envs["docker-compose.yml"], envs["docker-compose.prod.yml"]
    only_dev = sorted(set(dev) - set(prod))
    only_prod = sorted(set(prod) - set(dev))
    diff = sorted(
        k for k in set(dev) & set(prod) if dev[k] != prod[k]
    )
    assert not only_dev and not only_prod and not diff, (
        "两份 compose 的 environment 已漂移——请在另一份中同步修改。\n"
        f"仅 dev 有：{only_dev}\n仅 prod 有：{only_prod}\n值不同：{diff}"
    )


def test_environment_no_empty_placeholder() -> None:
    """防手滑：environment 值不应为空字符串占位（空值只允许 LLM_API_KEY）。"""
    for name, env in _load_env().items():
        empties = sorted(k for k, v in env.items() if v in ("", None))
        assert set(empties) <= {"LLM_API_KEY"}, f"{name} 出现空值占位：{empties}"
